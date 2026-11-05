import asyncio
import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kube_investigator.config import Settings
from kube_investigator.mcp_auth import authorize_actor, verify_actor_token
from kube_investigator.mcp_server import mcp, search_memory


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def keypair():
    private_key = Ed25519PrivateKey.generate()
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_key, b64(public_raw)


def token(private_key, *, actor="memory-researcher", tenant="local", ttl=3600):
    now = int(time.time())
    payload = {"actor": actor, "tenant": tenant, "iat": now, "exp": now + ttl}
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"{b64(payload_bytes)}.{b64(private_key.sign(payload_bytes))}"


def test_signed_actor_token_verifies_and_expires():
    private_key, public_key = keypair()
    payload = verify_actor_token(token(private_key), public_key)
    assert payload["actor"] == "memory-researcher"
    assert payload["tenant"] == "local"

    with pytest.raises(ValueError, match="expired"):
        verify_actor_token(token(private_key, ttl=-1), public_key)


def test_authorize_actor_requires_a_matching_valid_token(monkeypatch, tmp_path):
    private_key, public_key = keypair()
    settings = Settings(tenant="local", data_dir=tmp_path)
    monkeypatch.setenv("INVESTIGATOR_MCP_PUBLIC_KEY", public_key)
    monkeypatch.setenv("INVESTIGATOR_MCP_ACTOR", "memory-researcher")

    monkeypatch.delenv("INVESTIGATOR_MCP_ACTOR_TOKEN", raising=False)
    actor, error = authorize_actor(settings)
    assert actor is None
    assert error == "MCP actor token is required"

    monkeypatch.setenv(
        "INVESTIGATOR_MCP_ACTOR_TOKEN",
        token(private_key, actor="memory-researcher", tenant="local"),
    )
    actor, error = authorize_actor(settings)
    assert actor == "memory-researcher"
    assert error is None

    monkeypatch.setenv(
        "INVESTIGATOR_MCP_ACTOR_TOKEN",
        token(private_key, actor="memory-researcher", tenant="other"),
    )
    actor, error = authorize_actor(settings)
    assert actor is None
    assert error == "MCP actor token is outside the configured tenant"

    monkeypatch.setenv(
        "INVESTIGATOR_MCP_ACTOR_TOKEN",
        token(private_key, actor="incident-commander", tenant="local"),
    )
    actor, error = authorize_actor(settings)
    assert actor is None
    assert error == "MCP actor does not match the signed token"


def test_authorize_actor_falls_back_to_env_label_without_public_key(monkeypatch, tmp_path):
    settings = Settings(tenant="local", data_dir=tmp_path)
    monkeypatch.delenv("INVESTIGATOR_MCP_PUBLIC_KEY", raising=False)
    monkeypatch.setenv("INVESTIGATOR_MCP_ACTOR", "memory-researcher")
    actor, error = authorize_actor(settings)
    assert actor == "memory-researcher"
    assert error is None

    monkeypatch.delenv("INVESTIGATOR_MCP_ACTOR", raising=False)
    actor, error = authorize_actor(settings)
    assert actor == "mcp"
    assert error is None


def test_search_memory_rejects_missing_or_foreign_token(monkeypatch, tmp_path):
    private_key, public_key = keypair()
    monkeypatch.setenv("INVESTIGATOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INVESTIGATOR_TENANT", "local")
    monkeypatch.setenv("INVESTIGATOR_SWITCH_MODE", "off")
    monkeypatch.setenv("INVESTIGATOR_MCP_PUBLIC_KEY", public_key)
    monkeypatch.setenv("INVESTIGATOR_MCP_ACTOR", "memory-researcher")
    monkeypatch.delenv("INVESTIGATOR_MCP_ACTOR_TOKEN", raising=False)

    missing = search_memory(
        cluster="demo-cluster",
        namespace="shop",
        query="checkout selector",
    )
    assert missing["ok"] is False
    assert missing["error"] == "MCP actor token is required"

    monkeypatch.setenv(
        "INVESTIGATOR_MCP_ACTOR_TOKEN",
        token(private_key, actor="memory-researcher", tenant="other"),
    )
    foreign = search_memory(
        cluster="demo-cluster",
        namespace="shop",
        query="checkout selector",
    )
    assert foreign["ok"] is False
    assert foreign["error"] == "MCP actor token is outside the configured tenant"

    monkeypatch.setenv(
        "INVESTIGATOR_MCP_ACTOR_TOKEN",
        token(private_key, actor="memory-researcher", tenant="local"),
    )
    allowed = search_memory(
        cluster="demo-cluster",
        namespace="shop",
        query="checkout selector",
    )
    assert allowed["ok"] is True
    assert allowed["count"] == 0


