"""Signed actor tokens for the local MCP server.

The MCP server is a stdio child of the agent process.  When
``INVESTIGATOR_MCP_PUBLIC_KEY`` is configured, every memory read must carry an
Ed25519-signed token that names the actor and tenant.  The private key stays
with the operator; the MCP server only needs the public key.  This is a
credential check, not a replacement for OS/process isolation.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from kube_investigator.config import Settings


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def verify_actor_token(token: str, public_key: str) -> dict[str, Any]:
    """Verify a ``payload.signature`` token and return its payload."""
    if len(token) > 8192:
        raise ValueError("token too large")
    payload_text, separator, signature_text = token.partition(".")
    if not separator or not payload_text or not signature_text:
        raise ValueError("invalid token format")
    payload_bytes = _decode(payload_text)
    signature = _decode(signature_text)
    key = Ed25519PublicKey.from_public_bytes(_decode(public_key))
    key.verify(signature, payload_bytes)
    payload = json.loads(payload_bytes)
    if not isinstance(payload, dict):
        raise ValueError("invalid token payload")
    if not isinstance(payload.get("actor"), str) or not payload["actor"]:
        raise ValueError("token actor is missing")
    if not isinstance(payload.get("tenant"), str) or not payload["tenant"]:
        raise ValueError("token tenant is missing")
    expires = payload.get("exp")
    if not isinstance(expires, int) or expires <= int(time.time()):
        raise ValueError("token expired")
    return payload


def authorize_actor(settings: Settings) -> tuple[str | None, str | None]:
    """Return ``(actor, error)`` for the current MCP process.

    With no public key configured, the server runs in local development mode
    and uses ``INVESTIGATOR_MCP_ACTOR`` (or ``mcp``).  With a public key, a
    valid signed token is mandatory.
    """
    configured_actor = (os.getenv("INVESTIGATOR_MCP_ACTOR") or "").strip()
    public_key = (os.getenv("INVESTIGATOR_MCP_PUBLIC_KEY") or "").strip()
    if not public_key:
        return configured_actor or "mcp", None
    token = (os.getenv("INVESTIGATOR_MCP_ACTOR_TOKEN") or "").strip()
    if not token:
        return None, "MCP actor token is required"
    try:
        payload = verify_actor_token(token, public_key)
    except (ValueError, InvalidSignature, TypeError, json.JSONDecodeError):
        return None, "MCP actor token is invalid or expired"
    if payload["tenant"] != settings.tenant:
        return None, "MCP actor token is outside the configured tenant"
    if configured_actor and payload["actor"] != configured_actor:
        return None, "MCP actor does not match the signed token"
    return payload["actor"], None


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


def sign_actor_token(private_path: Path, actor: str, tenant: str, ttl: int) -> str:
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
    payload = {"actor": actor, "tenant": tenant, "iat": now, "exp": now + ttl}
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = key.sign(payload_bytes)
    token_payload = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
    token_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{token_payload}.{token_signature}"
