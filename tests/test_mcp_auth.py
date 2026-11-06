import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kube_investigator.config import Settings
from kube_investigator.mcp_auth import (
    authorize_actor,
    authorize_tool,
    inspect_actor_token,
    verify_actor_token,
)
from kube_investigator.mcp_server import search_memory


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def keypair():
    private_key = Ed25519PrivateKey.generate()
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_key, b64(public_raw)


def token(private_key, *, actor="memory-researcher", tenant="local", ttl=3600, tools=None):
    now = int(time.time())
    payload = {"actor": actor, "tenant": tenant, "iat": now, "exp": now + ttl}
    if tools is not None:
        payload["tools"] = tools
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


def test_token_tools_claim_enforces_least_privilege(monkeypatch, tmp_path):
    private_key, public_key = keypair()
    settings = Settings(tenant="local", data_dir=tmp_path)
    monkeypatch.setenv("INVESTIGATOR_MCP_PUBLIC_KEY", public_key)
    monkeypatch.setenv("INVESTIGATOR_MCP_ACTOR", "memory-researcher")
    monkeypatch.setenv(
        "INVESTIGATOR_MCP_ACTOR_TOKEN",
        token(private_key, actor="memory-researcher", tools=["memory"]),
    )

    actor, error = authorize_tool(settings, "memory")
    assert (actor, error) == ("memory-researcher", None)
    actor, error = authorize_tool(settings, "run")
    assert actor == "memory-researcher"
    assert error == "MCP actor is not authorized for this tool"
    actor, error = authorize_tool(settings, "kube_investigator_verify")
    assert error == "MCP actor is not authorized for this tool"


def test_known_role_map_restricts_legacy_tokens_without_a_claim(monkeypatch, tmp_path):
    private_key, public_key = keypair()
    settings = Settings(tenant="local", data_dir=tmp_path)
    monkeypatch.setenv("INVESTIGATOR_MCP_PUBLIC_KEY", public_key)
    monkeypatch.setenv("INVESTIGATOR_MCP_ACTOR", "memory-researcher")
    monkeypatch.setenv(
        "INVESTIGATOR_MCP_ACTOR_TOKEN",
        token(private_key, actor="memory-researcher"),
    )
    assert authorize_tool(settings, "memory") == ("memory-researcher", None)
    assert authorize_tool(settings, "run")[1] == ("MCP actor is not authorized for this tool")


def test_server_side_tool_allowlist_restricts_legacy_tokens(monkeypatch, tmp_path):
    private_key, public_key = keypair()
    settings = Settings(tenant="local", data_dir=tmp_path)
    monkeypatch.setenv("INVESTIGATOR_MCP_PUBLIC_KEY", public_key)
    monkeypatch.setenv("INVESTIGATOR_MCP_ACTOR", "memory-researcher")
    monkeypatch.setenv(
        "INVESTIGATOR_MCP_ACTOR_TOKEN",
        token(private_key, actor="memory-researcher"),
    )
    monkeypatch.setenv("INVESTIGATOR_MCP_TOOLS", "memory,get")

    assert authorize_tool(settings, "memory") == ("memory-researcher", None)
    assert authorize_tool(settings, "run")[1] == ("MCP actor is not authorized for this tool")


def test_inspect_actor_token_reports_expiry_and_tools():
    private_key, public_key = keypair()
    token_value = token(private_key, ttl=3600, tools=["get", "verify"])
    status = inspect_actor_token(token_value, public_key)
    assert status["valid"] is True
    assert status["signature_checked"] is True
    assert status["actor"] == "memory-researcher"
    assert status["tools"] == ["get", "verify"]
    assert status["expires_at"]
    assert 0 < status["expires_in_seconds"] <= 3600

    expired = inspect_actor_token(token(private_key, ttl=1), public_key, now=int(time.time()) + 5)
    assert expired["valid"] is False
    assert expired["expired"] is True


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

    # Without a token at all, health stays open but is reduced to liveness.
    from kube_investigator.mcp_server import kube_investigator_health

    monkeypatch.delenv("INVESTIGATOR_MCP_ACTOR_TOKEN", raising=False)
    health = kube_investigator_health()
    assert health["ok"] is True
    assert health["authenticated"] is False
    assert "tenant" not in health
    assert "cluster" not in health
    assert "namespaces" not in health

    monkeypatch.setenv(
        "INVESTIGATOR_MCP_ACTOR_TOKEN",
        token(private_key, actor="incident-commander", tenant="local"),
    )
    detailed = kube_investigator_health()
    assert detailed["authenticated"] is True
    assert detailed["tenant"] == "local"
    assert detailed["cluster"] == "demo-cluster"

    # The MCP transport path still answers liveness without a token.
    import asyncio

    from kube_investigator.mcp_server import mcp

    monkeypatch.delenv("INVESTIGATOR_MCP_ACTOR_TOKEN", raising=False)
    protocol = asyncio.run(mcp.call_tool("kube_investigator_health", {}))
    assert "ok" in json.dumps(protocol, default=str)


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


