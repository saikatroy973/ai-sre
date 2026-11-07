#!/usr/bin/env python3
"""Probe a Console agent's MCP configuration over a real stdio spawn.

Reads a per-agent ``opencode.json`` written by
``ai-sre switch console setup --actor-tokens``, spawns the actual
``ai_sre.mcp_server`` as the agent runtime would, and proves the
actor-token contract by calling ``ai_sre_memory``:

``ok``      a valid token is accepted and the search succeeds;
``missing`` no token configured -> the call fails closed;
``foreign`` another agent's token with a mismatched actor label -> rejected.

Usage:
  python scripts/mcp_token_probe.py --config .data/console-agents/<agent>/opencode.json --expect ok
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

INIT_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "ai-sre-probe", "version": "0"},
    },
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
MEMORY_CALL = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {
        "name": "ai_sre_memory",
        "arguments": {
            "cluster": "demo-cluster",
            "namespace": "shop",
            "query": "probe",
            "limit": 1,
        },
    },
}


def _read_response(pipe, wanted_id: int) -> dict:
    """Read JSON-RPC messages until the response with ``wanted_id`` arrives."""
    while True:
        line = pipe.readline()
        if not line:
            raise SystemExit("MCP server closed stdout before responding")
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        if message.get("id") != wanted_id:
            continue
        return message


def run(config_path: Path, expect: str, *, process_env: dict[str, str]) -> dict:
    payload = json.loads(config_path.read_text())
    server = payload.get("mcp", {}).get("ai-sre")
    if not server or server.get("type") != "local":
        raise SystemExit(f"{config_path} does not configure a local ai-sre server")
    command = list(server.get("command") or [])
    if not command:
        raise SystemExit(f"{config_path} does not carry an MCP command")

    env = dict(os.environ)
    env.update(process_env or {})
    process = subprocess.Popen(
        command,
        cwd=str(config_path.parent),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps(INIT_REQUEST) + "\n")
        process.stdin.flush()
        init_response = _read_response(process.stdout, 1)
        if "result" not in init_response:
            raise SystemExit(f"MCP initialize rejected: {init_response}")
        process.stdin.write(json.dumps(INITIALIZED) + "\n")
        process.stdin.write(json.dumps(MEMORY_CALL) + "\n")
        process.stdin.flush()
        memory_response = _read_response(process.stdout, 2)
        payload = memory_response.get("result") or {}
        structured = payload.get("structuredContent")
        if isinstance(structured, dict) and isinstance(structured.get("result"), dict):
            structured = structured["result"]
        if not isinstance(structured, dict):
            content = payload.get("content") or []
            text = content[0].get("text", "") if content else ""
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = {}
            if isinstance(parsed, dict) and isinstance(parsed.get("result"), dict):
                parsed = parsed["result"]
            structured = parsed if isinstance(parsed, dict) else {}
        if not structured:
            raise SystemExit(f"memory call returned no parseable payload: {memory_response}")
        ok = structured.get("ok")
        error_text = str(structured.get("error") or "")
        if expect == "ok" and ok is not True:
            raise SystemExit(f"probe expected ok, got: {structured}")
        if expect == "missing" and (ok is not False or "required" not in error_text):
            raise SystemExit(f"probe expected fail-closed (missing), got: {structured}")
        if expect == "foreign" and (ok is not False or "match the signed token" not in error_text):
            raise SystemExit(f"probe expected fail-closed (foreign), got: {structured}")
        return {"expect": expect, "ok": structured.get("ok"), "error": error_text}
    finally:
        process.stdin.close()
        process.terminate()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--expect", choices=("ok", "missing", "foreign"), required=True)
    parser.add_argument(
        "--override-actor",
        default=None,
        help="Attach this actor label while keeping the config's token (foreign probe)",
    )
    parser.add_argument(
        "--drop-token",
        action="store_true",
        help="Remove the token from the environment (missing probe)",
    )
    args = parser.parse_args()

    from ai_sre.switch.agent_config import load_config_environment

    environment = load_config_environment(args.config)
    if args.drop_token:
        environment.pop("AI_SRE_MCP_ACTOR_TOKEN", None)
    if args.override_actor:
        environment["AI_SRE_MCP_ACTOR"] = args.override_actor
    run(args.config, args.expect, process_env=environment)
    print(f"probe OK: {args.config} expect={args.expect}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
