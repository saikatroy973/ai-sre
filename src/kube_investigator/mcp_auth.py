"""Signed actor tokens for the local MCP server.

The MCP server is a stdio child of the agent process.  When
``INVESTIGATOR_MCP_PUBLIC_KEY`` is configured, every tool call except
``health`` must carry an Ed25519-signed token that names the actor and
tenant.  The private key stays with the operator; the MCP server only needs
the public key.  This is a credential check, not a replacement for
OS/process isolation.

Same-uid limit: agents that share one operating-system account can read each
other's mode-0600 token configs and, when it is stored under that account, the
shared signing key.  Tokens are also passed in the MCP child environment, so
``ps -ax -Eww`` exposes them to any same-user process.  A compromised same-uid
agent can therefore present another actor's token or mint a fresh one and
forge attribution; that account can also open the evidence DB or audit files
directly.  A signing service with an independent approval/identity check, or a
separate uid/container per agent, is required before actor names are a security
boundary.
"""

from __future__ import annotations

import base64
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from kube_investigator.config import Settings

# Tools that require a signed actor and participate in least-privilege checks.
# ``health`` is deliberately not here: it stays open as a liveness probe and
# exposes only non-sensitive data when unauthenticated.
AUTHENTICATED_TOOLS = frozenset({"run", "get", "verify", "memory", "source_status", "docs"})
ROLE_TOOL_GRANTS: dict[str, tuple[str, ...]] = {
    "incident-commander": ("run", "get", "verify", "memory", "source_status"),
    "cluster-investigator": ("docs", "get", "verify", "source_status"),
    "telemetry-analyst": ("get", "source_status"),
    "change-analyst": ("get", "source_status"),
    "memory-researcher": ("memory",),
    "evidence-verifier": ("get", "verify"),
}
_TOOL_PREFIX = "kube_investigator_"


def tools_for_actor(actor: str) -> list[str]:
    """Return the default least-privilege grants for a known actor role."""
    return list(ROLE_TOOL_GRANTS.get(actor, tuple(sorted(AUTHENTICATED_TOOLS))))


def canonical_tool_name(tool: str) -> str:
    """Accept either ``run`` or the exposed ``kube_investigator_run`` name."""
    value = tool.strip()
    if value.startswith(_TOOL_PREFIX):
        value = value[len(_TOOL_PREFIX) :]
    return value


def _normalized_tools(tools: list[str]) -> list[str]:
    normalized: set[str] = set()
    for item in tools:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("token tools claim is invalid")
        name = canonical_tool_name(item)
        if name not in AUTHENTICATED_TOOLS:
            raise ValueError("token tools claim names an unknown tool")
        normalized.add(name)
    return sorted(normalized)


def _configured_tools() -> frozenset[str] | None:
    """Optional server-side allowlist, used when a legacy token has no claim."""
    raw = (os.getenv("INVESTIGATOR_MCP_TOOLS") or "").strip()
    if not raw:
        return None
    return frozenset(canonical_tool_name(item) for item in raw.split(",") if item.strip())


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _parse_actor_token(token: str) -> tuple[dict[str, Any], bytes]:
    if len(token) > 8192:
        raise ValueError("token too large")
    payload_text, separator, signature_text = token.partition(".")
    if not separator or not payload_text or not signature_text:
        raise ValueError("invalid token format")
    payload_bytes = _decode(payload_text)
    _decode(signature_text)  # validate the signature encoding as well
    payload = json.loads(payload_bytes)
    if not isinstance(payload, dict):
        raise ValueError("invalid token payload")
    if not isinstance(payload.get("actor"), str) or not payload["actor"]:
        raise ValueError("token actor is missing")
    if not isinstance(payload.get("tenant"), str) or not payload["tenant"]:
        raise ValueError("token tenant is missing")
    if "tools" in payload:
        if not isinstance(payload["tools"], list):
            raise ValueError("token tools claim is invalid")
        payload["tools"] = _normalized_tools(payload["tools"])
    return payload, payload_bytes


def verify_actor_token(token: str, public_key: str) -> dict[str, Any]:
    """Verify a ``payload.signature`` token and return its payload."""
    payload, payload_bytes = _parse_actor_token(token)
    _payload_text, separator, signature_text = token.partition(".")
    assert separator
    key = Ed25519PublicKey.from_public_bytes(_decode(public_key))
    key.verify(_decode(signature_text), payload_bytes)
    expires = payload.get("exp")
    if not isinstance(expires, int) or expires <= int(time.time()):
        raise ValueError("token expired")
    return payload