def test_memory_researcher_token_cannot_run_investigation(monkeypatch, tmp_path):
    from kube_investigator.mcp_server import run_investigation

    private_key, public_key = keypair()
    monkeypatch.setenv("INVESTIGATOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INVESTIGATOR_BACKEND", "fixture")
    monkeypatch.setenv("INVESTIGATOR_SWITCH_MODE", "off")
    monkeypatch.setenv("INVESTIGATOR_MCP_PUBLIC_KEY", public_key)
    monkeypatch.setenv("INVESTIGATOR_MCP_ACTOR", "memory-researcher")
    monkeypatch.setenv(
        "INVESTIGATOR_MCP_ACTOR_TOKEN",
        token(private_key, actor="memory-researcher", tools=["memory"]),
    )
    result = run_investigation(
        question="Why is checkout failing?",
        cluster="demo-cluster",
        namespace="shop",
        service="checkout",
        deployment="checkout",
    )
    assert result == {
        "ok": False,
        "error": "MCP actor is not authorized for this tool",
    }


def test_server_allowlist_caps_a_signed_tools_claim(monkeypatch, tmp_path):
    private_key, public_key = keypair()
    settings = Settings(tenant="local", data_dir=tmp_path)
    monkeypatch.setenv("INVESTIGATOR_MCP_PUBLIC_KEY", public_key)
    monkeypatch.setenv("INVESTIGATOR_MCP_ACTOR", "incident-commander")
    monkeypatch.setenv(
        "INVESTIGATOR_MCP_ACTOR_TOKEN",
        token(private_key, actor="incident-commander", tools=["run", "get"]),
    )
    monkeypatch.setenv("INVESTIGATOR_MCP_TOOLS", "get")

    assert authorize_tool(settings, "get") == ("incident-commander", None)
    actor, error = authorize_tool(settings, "run")
    assert actor == "incident-commander"
    assert error == "MCP actor is not authorized for this tool"


def test_actor_token_without_a_public_key_fails_closed(monkeypatch, tmp_path):
    settings = Settings(tenant="local", data_dir=tmp_path)
    monkeypatch.setenv("INVESTIGATOR_MCP_ACTOR", "telemetry-analyst")
    monkeypatch.setenv("INVESTIGATOR_MCP_ACTOR_TOKEN", "payload.signature")
    monkeypatch.delenv("INVESTIGATOR_MCP_PUBLIC_KEY", raising=False)

    actor, error = authorize_tool(settings, "run")
    assert actor is None
    assert error == "MCP public key is required when an actor token is configured"


def test_inspect_actor_token_never_claims_an_unverified_signature():
    private_key, _public_key = keypair()
    token_value = token(private_key, tools=["get"])
    status = inspect_actor_token(token_value, "not-a-valid-public-key")
    assert status["valid"] is False
    assert status["signature_checked"] is False
    assert status["error"]


def test_empty_tools_claim_denies_every_tool(monkeypatch, tmp_path):
    private_key, public_key = keypair()
    settings = Settings(tenant="local", data_dir=tmp_path)
    monkeypatch.setenv("INVESTIGATOR_MCP_PUBLIC_KEY", public_key)
    monkeypatch.setenv("INVESTIGATOR_MCP_ACTOR", "memory-researcher")
    monkeypatch.setenv(
        "INVESTIGATOR_MCP_ACTOR_TOKEN",
        token(private_key, actor="memory-researcher", tools=[]),
    )

    assert authorize_tool(settings, "memory")[1] == "MCP actor is not authorized for this tool"
    assert authorize_tool(settings, "health")[1] == "MCP actor is not authorized for this tool"