def test_every_evidence_tool_requires_the_signed_token(monkeypatch, tmp_path):
    """run/get/verify/source_status are gated exactly like memory search."""
    from uuid import uuid4

    from kube_investigator.mcp_server import (
        get_investigation,
        run_investigation,
        source_status,
        verify_candidate,
    )

    private_key, public_key = keypair()
    monkeypatch.setenv("INVESTIGATOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INVESTIGATOR_SWITCH_MODE", "off")
    monkeypatch.setenv("INVESTIGATOR_MCP_PUBLIC_KEY", public_key)
    monkeypatch.setenv("INVESTIGATOR_MCP_ACTOR", "incident-commander")
    monkeypatch.delenv("INVESTIGATOR_MCP_ACTOR_TOKEN", raising=False)

    missing = "MCP actor token is required"
    assert run_investigation(
        question="Why is checkout failing?",
        cluster="demo-cluster",
        namespace="shop",
        service="checkout",
        deployment="checkout",
    ) == {"ok": False, "error": missing}
    assert get_investigation(str(uuid4())) == {"ok": False, "error": missing}
    assert get_investigation("not-a-uuid") == {"ok": False, "error": missing}
    assert verify_candidate(str(uuid4()), "service_selector_mismatch", []) == {
        "ok": False,
        "error": missing,
    }
    assert source_status("kubernetes") == {"ok": False, "error": missing}

    monkeypatch.setenv(
        "INVESTIGATOR_MCP_ACTOR_TOKEN",
        token(private_key, actor="incident-commander", tenant="local"),
    )
    status = source_status("kubernetes")
    assert status["ok"] is True
    assert status["configured"] is False

    # Without a token at all, health stays open: it is a liveness probe.
    monkeypatch.delenv("INVESTIGATOR_MCP_ACTOR_TOKEN", raising=False)
    health = asyncio.run(mcp.call_tool("kube_investigator_health", {}))
    assert "ok" in json.dumps(health, default=str)


def test_every_authenticated_tool_writes_actor_attributed_audit(monkeypatch, tmp_path):
    from kube_investigator.audit import read_audit, verify_audit
    from kube_investigator.mcp_server import (
        get_investigation,
        run_investigation,
        source_status,
        verify_candidate,
    )

    private_key, public_key = keypair()
    data_dir = tmp_path / "data"
    monkeypatch.setenv("INVESTIGATOR_DATA_DIR", str(data_dir))
    monkeypatch.setenv("INVESTIGATOR_BACKEND", "fixture")
    monkeypatch.setenv("INVESTIGATOR_SCENARIO", "selector-mismatch")
    monkeypatch.setenv("INVESTIGATOR_REASONER", "deterministic")
    monkeypatch.setenv("INVESTIGATOR_SWITCH_MODE", "off")
    monkeypatch.setenv("INVESTIGATOR_MCP_PUBLIC_KEY", public_key)
    monkeypatch.setenv("INVESTIGATOR_MCP_ACTOR", "incident-commander")
    monkeypatch.setenv(
        "INVESTIGATOR_MCP_ACTOR_TOKEN",
        token(private_key, actor="incident-commander", tenant="local"),
    )

    run = run_investigation(
        question="Why is checkout failing?",
        cluster="demo-cluster",
        namespace="shop",
        service="checkout",
        deployment="checkout",
    )
    assert run["ok"] is True
    assert get_investigation(run["investigation_id"])["ok"] is True
    evidence_ids = [item["id"] for item in run["evidence"]]
    assert (
        verify_candidate(run["investigation_id"], "service_selector_mismatch", evidence_ids)["ok"]
        is True
    )
    assert source_status("kubernetes")["ok"] is True

    entries = read_audit(data_dir, "tool-audit", limit=10)
    assert [entry["tool"] for entry in entries] == ["run", "get", "verify", "source_status"]
    assert {entry["actor"] for entry in entries} == {"incident-commander"}
    assert {entry["outcome"] for entry in entries} == {"ok"}
    assert {entry["auth_mode"] for entry in entries} == {"token"}
    assert entries[0]["investigation_id"] == run["investigation_id"]
    assert entries[0]["trace_id"] == run["trace_id"]
    assert entries[0]["cluster"] == "demo-cluster"
    assert entries[0]["namespace"] == "shop"
    assert entries[0]["service"] == "checkout"
    assert entries[0]["question_sha256"]
    assert "question" not in entries[0]
    assert entries[2]["evidence_count"] == len(evidence_ids)
    assert entries[2]["passed"] is True

    ok, errors = verify_audit(data_dir, "tool-audit")
    assert ok is True
    assert errors == []