def inspect_actor_token(
    token: str,
    public_key: str | None = None,
    *,
    now: int | None = None,
) -> dict[str, Any]:
    """Return safe token metadata for operator status without echoing the token."""
    now = int(time.time()) if now is None else now
    try:
        payload, payload_bytes = _parse_actor_token(token)
        signature_checked = False
        if public_key:
            _payload_text, separator, signature_text = token.partition(".")
            assert separator
            key = Ed25519PublicKey.from_public_bytes(_decode(public_key))
            key.verify(_decode(signature_text), payload_bytes)
            signature_checked = True
    except (ValueError, InvalidSignature, TypeError, json.JSONDecodeError) as exc:
        return {
            "valid": False,
            "signature_checked": False,
            "error": "token is invalid" if not isinstance(exc, ValueError) else str(exc),
        }
    expires = payload.get("exp")
    expired = not isinstance(expires, int) or expires <= now
    return {
        "valid": not expired and (signature_checked if public_key else True),
        "signature_checked": signature_checked,
        "actor": payload.get("actor"),
        "tenant": payload.get("tenant"),
        "tools": payload.get("tools"),
        "issued_at": _timestamp(payload.get("iat")),
        "expires_at": _timestamp(expires),
        "expires_in_seconds": (expires - now) if isinstance(expires, int) else None,
        "expired": expired,
    }


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _authenticate(settings: Settings) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Return ``(payload, actor, error)`` for the current MCP process."""
    configured_actor = (os.getenv("INVESTIGATOR_MCP_ACTOR") or "").strip()
    public_key = (os.getenv("INVESTIGATOR_MCP_PUBLIC_KEY") or "").strip()
    token = (os.getenv("INVESTIGATOR_MCP_ACTOR_TOKEN") or "").strip()
    if not public_key:
        if token:
            # A token without a public key would be silently ignored: refuse
            # instead of falling back to unauthenticated development mode.
            return None, None, "MCP public key is required when an actor token is configured"
        return None, configured_actor or "mcp", None
    if not token:
        return None, None, "MCP actor token is required"
    try:
        payload = verify_actor_token(token, public_key)
    except (ValueError, InvalidSignature, TypeError, json.JSONDecodeError):
        return None, None, "MCP actor token is invalid or expired"
    if payload["tenant"] != settings.tenant:
        return None, None, "MCP actor token is outside the configured tenant"
    if configured_actor and payload["actor"] != configured_actor:
        return None, None, "MCP actor does not match the signed token"
    return payload, payload["actor"], None


def authorize_actor(settings: Settings) -> tuple[str | None, str | None]:
    """Return ``(actor, error)`` for the current MCP process.

    With no public key configured, the server runs in local development mode
    and uses ``INVESTIGATOR_MCP_ACTOR`` (or ``mcp``).  With a public key, a
    valid signed token is mandatory.
    """
    _payload, actor, error = _authenticate(settings)
    return actor, error


def authorize_tool(settings: Settings, tool: str) -> tuple[str | None, str | None]:
    """Authenticate the actor and enforce the token's ``tools`` claim.

    A signed token may carry ``tools``; generated Console tokens always do.
    Legacy tokens without the claim fall back to the built-in role map for
    known actors, then the optional server-side ``INVESTIGATOR_MCP_TOOLS``
    allowlist.  When that allowlist is configured it also caps a token's
    ``tools`` claim (the effective grant is the intersection), so an operator
    can narrow every caller from the server side.  Unknown/custom actors
    remain fully authorized unless the allowlist is configured.
    """
    payload, actor, error = _authenticate(settings)
    if error is not None:
        return None, error
    name = canonical_tool_name(tool)
    configured = _configured_tools()
    if payload is not None and "tools" in payload:
        allowed = frozenset(payload["tools"])
        if configured is not None:
            allowed &= configured
    else:
        allowed = configured
        if payload is not None:
            # A signed legacy token is still capped by the built-in role map;
            # the server allowlist can narrow that grant, never widen it.
            role = frozenset(tools_for_actor(actor or ""))
            allowed = role if allowed is None else allowed & role
    if allowed is not None and name not in allowed:
        return actor, "MCP actor is not authorized for this tool"
    return actor, None


PRIVATE_KEY_NAME = "mcp-actor-private.pem"


def generate_actor_keypair(out_dir: Path) -> tuple[Path, str]:
    """Create or reuse the operator keypair; return ``(private_path, public_key)``.

    The directory is private; the PEM file is mode 0600 and is never printed
    or logged.  Re-running against the same directory reuses the existing
    key so tokens stay verifiable across setup runs.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Enforce the private modes on every run: a pre-existing key or directory
    # with looser permissions must not be silently reused.
    os.chmod(out_dir, 0o700)
    private_path = out_dir / PRIVATE_KEY_NAME
    if private_path.exists():
        key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError(f"{private_path} is not an Ed25519 private key")
    else:
        key = Ed25519PrivateKey.generate()
        private_path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    os.chmod(private_path, 0o600)
    public_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_path, base64.urlsafe_b64encode(public_raw).rstrip(b"=").decode()


def sign_actor_token(
    private_path: Path,
    actor: str,
    tenant: str,
    ttl: int,
    tools: list[str] | None = None,
) -> str:
    """Return an Ed25519-signed ``payload.signature`` actor token."""
    import time as _time

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if ttl < 1:
        raise ValueError("ttl must be positive")
    if not actor or not tenant:
        raise ValueError("actor and tenant are required")
    key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{private_path} is not an Ed25519 private key")
    now = int(_time.time())
    payload: dict[str, Any] = {"actor": actor, "tenant": tenant, "iat": now, "exp": now + ttl}
    if tools is not None:
        payload["tools"] = _normalized_tools(tools)
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = key.sign(payload_bytes)
    token_payload = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
    token_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{token_payload}.{token_signature}"
