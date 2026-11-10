"""CodeScan preflight: quota exhaustion is a skip, other provider errors fail."""

import importlib.util
import io
import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "trust_llm_preflight.py"
spec = importlib.util.spec_from_file_location("trust_llm_preflight", SCRIPT)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def set_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SWITCH_TRUST_LLM_MODEL", "openai:test-model")
    monkeypatch.setenv("SWITCH_TRUST_LLM_API_KEY", "test-key")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))


def http_error(code, payload):
    body = io.BytesIO(json.dumps(payload).encode())

    def fail(*args, **kwargs):
        raise HTTPError("https://provider.test", code, "error", {}, body)

    return fail


def test_exhausted_quota_skips_instead_of_failing(monkeypatch, tmp_path):
    set_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        preflight,
        "urlopen",
        http_error(429, {"error": {"code": "credit_balance_exhausted"}}),
    )
    assert preflight.main() == 0
    assert (tmp_path / "output").read_text() == "skip=true\n"


def test_other_http_errors_still_fail(monkeypatch, tmp_path):
    set_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        preflight, "urlopen", http_error(401, {"error": {"code": "invalid_api_key"}})
    )
    with pytest.raises(SystemExit) as exc:
        preflight.main()
    assert exc.value.code and "HTTP 401" in str(exc.value.code)


def test_network_failure_still_fails(monkeypatch, tmp_path):
    set_env(monkeypatch, tmp_path)

    def fail(*args, **kwargs):
        raise URLError("dns failure")

    monkeypatch.setattr(preflight, "urlopen", fail)
    with pytest.raises(SystemExit) as exc:
        preflight.main()
    assert "request failed" in str(exc.value.code)
