from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest

from kube_investigator.auth import Principal
from kube_investigator.coordinator import build_coordinator
from kube_investigator.errors import AuthorizationError, BusyError, InvestigatorError, NotFoundError
from kube_investigator.messaging import MessageBus
from kube_investigator.models import AgentRole, EvidenceKind, InvestigationState
from kube_investigator.storage import Store


@pytest.mark.parametrize(
    ("scenario", "outcome"),
    [
        ("selector-mismatch", "supported"),
        ("healthy", "insufficient_evidence"),
        ("ambiguous", "insufficient_evidence"),
    ],
)
def test_end_to_end_scenarios(settings, request_data, principal, scenario, outcome):
    service = build_coordinator(replace(settings, scenario=scenario))
    try:
        result = service.run(request_data, principal)
        assert result.state == InvestigationState.REPORTED
        assert result.report.outcome == outcome
        assert result.report.read_only is True
        assert result.report.verification.passed is (outcome == "supported")
        captured = service.store.evidence(result.id)
        assert len(captured) == 5
        assert {item.kind for item in captured} == set(EvidenceKind)
        assert all(item.sha256 == item.digest() for item in captured)
        ids = {item.id for item in captured}
        assert all(set(claim.evidence_ids) <= ids for claim in result.report.verification.claims)
        assert Store(settings.data_dir).get(result.id) == result
    finally:
        service.close()


def test_messages_have_bounded_handoffs_and_connected_trace(coordinator, request_data, principal):
    result = coordinator.run(request_data, principal)
    messages = coordinator.store.messages(result.id)
    assert [message.type for message in messages] == [
        "task_request",
        "task_result",
        "verification_request",
        "verification_result",
        "report",
    ]
    assert {message.trace_id for message in messages} == {result.trace_id}
    seen = set()
    for message in messages:
        assert message.parent_span_id is None or message.parent_span_id in seen
        seen.add(message.span_id)
    assert messages[0].task_id == messages[1].task_id
    assert messages[2].task_id == messages[3].task_id
    assert messages[0].payload["max_tool_calls"] == 5


@pytest.mark.parametrize("field,value", [("cluster", "other-cluster"), ("namespace", "finance")])
def test_scope_denied_before_collection(
    coordinator, request_data, principal, monkeypatch, field, value
):
    def forbidden(*args):
        pytest.fail("Collector must not be invoked for unauthorized targets")

    monkeypatch.setattr(coordinator.collector, "collect", forbidden)
    request_data = request_data.model_copy(update={field: value})
    with pytest.raises(AuthorizationError):
        coordinator.run(request_data, principal)
    with coordinator.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM investigations").fetchone()[0] == 0


def test_principal_cannot_expand_server_scope(coordinator, request_data):
    forged = Principal("other", "other-tenant", "demo-cluster", frozenset({"shop"}))
    with pytest.raises(AuthorizationError):
        coordinator.run(request_data, forged)


def test_read_is_owner_and_scope_bound(coordinator, request_data, principal):
    result = coordinator.run(request_data, principal)
    for other in (
        replace(principal, id="other-operator"),
        replace(principal, tenant="other-tenant"),
        replace(principal, namespaces=frozenset({"other"})),
    ):
        with pytest.raises(NotFoundError):
            coordinator.get(result.id, other)


def test_missing_target_produces_partial_report(coordinator, request_data, principal):
    result = coordinator.run(request_data.model_copy(update={"service": "missing"}), principal)
    assert result.report.outcome == "partial"
    assert result.report.diagnosis == "undetermined"
    assert result.report.verification.claims == []
    assert all(item.error_code == "not_found" for item in coordinator.store.evidence(result.id))


def test_incomplete_collection_does_not_spend_a_model_call(
    coordinator, request_data, principal, monkeypatch
):
    def forbidden(*args):
        pytest.fail("Incomplete evidence must not be sent to a model")

    monkeypatch.setattr(coordinator.reasoner, "propose", forbidden)
    result = coordinator.run(request_data.model_copy(update={"service": "missing"}), principal)
    assert result.report.outcome == "partial"
    messages = coordinator.store.messages(result.id)
    assert messages[1].payload["reasoning_skipped"] == "incomplete_collection"


def test_unexpected_failure_is_persisted_sanitized_and_releases_capacity(
    coordinator, request_data, principal, monkeypatch
):
    original = coordinator.collector.collect

    def fail(*args):
        raise RuntimeError("private-source-error-with-secret")

    monkeypatch.setattr(coordinator.collector, "collect", fail)
    with pytest.raises(InvestigatorError) as error:
        coordinator.run(request_data, principal)
    assert str(error.value) == "Investigation could not complete"
    with coordinator.store.connection() as connection:
        record = connection.execute("SELECT state, record FROM investigations").fetchone()
    assert record[0] == "failed"
    assert "private-source-error-with-secret" not in record[1]
    monkeypatch.setattr(coordinator.collector, "collect", original)
    assert coordinator.run(request_data, principal).report.outcome == "supported"


def test_admission_is_bounded_and_retryable(coordinator, request_data, principal):
    for _ in range(coordinator.settings.max_concurrent_investigations):
        assert coordinator.slots.acquire(blocking=False)
    try:
        with pytest.raises(BusyError):
            coordinator.run(request_data, principal)
    finally:
        for _ in range(coordinator.settings.max_concurrent_investigations):
            coordinator.slots.release()
    assert coordinator.run(request_data, principal).report.outcome == "supported"


def test_parallel_investigations_do_not_share_evidence_or_trace(
    coordinator, request_data, principal
):
    with ThreadPoolExecutor(max_workers=3) as workers:
        results = list(workers.map(lambda _: coordinator.run(request_data, principal), range(3)))
    assert len({result.id for result in results}) == 3
    assert len({result.trace_id for result in results}) == 3
    all_ids = []
    for result in results:
        assert result.report.outcome == "supported"
        captured = coordinator.store.evidence(result.id)
        assert {item.investigation_id for item in captured} == {result.id}
        all_ids.extend(item.id for item in captured)
    assert len(set(all_ids)) == 15


def test_specialist_cannot_dispatch_to_arbitrary_peer(coordinator, investigation):
    coordinator.store.create(investigation)
    with pytest.raises(ValueError, match="not allowed"):
        MessageBus(coordinator.store).send(
            investigation,
            sender=AgentRole.INVESTIGATOR,
            recipient=AgentRole.VERIFIER,
            task_id=uuid4(),
            parent_task_id=investigation.id,
            parent_span_id=None,
            payload={},
        )
