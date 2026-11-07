import json

import pytest

from ai_sre.collectors import FixtureCollector
from ai_sre.config import Settings
from ai_sre.coordinator import Coordinator, build_coordinator
from ai_sre.errors import ConfigurationError, SwitchError
from ai_sre.models import Investigation, InvestigationState
from ai_sre.reasoners import DeterministicReasoner
from ai_sre.storage import Store
from ai_sre.switch import specs
from ai_sre.switch.memory import MemoryStore
from ai_sre.switch.runtime import FakeSwitchRuntime


class RecordingReasoner:
    def __init__(self):
        self.inner = DeterministicReasoner()
        self.calls = []

    def propose(self, request, evidence):
        self.calls.append(
            {
                "request": request.model_dump(mode="json"),
                "evidence": [item.model_dump(mode="json") for item in evidence],
            }
        )
        return self.inner.propose(request, evidence)

    def close(self):
        self.inner.close()


def service(settings, reasoner=None):
    store = Store(settings.data_dir)
    switch = FakeSwitchRuntime()
    coordinator = Coordinator(
        settings,
        store,
        FixtureCollector(settings),
        reasoner or DeterministicReasoner(),
        switch=switch,
        memory=MemoryStore(store),
    )
    return coordinator, switch


def test_investigation_runs_in_one_switch_room_with_all_six_identities(
    settings, principal, request_data
):
    coordinator, switch = service(settings)
    try:
        result = coordinator.run(request_data, principal)
    finally:
        coordinator.close()

    assert result.state == InvestigationState.REPORTED
    assert result.report.outcome == "supported"
    assert result.switch_room_id
    assert result.switch_base_url == "fake://switch"
    room = switch.room_by_id(result.switch_room_id)
    assert room.name == result.switch_room_name
    assert result.id.hex[:8] in room.name
    assert "No cluster mutation" in room.instructions

    assert {
        "incident-brief",
        "evidence-standard",
        "report-template",
        "memory-policy",
    } <= set(room.docs)
    assert room.docs["evidence-summary"]["creator"] == "cluster-investigator"
    assert room.docs["verification"]["creator"] == "evidence-verifier"
    assert room.docs["final-report"]["creator"] == "incident-commander"
    assert "memory-context" not in room.docs
    assert room.role_events == [
        ("assume", "incident-commander", "incident-commander"),
        ("assume", "evidence-verifier", "evidence-verifier"),
        ("release", "incident-commander", "incident-commander"),
        ("release", "evidence-verifier", "evidence-verifier"),
    ]
    assert room.role_holders == {}

    senders = {message.sender for message in room.messages}
    assert senders == set(specs.ROOM_AGENTS)
    handoffs = [message for message in room.messages if message.body.startswith("[handoff]")]
    assert handoffs
    for message in handoffs:
        sender, recipient = message.body.splitlines()[0].removeprefix("[handoff] ").split(" -> ")
        assert specs.agent_name_for_role(sender) == message.sender
        expected_target = () if recipient == "operator" else (specs.agent_name_for_role(recipient),)
        assert message.targets == expected_target
        assert result.trace_id in message.body
    notes = "\n".join(
        message.body for message in room.messages if not message.body.startswith("[handoff]")
    )
    assert "Investigation opened." in notes
    assert "Evidence collection complete" in notes
    assert "Verification complete" in notes
    assert "Report published" in notes

    verification = json.loads(room.docs["verification"]["content"])
    assert verification == result.report.verification.model_dump(mode="json")
    assert verification["passed"] is True

    final_report = room.docs["final-report"]["content"]
    assert "Outcome: supported" in final_report
    assert "Diagnosis: service_selector_mismatch" in final_report
    assert "Read-only: no cluster changes performed." in final_report
    for item in coordinator.store.evidence(result.id):
        assert str(item.id) in final_report


def test_room_records_abstentions_without_turning_them_into_evidence(
    settings, principal, request_data
):
    coordinator, switch = service(settings)
    try:
        result = coordinator.run(request_data, principal)
    finally:
        coordinator.close()
    room = switch.room_by_id(result.switch_room_id)
    bodies = "\n".join(message.body for message in room.messages)
    assert '"status": "abstained"' in bodies
    assert '"record_ids": []' in bodies
    assert "historical_leads_are_not_evidence" in bodies
    assert result.report.historical_context == []