def test_cross_agent_read_keeps_each_signed_actor_distinct(monkeypatch, tmp_path):
    from kube_investigator.audit import read_audit
    from kube_investigator.mcp_server import get_investigation, run_investigation

    private_key, public_key = keypair()
    data_dir = tmp_path / "data"
    monkeypatch.setenv("INVESTIGATOR_DATA_DIR", str(data_dir))
    monkeypatch.setenv("INVESTIGATOR_BACKEND", "fixture")
    monkeypatch.setenv("INVESTIGATOR_SWITCH_MODE", "off")
    monkeypatch.setenv("INVESTIGATOR_MCP_PUBLIC_KEY", public_key)

    monkeypatch.setenv("INVESTIGATOR_MCP_ACTOR", "incident-commander")
    monkeypatch.setenv(
        "INVESTIGATOR_MCP_ACTOR_TOKEN",
        token(private_key, actor="incident-commander", tenant="local"),
    )
    run = run_investigation(
        question="Why is checkout failing?",
        cluster="demo-cluster",
        namespace="shop",
        service="checkout",
        deployment="checkout",
    )
    assert run["ok"] is True

    monkeypatch.setenv("INVESTIGATOR_MCP_ACTOR", "evidence-verifier")
    monkeypatch.setenv(
        "INVESTIGATOR_MCP_ACTOR_TOKEN",
        token(private_key, actor="evidence-verifier", tenant="local"),
    )
    assert get_investigation(run["investigation_id"])["ok"] is True

    entries = read_audit(data_dir, "tool-audit", limit=10)
    assert [(entry["tool"], entry["actor"]) for entry in entries] == [
        ("run", "incident-commander"),
        ("get", "evidence-verifier"),
    ]


def test_rejected_tool_calls_are_audited_as_unauthenticated(monkeypatch, tmp_path):
    from kube_investigator.audit import read_audit
    from kube_investigator.mcp_server import (
        get_investigation,
        run_investigation,
        source_status,
        verify_candidate,
    )

    private_key, public_key = keypair()
    data_dir = tmp_path / "data"
    monkeypatch.setenv("INVESTIGATOR_DATA_DIR", str(data_dir))
    monkeypatch.setenv("INVESTIGATOR_SWITCH_MODE", "off")
    monkeypatch.setenv("INVESTIGATOR_MCP_PUBLIC_KEY", public_key)
    monkeypatch.setenv("INVESTIGATOR_MCP_ACTOR", "incident-commander")
    monkeypatch.delenv("INVESTIGATOR_MCP_ACTOR_TOKEN", raising=False)

    run_investigation(
        question="Why is checkout failing?",
        cluster="demo-cluster",
        namespace="shop",
        service="checkout",
        deployment="checkout",
    )
    get_investigation("00000000-0000-0000-0000-000000000001")
    verify_candidate(
        "00000000-0000-0000-0000-000000000001",
        "service_selector_mismatch",
        [],
    )
    source_status("telemetry")

    entries = read_audit(data_dir, "tool-audit", limit=10)
    assert [entry["tool"] for entry in entries] == ["run", "get", "verify", "source_status"]
    assert {entry["actor"] for entry in entries} == {"unauthenticated"}
    assert {entry["outcome"] for entry in entries} == {"rejected"}
    assert {entry["error"] for entry in entries} == {"MCP actor token is required"}


def test_audit_failure_fails_the_tool_closed(monkeypatch, tmp_path):
    from kube_investigator import mcp_server

    monkeypatch.setenv("INVESTIGATOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INVESTIGATOR_BACKEND", "fixture")
    monkeypatch.setenv("INVESTIGATOR_SWITCH_MODE", "off")
    monkeypatch.setattr(mcp_server, "append_audit", lambda *args, **kwargs: False)

    result = mcp_server.source_status("kubernetes")
    assert result == {"ok": False, "error": "tool audit failed"}


def test_tool_audit_bounds_caller_supplied_metadata(monkeypatch, tmp_path):
    from kube_investigator.audit import read_audit
    from kube_investigator.mcp_server import get_investigation

    data_dir = tmp_path / "data"
    monkeypatch.setenv("INVESTIGATOR_DATA_DIR", str(data_dir))
    monkeypatch.setenv("INVESTIGATOR_SWITCH_MODE", "off")
    assert get_investigation("x" * 10_000)["ok"] is False
    entries = read_audit(data_dir, "tool-audit", limit=5)
    assert len(entries) == 1
    assert len(entries[0]["investigation_id"]) <= 201
    assert entries[0]["auth_mode"] == "dev"
