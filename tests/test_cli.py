import json

from kube_investigator.cli import main


def test_demo_is_offline_even_if_live_and_model_environment_is_set(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("INVESTIGATOR_BACKEND", "kubernetes")
    monkeypatch.setenv("INVESTIGATOR_REASONER", "scale")
    assert main(["demo", "--json", "--data-dir", str(tmp_path / "demo")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["report"]["outcome"] == "supported"
    assert result["report"]["usage"] is None
    assert result["report"]["limitations"][0] == "Fixture data; no cluster was contacted."


def test_demo_output_exposes_handoffs_and_citations(tmp_path, capsys):
    assert main(["demo", "--data-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "coordinator -> cluster-investigator" in output
    assert "evidence-verifier -> coordinator" in output
    assert "Evidence:" in output
    assert "Read-only: no cluster changes performed." in output


def test_report_can_be_retrieved_after_process_state_is_gone(tmp_path, capsys):
    assert main(["demo", "--json", "--data-dir", str(tmp_path)]) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(["show", first["id"], "--json", "--data-dir", str(tmp_path)]) == 0
    second = json.loads(capsys.readouterr().out)
    assert first == second


def test_healthy_scenario_abstains(tmp_path, capsys):
    assert main(["demo", "--scenario", "healthy", "--json", "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["report"]["outcome"] == "insufficient_evidence"


def test_show_does_not_initialize_runtime_adapters(tmp_path, capsys, monkeypatch):
    assert main(["demo", "--json", "--data-dir", str(tmp_path)]) == 0
    first = json.loads(capsys.readouterr().out)
    monkeypatch.setenv("INVESTIGATOR_BACKEND", "kubernetes")
    monkeypatch.setenv("INVESTIGATOR_KUBE_CONTEXT", "explicit-read-only-context")

    def forbidden(*args):
        raise AssertionError("Report reads must not initialize Kubernetes/model adapters")

    monkeypatch.setattr("kube_investigator.cli.build_coordinator", forbidden)
    assert main(["show", first["id"], "--json", "--data-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == first


def test_audit_command_reads_and_verifies_tool_log(tmp_path, capsys):
    from kube_investigator.audit import append_audit

    data_dir = tmp_path / "data"
    assert append_audit(
        data_dir,
        "tool-audit",
        {"tool": "get", "actor": "agent-a", "outcome": "ok", "error": None},
    )

    assert main(["audit", "--json", "--data-dir", str(data_dir)]) == 0
    entries = json.loads(capsys.readouterr().out)
    assert entries[0]["actor"] == "agent-a"
    assert entries[0]["tool"] == "get"
    assert main(["audit", "--verify", "--data-dir", str(data_dir)]) == 0
    assert "Tool audit chain is valid." in capsys.readouterr().out


def test_audit_verify_rejects_an_empty_log(tmp_path, capsys):
    assert main(["audit", "--verify", "--data-dir", str(tmp_path / "empty")]) == 1
    output = capsys.readouterr().out
    assert "Tool audit chain is invalid." in output
    assert "audit log is empty" in output


def test_external_audit_anchor_detects_truncation_and_deletion(tmp_path, monkeypatch):
    from kube_investigator.audit import append_audit, verify_audit

    data_dir = tmp_path / "data"
    anchor_dir = tmp_path / "anchor"
    monkeypatch.setenv("INVESTIGATOR_AUDIT_ANCHOR_DIR", str(anchor_dir))
    assert append_audit(data_dir, "tool-audit", {"tool": "get", "actor": "a"})
    assert append_audit(data_dir, "tool-audit", {"tool": "verify", "actor": "a"})
    ok, errors = verify_audit(data_dir, "tool-audit")
    assert ok is True
    assert errors == []
    anchors = list(anchor_dir.glob("tool-audit.*.anchor.json"))
    assert len(anchors) == 1

    audit_path = data_dir / "tool-audit.jsonl"
    lines = audit_path.read_text().splitlines()
    audit_path.write_text(lines[0] + "\n")
    ok, errors = verify_audit(data_dir, "tool-audit")
    assert ok is False
    assert "external audit anchor entry count mismatch" in errors

    audit_path.unlink()
    ok, errors = verify_audit(data_dir, "tool-audit")
    assert ok is False
    assert "audit log is empty" in errors
    assert "external audit anchor entry count mismatch" in errors


def test_audit_verify_rejects_an_empty_log_without_an_anchor(tmp_path, capsys):
    from kube_investigator.audit import verify_audit

    ok, errors = verify_audit(tmp_path / "empty", "tool-audit")
    assert ok is False
    assert errors == ["audit log is empty"]
