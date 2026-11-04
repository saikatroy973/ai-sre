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