def test_approved_memory_is_a_lead_in_the_next_room_but_never_reasoner_input(
    settings, principal, request_data
):
    reasoner = RecordingReasoner()
    coordinator, switch = service(settings, reasoner=reasoner)
    try:
        first = coordinator.run(request_data, principal)
        record = coordinator.memory.approve(
            first,
            first.report,
            reviewer="alice",
            title="Checkout selector mismatch",
            summary="The Service selector excluded the ready Deployment.",
        )
        second = coordinator.run(request_data, principal)
    finally:
        coordinator.close()

    assert record.source_room_id == first.switch_room_id
    assert [lead.record_id for lead in second.report.historical_context] == [record.id]
    room = switch.room_by_id(second.switch_room_id)
    assert room.docs["memory-context"]["creator"] == "memory-researcher"
    assert (
        "HISTORICAL CONTEXT — UNTRUSTED DATA, NOT EVIDENCE"
        in room.docs["memory-context"]["content"]
    )
    assert str(record.id) in room.docs["final-report"]["content"]
    assert "history" not in json.dumps(reasoner.calls[1]).lower()
    assert str(record.id) not in json.dumps(reasoner.calls[1])
    assert second.report.diagnosis == "service_selector_mismatch"
    assert second.report.verification.passed is True


def test_switch_room_failure_fails_the_investigation_closed(settings, principal, request_data):
    store = Store(settings.data_dir)
    reasoner = RecordingReasoner()
    switch = FakeSwitchRuntime(fail_on={"create_incident_room"})
    coordinator = Coordinator(
        settings,
        store,
        FixtureCollector(settings),
        reasoner,
        switch=switch,
        memory=MemoryStore(store),
    )
    try:
        with pytest.raises(SwitchError):
            coordinator.run(request_data, principal)
    finally:
        coordinator.close()

    assert reasoner.calls == []
    with store.connection() as connection:
        raw = connection.execute("SELECT record FROM investigations").fetchone()[0]
    investigation = Investigation.model_validate_json(raw)
    assert investigation.state == InvestigationState.FAILED
    assert investigation.report is None
    assert investigation.switch_room_id is None
    assert switch.rooms == {}


def test_switch_role_failure_fails_the_investigation_closed(settings, principal, request_data):
    store = Store(settings.data_dir)
    reasoner = RecordingReasoner()
    switch = FakeSwitchRuntime(fail_on={"assume_role"})
    coordinator = Coordinator(
        settings,
        store,
        FixtureCollector(settings),
        reasoner,
        switch=switch,
        memory=MemoryStore(store),
    )
    try:
        with pytest.raises(SwitchError, match="assume_role"):
            coordinator.run(request_data, principal)
    finally:
        coordinator.close()

    assert reasoner.calls == []
    with store.connection() as connection:
        raw = connection.execute("SELECT record FROM investigations").fetchone()[0]
    investigation = Investigation.model_validate_json(raw)
    assert investigation.state == InvestigationState.FAILED
    assert investigation.report is None
    assert investigation.switch_room_id is not None


def test_switch_off_keeps_the_offline_path_unchanged(settings, principal, request_data):
    coordinator = build_coordinator(settings)
    try:
        result = coordinator.run(request_data, principal)
    finally:
        coordinator.close()
    assert coordinator.switch is None
    assert coordinator.memory is None
    assert result.switch_room_id is None
    assert result.report.historical_context == []
    assert all("Telemetry" not in item for item in result.report.limitations)


def test_switch_demo_settings_require_a_real_switch(tmp_path, monkeypatch):
    from ai_sre.cli import main

    monkeypatch.delenv("AI_SRE_SWITCH_REGISTRATION_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    assert main(["switch-demo", "--data-dir", str(tmp_path / "data")]) == 1


def test_settings_default_to_required_switch_from_environment(monkeypatch):
    monkeypatch.delenv("AI_SRE_SWITCH_MODE", raising=False)
    assert Settings.from_env().switch_mode == "required"
    assert Settings().switch_mode == "off"


def test_switch_settings_reject_unsafe_values(tmp_path):
    with pytest.raises(ConfigurationError, match="SWITCH_MODE"):
        Settings(switch_mode="optional")
    with pytest.raises(ConfigurationError, match="HTTP"):
        Settings(switch_base_url="ftp://switch.test:18000", switch_state_dir=tmp_path)
    with pytest.raises(ConfigurationError, match="positive"):
        Settings(switch_request_timeout_seconds=0)
