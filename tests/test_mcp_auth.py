import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kube_investigator.config import Settings
from kube_investigator.mcp_auth import authorize_actor, verify_actor_token
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
