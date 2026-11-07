import ast
from pathlib import Path


def test_trust_harness_is_a_scannable_agent_with_guardrails():
    path = Path(__file__).resolve().parents[1] / "src" / "ai_sre" / "trust_harness.py"
    tree = ast.parse(path.read_text())
    imports = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "agents" in imports
    assert "fastapi" in imports
    source = path.read_text()
    assert "input_guardrails=" in source
    assert "output_guardrails=" in source
    assert "max_turns=4" in source
    assert "@function_tool" in source
    assert '@app.post("/run")' in source
