#!/usr/bin/env python3
"""Generate Ed25519 keys and signed MCP actor tokens."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def keygen(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_key = Ed25519PrivateKey.generate()
    private_path = out_dir / "mcp-actor-private.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(private_path, 0o600)
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    print(f"private_key={private_path}")
    print(f"public_key={_b64(public_raw)}")
    return 0


def sign(private_path: Path, actor: str, tenant: str, ttl: int) -> int:
    if ttl < 1:
        print("ttl must be positive", file=sys.stderr)
        return 1
    if not actor or not tenant:
        print("actor and tenant are required", file=sys.stderr)
        return 1
    private_key = serialization.load_pem_private_key(
        private_path.read_bytes(),
        password=None,
    )
    if not isinstance(private_key, Ed25519PrivateKey):
        print("private key is not Ed25519", file=sys.stderr)
        return 1
    now = int(time.time())
    payload = {"actor": actor, "tenant": tenant, "iat": now, "exp": now + ttl}
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    token = f"{_b64(payload_bytes)}.{_b64(private_key.sign(payload_bytes))}"
    print(token)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    keygen_parser = commands.add_parser("keygen", help="create an Ed25519 keypair")
    keygen_parser.add_argument("--out-dir", type=Path, required=True)
    sign_parser = commands.add_parser("sign", help="sign an actor token")
    sign_parser.add_argument("--private-key", type=Path, required=True)
    sign_parser.add_argument("--actor", required=True)
    sign_parser.add_argument("--tenant", required=True)
    sign_parser.add_argument("--ttl", type=int, default=3600)
    args = parser.parse_args()
    if args.command == "keygen":
        return keygen(args.out_dir)
    if args.command == "sign":
        return sign(args.private_key, args.actor, args.tenant, args.ttl)
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
