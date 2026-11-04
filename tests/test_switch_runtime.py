from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from kube_investigator.errors import ConfigurationError, SwitchError
from kube_investigator.models import InvestigationRequest, Scope, utc_now
from kube_investigator.switch.identity import AgentCredential, CredentialStore
from kube_investigator.switch.memory import MemoryRecord
from kube_investigator.switch.runtime import (
    FakeSwitchRuntime,
    build_switch_runtime,
    incident_room_name,
)
from kube_investigator.switch.specs import CORE_ROOMS


def record():
    now = utc_now()
    return MemoryRecord.create(
        id=uuid4(),
        scope=Scope(tenant="local", cluster="demo-cluster", namespace="shop"),
        title="Checkout selector mismatch",
        summary="Approved historical incident.",
        diagnosis="service_selector_mismatch",
        confidence="confirmed",
        source_investigation_id=uuid4(),
        source_room_id="room-source",
        source_trace_id="trace-1",
        source_evidence_ids=[uuid4()],
        source_message_ids=[uuid4()],
        reviewer="alice",
        approved_at=now,
        expires_at=now + timedelta(days=30),
    )


def test_fake_bootstrap_registers_agents_and_creates_core_rooms():
    runtime = FakeSwitchRuntime()
    result = runtime.bootstrap()
    assert result.agents == tuple(runtime.registered)
    assert len(result.agents) == 6
    assert set(result.rooms) == set(CORE_ROOMS)
    assert {room.name for room in runtime.rooms.values()} == set(CORE_ROOMS.values())
    assert runtime.rooms["k8s-incidents"].links
    runtime.bootstrap()
    assert len(runtime.rooms) == 4


def test_fake_incident_room_has_the_template_documents_and_members():
    runtime = FakeSwitchRuntime()
    runtime.bootstrap()
    investigation = _investigation()
    room = runtime.create_incident_room(investigation)
    fake = runtime.room_by_id(room.id)
    assert fake.name == incident_room_name(investigation)
    assert set(fake.docs) == {
        "incident-brief",
        "evidence-standard",
        "report-template",
        "memory-policy",
    }
    assert fake.agents == (
        "incident-commander",
        "cluster-investigator",
        "telemetry-analyst",
        "change-analyst",
        "memory-researcher",
        "evidence-verifier",
    )
    assert len(fake.links) == 2


def test_fake_session_posts_as_the_real_sender_and_targets_a_recipient():
    runtime = FakeSwitchRuntime()
    runtime.bootstrap()
    room = runtime.create_incident_room(_investigation())
    with runtime.open_session("cluster-investigator", room.id) as session:
        event_id = session.post("evidence is ready", targets=["incident-commander"])
        document_id = session.create_document(
            name="evidence-summary",
            description="d",
            instructions="i",
            content="c",
        )
        session.update_document(document_id, content="c2")
    fake = runtime.room_by_id(room.id)
    assert fake.messages[-1].sender == "cluster-investigator"
    assert fake.messages[-1].targets == ("incident-commander",)
    assert event_id
    assert fake.docs["evidence-summary"]["content"] == "c2"
    assert session.closed is True


def test_fake_history_reads_room_messages():
    runtime = FakeSwitchRuntime()
    runtime.bootstrap()
    room = runtime.create_incident_room(_investigation())
    with runtime.open_session("incident-commander", room.id) as session:
        session.post("hello")
    history = runtime.history("incident-commander", room.id, limit=10)
    assert history == [
        {
            "sender": "incident-commander",
            "body": "hello",
            "event_id": history[0]["event_id"],
        }
    ]


def test_fake_memory_publishing_writes_a_document_and_a_message():
    runtime = FakeSwitchRuntime()
    runtime.bootstrap()
    item = record()
    document_id = runtime.post_memory(item)
    memory_room = runtime.room_by_id(runtime.room_ids["memory"])
    assert memory_room.docs[f"memory-{item.id}"]["id"] == document_id
    assert memory_room.messages[-1].sender == "memory-researcher"
    assert str(item.id) in memory_room.messages[-1].body


def test_fake_runtime_can_fail_closed_by_operation():
    runtime = FakeSwitchRuntime(fail_on={"create_incident_room"})
    with pytest.raises(SwitchError, match="Simulated Switch failure"):
        runtime.create_incident_room(_investigation())
    runtime = FakeSwitchRuntime(fail_on={"post"})
    runtime.bootstrap()
    room = runtime.create_incident_room(_investigation())
    with runtime.open_session("incident-commander", room.id) as session:
        with pytest.raises(SwitchError, match="Simulated Switch failure"):
            session.post("hello")


def test_incident_room_names_are_bounded_and_dns_safe():
    investigation = _investigation(
        InvestigationRequest(
            question="Why?",
            cluster="demo-cluster",
            namespace="shop",
            service="a" * 63,
            deployment="checkout",
        )
    )
    name = incident_room_name(investigation)
    assert name.startswith(f"incident-{str(investigation.id)[:8]}-shop-")
    assert len(name) <= 63
    assert name == name.lower().strip("-")
    assert all(character.isalnum() or character == "-" for character in name)


def test_switch_off_builds_no_runtime(settings):
    assert build_switch_runtime(settings) is None


def test_switch_required_refuses_to_start_without_registered_agents(tmp_path, settings):
    required = replace(
        settings,
        switch_mode="required",
        switch_state_dir=tmp_path / "switch",
        switch_registration_token=None,
    )
    with pytest.raises(ConfigurationError, match="not all agents are registered"):
        build_switch_runtime(required)


def test_switch_required_refuses_a_partial_credential_store(tmp_path, settings):
    state = tmp_path / "switch"
    CredentialStore(state / "agents.json", "http://127.0.0.1:18000").save(
        {
            "incident-commander": AgentCredential(
                name="incident-commander", agent_id="a1", api_key="one-time"
            )
        }
    )
    required = replace(
        settings,
        switch_mode="required",
        switch_state_dir=state,
        switch_registration_token=None,
    )
    with pytest.raises(ConfigurationError, match="not all agents are registered"):
        build_switch_runtime(required)


def _investigation(request=None):
    from kube_investigator.auth import Principal
    from kube_investigator.config import Settings
    from kube_investigator.models import Investigation

    request = request or InvestigationRequest(
        question="Why is checkout failing?",
        cluster="demo-cluster",
        namespace="shop",
        service="checkout",
        deployment="checkout",
    )
    principal = Principal.operator(Settings())
    return Investigation(
        principal_id=principal.id,
        scope=principal.authorize(request),
        request=request,
    )
