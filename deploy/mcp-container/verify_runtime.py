"""Fail-closed, in-container proof that the MCP runtime is really isolated.

Prints one ``PASS <name>`` or ``FAIL <name>: <reason>`` line per probe and
exits nonzero on any FAIL.  verify.sh (the host side) greps these lines and
is the gate; this file only produces the evidence.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import urllib.error
import urllib.request


def _probe(name: str, fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - probes deliberately report anything
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        return
    print(f"PASS {name}")


def probe_uid() -> None:
    if os.getuid() == 0:
        raise RuntimeError("running as root")
    if os.geteuid() != os.getuid():
        raise RuntimeError("euid differs from uid")


def probe_filesystem_is_read_only() -> None:
    for target in ("/app", "/usr/local/lib", "/etc", "/root"):
        probe_path = pathlib.Path(target) / "isolation-selftest"
        try:
            probe_path.write_text("x")
        except OSError:
            continue
        raise RuntimeError(f"writable program path: {target}")


def probe_no_network() -> None:
    for host, port in (("1.1.1.1", 443), ("10.0.0.1", 53)):
        try:
            socket.create_connection((host, port), timeout=2).close()
        except OSError:
            continue
        raise RuntimeError(f"opened a socket to {host}:{port}")
    try:
        urllib.request.urlopen("http://example.invalid/", timeout=2)  # noqa: S310 - bound, dead
    except (urllib.error.URLError, OSError):
        return
    raise RuntimeError("served an HTTP request unexpectedly")


def probe_mcp_stdio_handshake() -> None:
    """The packaged MCP server must start and answer over stdio.

    Dev-mode auth is permitted here (no public key) because identity makes no
    sense without a caller; probes for tokens live in mcp_token_probe.py.
    """
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "container-probe", "version": "0"},
        },
    }
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    list_tools = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    memory_call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "kube_investigator_memory",
            "arguments": {
                "cluster": "demo-cluster",
                "namespace": "shop",
                "query": "isolation-probe",
                "limit": 1,
            },
        },
    }
    env = dict(os.environ)
    env.setdefault("INVESTIGATOR_DATA_DIR", "/data")
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        ["python", "-m", "kube_investigator.mcp_server"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps(init) + "\n")
        process.stdin.flush()
        line = process.stdout.readline()
        if "result" not in line:
            raise RuntimeError(f"initialize rejected: {line.strip()[:200]}")
        process.stdin.write(json.dumps(initialized) + "\n")
        process.stdin.write(json.dumps(list_tools) + "\n")
        process.stdin.flush()
        listing = ""
        while True:
            line = process.stdout.readline()
            if not line:
                raise RuntimeError("server closed before tools/list")
            if '"id":2' in line or '"id": 2' in line:
                listing = line
                break
        tools = json.dumps(listing)
        for expected in ("kube_investigator_health", "kube_investigator_memory"):
            if expected not in tools:
                raise RuntimeError(f"missing tool {expected}")
        process.stdin.write(json.dumps(memory_call) + "\n")
        process.stdin.flush()
        call = ""
        while True:
            line = process.stdout.readline()
            if not line:
                raise RuntimeError("server closed before the memory call")
            if '"id":3' in line or '"id": 3' in line:
                call = line
                break
        try:
            message = json.loads(call)
            text = message["result"]["content"][0]["text"]
            payload = json.loads(text)
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise RuntimeError(f"memory call malformed: {call.strip()[:200]}") from exc
        if payload.get("ok") is not True:
            raise RuntimeError(f"memory call failed: {payload}")
    finally:
        process.stdin.close()
        process.terminate()


def main() -> int:
    failed: list[str] = []

    def run(name: str, fn) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - probes deliberately report anything
            failed.append(name)
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}")

    run("uid-isolation", probe_uid)
    run("filesystem-isolation", probe_filesystem_is_read_only)
    run("network-isolation", probe_no_network)
    run("mcp-stdio-handshake", probe_mcp_stdio_handshake)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
