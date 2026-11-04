import importlib.util
from pathlib import Path


def load_script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_trust_gate_parses_tabular_issue_rows():
    gate = load_script("trust_gate")
    payload = {
        "header": ["rule_id", "severity", "issue_id", "rule_name"],
        "rows": [
            ["r-1", "CRITICAL", "i-1", "Model security risk"],
            ["r-2", "MEDIUM", "i-2", "Model misuse risk"],
        ],
        "total": 2,
    }
    rows = gate.issue_rows(payload)
    assert rows[0]["severity"] == "CRITICAL"
    assert gate.failing_findings(rows, "high") == [rows[0]]
    assert gate.failing_findings(rows, "medium") == rows


def test_trust_gate_rejects_unknown_row_shape():
    gate = load_script("trust_gate")
    try:
        gate.issue_rows({"rows": [["unparseable"]]})
    except SystemExit as exc:
        assert "unknown shape" in str(exc)
    else:
        raise AssertionError("unknown issue rows must fail closed")


def test_trust_gate_fails_high_and_unknown_severity():
    gate = load_script("trust_gate")
    rows = [
        {"severity": "low"},
        {"severity": "high"},
        {"severity": "unexpected"},
    ]
    assert gate.failing_findings(rows, "high") == rows[1:]
    assert gate.failing_findings(rows, "critical") == rows[2:]
    assert gate.finding_severity({"ai_spm_severity": "Critical"}) == "critical"
    assert gate.finding_severity({"severity": "Informational"}) == "info"
    assert gate.finding_severity({"severity": "unexpected"}) is None


def test_trust_gate_api_base_uses_v1():
    gate = load_script("trust_gate")
    assert gate.api_base("https://app.switchagents.ai") == "https://api.switchagents.ai/api/v1"
    assert gate.api_base("http://localhost:4000") == "http://localhost:4000/api/v1"
