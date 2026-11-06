"""Fail-closed, in-container proof that the MCP runtime is really isolated.

Prints one ``PASS <name>`` or ``FAIL <name>: <reason>`` line per probe and
exits nonzero on any FAIL.  verify.sh (the host side) greps these lines and
is the gate; this file only produces the evidence.
"""

from __future__ import annotations

import errno
import hashlib
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
        base = pathlib.Path(target)
        # A missing path must not count as read-only: the probe would pass
        # vacuously if the image layout changed.
        if not base.is_dir():
            raise RuntimeError(f"probe path is missing: {target}")
        try:
            (base / "isolation-selftest").write_text("x")
        except PermissionError:
            continue
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                continue
            raise RuntimeError(f"unexpected error writing {target}: {exc}") from exc
        raise RuntimeError(f"writable program path: {target}")


def probe_no_network() -> None:
    for host, port in (("1.1.1.1", 443), ("10.0.0.1", 53)):
        try:
            socket.create_connection((host, port), timeout=2).close()
        except TimeoutError as exc:
            # Dropped packets mean a filtered network, not an absent one.
            raise RuntimeError(f"connect to {host}:{port} timed out (filtered)") from exc
        except OSError:
            continue
        raise RuntimeError(f"opened a socket to {host}:{port}")
    try:
        urllib.request.urlopen("http://example.invalid/", timeout=2)  # noqa: S310 - bound, dead
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise RuntimeError("HTTP probe timed out (filtered network)") from exc
        return
    except OSError:
        return
    raise RuntimeError("served an HTTP request unexpectedly")


def _spawn_server(env: dict[str, str]):
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        ["python", "-m", "kube_investigator.mcp_server"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None and process.stdout is not None
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "container-probe", "version": "0"},
        },
    }
    process.stdin.write(json.dumps(initialize) + "\n")
    process.stdin.flush()
    response = _read_for_id(process, 1, "initialize")
    if "result" not in response:
        raise RuntimeError(f"initialize rejected: {response.strip()[:200]}")
    process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    process.stdin.write("\n")
    process.stdin.flush()
    return process


def _read_for_id(process, request_id: int, context: str) -> str:
    assert process.stdout is not None
    while True:
        line = process.stdout.readline()
        if not line:
            raise RuntimeError(f"server closed before {context}")
        if f'"id":{request_id}' in line or f'"id": {request_id}' in line:
            return line


def _call_tool(process, request_id: int, name: str, arguments: dict) -> dict:
    """Send one tools/call and return the parsed JSON payload it printed."""
    process.stdin.write(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        + "\n"
    )
    process.stdin.flush()
    line = _read_for_id(process, request_id, f"the {name} call")
    try:
        message = json.loads(line)
        return json.loads(message["result"]["content"][0]["text"])
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise RuntimeError(f"{name} payload malformed: {line.strip()[:200]}") from exc


def probe_mcp_stdio_handshake() -> None:
    """The packaged MCP server must start and answer over stdio.

    Dev-mode auth is permitted here (no public key) because identity makes no
    sense without a caller; probes for tokens live in mcp_token_probe.py.
    """
    list_tools = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    env = dict(os.environ)
    env.setdefault("INVESTIGATOR_DATA_DIR", "/data")
    process = _spawn_server(env)
    try:
        process.stdin.write(json.dumps(list_tools) + "\n")
        process.stdin.flush()
        listing = _read_for_id(process, 2, "tools/list")
        tools = json.dumps(listing)
        for expected in ("kube_investigator_health", "kube_investigator_memory"):
            if expected not in tools:
                raise RuntimeError(f"missing tool {expected}")
        payload = _call_tool(
            process,
            3,
            "kube_investigator_memory",
            {
                "cluster": "demo-cluster",
                "namespace": "shop",
                "query": "isolation-probe",
                "limit": 1,
            },
        )
        if payload.get("ok") is not True:
            raise RuntimeError(f"memory call failed: {payload}")
    finally:
        process.stdin.close()
        process.terminate()


def probe_evidence_store() -> None:
    """get/verify must serve a real staged investigation from the ro mount.

    The snapshot in /evidence was cut by verify.sh from a completed demo
    investigation. The MCP child cannot create the store, write to the
    mount, or talk to a cluster; it must still answer get and verify.
    """
    investigation_id = (os.environ.get("INVESTIGATOR_EVIDENCE_PROBE_ID") or "").strip()
    diagnosis = (os.environ.get("INVESTIGATOR_EVIDENCE_PROBE_DIAGNOSIS") or "").strip()
    expected_sha256 = (os.environ.get("INVESTIGATOR_EVIDENCE_PROBE_SHA256") or "").strip()
    if not investigation_id or not diagnosis or not expected_sha256:
        raise RuntimeError("staged evidence env is missing (id, diagnosis, or digest)")
    snapshot = pathlib.Path("/evidence/investigator.sqlite3")
    if not snapshot.is_file():
        raise RuntimeError("/evidence exposes no investigator.sqlite3 snapshot")
    digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    if digest != expected_sha256:
        raise RuntimeError("staged evidence snapshot digest does not match probe.env")
    try:
        (pathlib.Path("/evidence") / "isolation-selftest").write_text("x")
    except OSError:
        pass
    else:
        raise RuntimeError("the evidence mount is writable; isolation is broken")

    env = dict(os.environ)
    env["INVESTIGATOR_DATA_DIR"] = "/data"
    env["INVESTIGATOR_EVIDENCE_STORE_DIR"] = "/evidence"
    process = _spawn_server(env)
    try:
        record = _call_tool(
            process,
            2,
            "kube_investigator_get",
            {"investigation_id": investigation_id},
        )
        if record.get("ok") is not True:
            raise RuntimeError(f"get failed on staged evidence: {record}")
        if record.get("state") != "reported":
            raise RuntimeError(f"staged investigation is not complete: {record.get('state')}")
        if not record.get("report"):
            raise RuntimeError("staged investigation carries no report")
        evidence_ids = [str(item.get("id")) for item in record.get("evidence") or []]
        if not evidence_ids:
            raise RuntimeError("staged investigation carries no evidence")
        verification = _call_tool(
            process,
            3,
            "kube_investigator_verify",
            {
                "investigation_id": investigation_id,
                "diagnosis": diagnosis,
                "evidence_ids": evidence_ids,
            },
        )
        if verification.get("ok") is not True:
            raise RuntimeError(f"verify failed on staged evidence: {verification}")
        if verification.get("verification", {}).get("passed") is not True:
            raise RuntimeError(f"staged evidence did not re-verify: {verification}")
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
    run("evidence-store", probe_evidence_store)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
