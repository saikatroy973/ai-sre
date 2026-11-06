"""Live telemetry tools: auth, fail-closed configuration, and redaction."""

import base64
import json
import time

from cryptography.hazmat.primitives import serialization

from kube_investigator.mcp_auth import generate_actor_keypair
from kube_investigator.mcp_server import (
    kube_investigator_logs,
    kube_investigator_metrics,
    source_status,
)
from kube_investigator.telemetry_source import redact


def configure(monkeypatch, tmp_path):
    monkeypatch.setenv("INVESTIGATOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INVESTIGATOR_SWITCH_MODE", "off")
    monkeypatch.setenv("INVESTIGATOR_BACKEND", "fixture")
    monkeypatch.delenv("INVESTIGATOR_KUBE_CONTEXT", raising=False)


def token(private_path, tools):
    private_key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    payload = {
        "actor": "telemetry-analyst",
        "tenant": "local",
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
        "tools": tools,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    return f"{encode(raw)}.{encode(private_key.sign(raw))}"


def test_redact_hides_credentials():
    line = "connecting password=hunter2 token: abc123 Authorization: Bearer xyz"
    redacted = redact(line)
    assert "hunter2" not in redacted
    assert "abc123" not in redacted
    assert "[redacted]" in redacted


def test_logs_fail_closed_without_a_live_backend(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    result = kube_investigator_logs("imesh-demo")
    assert result["ok"] is False
    assert "live telemetry requires" in result["error"]


def test_metrics_fail_closed_without_a_live_backend(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    result = kube_investigator_metrics("imesh-demo")
    assert result["ok"] is False
    assert "live telemetry requires" in result["error"]


def test_source_status_reports_live_telemetry(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("INVESTIGATOR_BACKEND", "kubernetes")
    monkeypatch.setenv("INVESTIGATOR_KUBE_CONTEXT", "kind-imesh")
    status = source_status("telemetry")
    assert status["configured"] is True
    assert status["sources"] == ["pod-logs", "metrics-server"]


def test_logs_require_the_logs_claim(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    private_path, public_key = generate_actor_keypair(tmp_path / "auth")
    monkeypatch.setenv("INVESTIGATOR_MCP_PUBLIC_KEY", public_key)
    monkeypatch.setenv("INVESTIGATOR_MCP_ACTOR", "telemetry-analyst")
    monkeypatch.setenv("INVESTIGATOR_MCP_ACTOR_TOKEN", token(private_path, ["get"]))
    result = kube_investigator_logs("imesh-demo")
    assert result["ok"] is False
    assert result["error"] == "MCP actor is not authorized for this tool"
