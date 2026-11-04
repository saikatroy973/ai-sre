import importlib.util
from pathlib import Path


def load_script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_trust_gate_api_base_uses_v1():
    gate = load_script("trust_gate")
    assert gate.api_base("https://app.switchagents.ai") == "https://api.switchagents.ai/api/v1"
    assert gate.api_base("http://localhost:4000") == "http://localhost:4000/api/v1"
