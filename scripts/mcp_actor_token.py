#!/usr/bin/env python3
"""Generate Ed25519 keys and signed MCP actor tokens.

These tokens authenticate an MCP caller and may carry a least-privilege
``tools`` claim. They are not a same-uid isolation boundary: agents running as
one OS user can read each other's token configs, token-bearing child
environments, and the signing key, so a compromised agent can mint or replay
another actor's token. Use a signing service with independent approval/identity
checks, or a separate uid/container per agent, before treating actor names as
identity.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ai_sre.mcp_auth import generate_actor_keypair, sign_actor_token


def keygen(out_dir: Path) -> int:
    try:
        private_path, public_key = generate_actor_keypair(out_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"private_key={private_path}")
    print(f"public_key={public_key}")
    return 0


def sign(private_path: Path, actor: str, tenant: str, ttl: int, tools: list[str] | None) -> int:
    try:
        token = sign_actor_token(private_path, actor, tenant, ttl, tools=tools)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
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
    sign_parser.add_argument(
        "--tools",
        default=None,
        help=(
            "comma-separated least-privilege tool claim "
            "(run,get,verify,memory,source_status); an empty string denies every tool"
        ),
    )
    args = parser.parse_args()
    if args.command == "keygen":
        return keygen(args.out_dir)
    if args.command == "sign":
        tools = (
            None
            if args.tools is None
            else [item.strip() for item in args.tools.split(",") if item.strip()]
        )
        return sign(args.private_key, args.actor, args.tenant, args.ttl, tools)
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
