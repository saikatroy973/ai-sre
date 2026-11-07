#!/usr/bin/env python3
"""Live reliability drill for the Switch-native investigation system.

Parts (each part fails closed on any assertion):

``protocol``
    10 identical-risk queue jobs create 10 concurrent Switch incident rooms
    through K live worker processes. The labeled worker that currently holds
    a ``running`` job is SIGKILLed mid-drill; after that job's lease expires,
    a replacement worker (new label) recovers the orphaned row via its
    startup ``recover_expired()`` and finishes the work. Verifies
    exactly-once completion, unique room bindings, per-room canary
    isolation, and real lease-expiry recovery.

``verify-protocol``
    Re-runs only the strict assessment (``_assess``) against an existing
    drill data directory: exactly-once completion, unique room binding, and
    live per-room canary isolation. Read-only.

``switch-restart``
    Restarts the self-hosted Switch API container and verifies every room
    history that could be read before the restart survives byte-for-byte.

``console-agent``
    SIGKILLs one Switch Console-managed ``opencode serve`` runtime.  Console
    does not proactively respawn the runtime; the part waits up to 60s and,
    with ``--console-kickoff``, then creates one fresh incident room whose
    kickoff makes the Console re-instantiate the runtime.  Verifies registry
    agents and rooms are unchanged and a new runtime process exists.

``console-rooms N``
    Creates N Console incident rooms concurrently and verifies every room is
    registered, carries the incident kit package, and links only to the three
    core rooms.

Usage:
  python scripts/reliability_drill.py protocol [--rooms 10] [--workers 3] [--lease 20]
  python scripts/reliability_drill.py verify-protocol --data-dir <dir> [--rooms 10]
  python scripts/reliability_drill.py switch-restart
  python scripts/reliability_drill.py console-agent [--console-kickoff]
  python scripts/reliability_drill.py console-rooms --rooms 10

Run from the repository root; real services must be reachable.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
KUBE = REPO / ".venv" / "bin" / "ai-sre"
DRILL_TIMESTAMP = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
HISTORY_LIMIT = 100

QUESTION = "Why is checkout failing?"
SCOPE = {
    "cluster": "demo-cluster",
    "namespace": "shop",
    "service": "checkout",
    "deployment": "checkout",
}


def _fail(message: str) -> None:
    raise SystemExit(f"DRILL FAILURE: {message}")


def _ts() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


def _env(drill_data: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "AI_SRE_DATA_DIR": str(drill_data),
            "AI_SRE_BACKEND": "fixture",
            "AI_SRE_SCENARIO": "selector-mismatch",
            "AI_SRE_REASONER": "deterministic",
            "AI_SRE_SWITCH_MODE": "required",
            "AI_SRE_SWITCH_BASE_URL": "http://127.0.0.1:18000",
            "AI_SRE_SWITCH_STATE_DIR": str(REPO / ".data" / "switch"),
        }
    )
    return env


def _run(
    args: list[str],
    env: dict[str, str] | None = None,
    *,
    check: bool = True,
) -> str:
    result = subprocess.run(
        [str(KUBE), *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if check and result.returncode != 0:
        _fail(f"{args} exited {result.returncode}: {result.stderr.strip()[:400]}")
    return result.stdout


def _open_store(data_dir: Path):
    from ai_sre.storage import Store

    return Store(data_dir)


def _jobs(store) -> list[dict[str, Any]]:
    """Read queue rows straight from the drill store.

    Shelling out to ``queue list`` would rebuild Settings and the Switch
    coordinator on every poll; a direct read keeps monitoring cheap while
    workers run.
    """
    return store.queue_jobs()


def _tasks_summary(status: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for job in status:
        counts[job["status"]] = counts.get(job["status"], 0) + 1
    return " ".join(f"{key}={value}" for key, value in sorted(counts.items()))


def _spawn_worker(
    drill_data: Path,
    base_env: dict[str, str],
    label: str,
    log_dir: Path,
) -> subprocess.Popen:
    worker_env = dict(base_env)
    # The harness label ties queue rows to this worker while they run.
    worker_env["AI_SRE_QUEUE_WORKER_LABEL"] = label
    handle = subprocess.Popen(
        [str(KUBE), "queue", "run", "--data-dir", str(drill_data), "--json"],
        cwd=REPO,
        env=worker_env,
        stdout=(log_dir / f"{label}.log").open("ab"),
        stderr=subprocess.STDOUT,
    )
    print(f"[{_ts()}] worker {label} pid={handle.pid} started", flush=True)
    return handle


def _retry_ok(output: str) -> bool:
    try:
        return json.loads(output).get("status") == "pending"
    except ValueError:
        return False


def _wait_for(predicate, timeout: int, description: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(1.0)
    _fail(f"timeout waiting for {description}")


def part_protocol(rooms: int, workers: int, lease_seconds: int) -> dict[str, Any]:
    if rooms < 2 or workers < 2 or workers > 8:
        _fail("protocol drill needs rooms>=2 and workers in 2..8")
    if lease_seconds < 20:
        _fail("lease is seconds; keep >=20 so the crash leg can observe a real expiry")
    drill_data = Path(tempfile.mkdtemp(prefix=f"kube-drill-{DRILL_TIMESTAMP}-"))
    env = _env(drill_data)
    env["AI_SRE_QUEUE_LEASE_SECONDS"] = str(lease_seconds)
    print(f"[{_ts()}] data dir: {drill_data}", flush=True)
    drill_started = time.monotonic()

    for index in range(rooms):
        _run(
            [
                "queue",
                "add",
                "--question",
                QUESTION,
                "--cluster",
                SCOPE["cluster"],
                "--namespace",
                SCOPE["namespace"],
                "--service",
                SCOPE["service"],
                "--deployment",
                SCOPE["deployment"],
                "--data-dir",
                str(drill_data),
                "--json",
            ],
            check=False,
        )
        print(f"[{_ts()}] queued job {index + 1}/{rooms}", flush=True)
    store = _open_store(drill_data)
    pending = store.queue_jobs()
    if len(pending) != rooms:
        _fail(f"expected {rooms} pending jobs, saw {len(pending)}")
    print(f"[{_ts()}] queued {rooms} identical-risk jobs", flush=True)

    log_dir = drill_data / "worker-logs"
    log_dir.mkdir()
    handles: list[subprocess.Popen] = []
    for worker in range(workers):
        handles.append(_spawn_worker(drill_data, env, f"worker-{worker}", log_dir))
        time.sleep(0.7)

    # Crash leg: SIGKILL exactly the labeled worker that currently HOLDS a
    # live "running" row, so the crash is observable in the same query plan
    # the workers use.
    started = time.monotonic()
    victim_job: dict[str, Any] | None = None
    while time.monotonic() - started < 180:
        running = [
            row
            for row in _jobs(store)
            if row["status"] == "running" and str(row.get("worker_id") or "").startswith("worker-")
        ]
        if running:
            row = running[0]
            # Confirm the same job is still running before firing.
            again = [
                item
                for item in _jobs(store)
                if item["id"] == row["id"]
                and item["status"] == "running"
                and str(item.get("worker_id") or "").startswith("worker-")
            ]
            if again:
                victim_job = again[0]
                break
        time.sleep(1.0)
    if victim_job is None:
        _fail("no worker was caught holding a running job before the deadline")
    victim_label = str(victim_job["worker_id"])
    victim_index = int(victim_label.rsplit("-", 1)[1])
    victim_pid = handles[victim_index].pid
    os.kill(victim_pid, signal.SIGKILL)
    for handle in handles:
        if handle.pid == victim_pid:
            handles.remove(handle)
            break
    print(
        f"[{_ts()}] SIGKILLed {victim_label} (pid={victim_pid}) mid-drill",
        flush=True,
    )

    orphan = [
        row for row in _jobs(store) if row["id"] == victim_job["id"] and row["status"] == "running"
    ]
    if not orphan:
        _fail("victim's job did not survive as running; lease-recovery leg is void")
    lease_until = datetime.fromisoformat(str(orphan[0]["lease_until"]))
    print(
        f"[{_ts()}] orphan running row survives: job={victim_job['id']} "
        f"lease_until={lease_until.isoformat()}",
        flush=True,
    )

    # Wait for the orphan's lease to expire, then start a replacement worker
    # whose startup recover_expired() clears the row back to pending.
    overshoot = (lease_until - datetime.now(UTC)).total_seconds()
    if overshoot > -120:
        time.sleep(max(0.0, overshoot + 2.0))
    print(f"[{_ts()}] orphan lease has expired; starting the replacement", flush=True)
    handles.append(_spawn_worker(drill_data, env, "recoverer-0", log_dir))

    # The replacement must release the row from 'running': either it returns
    # to pending from recover_expired, or the replacement instantly re-claims
    # it under its own label.
    def orphan_progress() -> dict[str, Any] | None:
        row = next(
            (item for item in _jobs(store) if item["id"] == victim_job["id"]),
            None,
        )
        if row is None:
            return None
        if row["status"] != "running":
            return row
        if str(row.get("worker_id") or "") == "recoverer-0":
            return row
        return None

    released = _wait_for(orphan_progress, 120, "the replacement worker to clear the orphaned lease")
    print(
        f"[{_ts()}] orphan row released by recoverer-0: status={released['status']}",
        flush=True,
    )

    # Transient faults under load (rate-limited room creation, session
    # storms) are retried by the supervisor like a real operator would, with
    # backoff. The final gate stays strict: every job must end complete.
    transient_retries = 0
    settled = False
    for round_index in range(6):
        status = _jobs(store)
        failed = [row for row in status if row["status"] == "failed"]
        retried_now = 0
        for row in failed:
            result = _run(
                [
                    "queue",
                    "retry",
                    "--id",
                    row["id"],
                    "--data-dir",
                    str(drill_data),
                    "--json",
                ],
                check=False,
            )
            if _retry_ok(result):
                retried_now += 1
        if retried_now:
            transient_retries += retried_now
            print(f"[{_ts()}] requeued {retried_now} failed job(s)", flush=True)
        alive = [handle for handle in handles if handle.poll() is None]
        pending_now = [row for row in _jobs(store) if row["status"] == "pending"]
        if not alive and pending_now:
            handles.append(_spawn_worker(drill_data, env, f"supervisor-{len(handles)}", log_dir))
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status = _jobs(store)
            if not any(row["status"] in {"pending", "running"} for row in status):
                settled = True
                break
            time.sleep(2.0)
        print(
            f"[{_ts()}] round {round_index}: settle={settled} jobs={_tasks_summary(status)}",
            flush=True,
        )
        if settled:
            break
        if time.monotonic() - drill_started > 1500:
            _fail("drill exceeded its total time budget before settling")
    if not settled:
        _fail("workers could not settle the queue within the round budget")

    status = _jobs(store)
    attempts_used = sum(int(row.get("attempts") or 0) for row in status)
    final_row = next((row for row in status if row["id"] == victim_job["id"]), None)
    if final_row is None or final_row["status"] != "complete":
        _fail("the orphaned job did not complete after lease recovery")
    if int(final_row.get("attempts") or 0) < 2:
        _fail("the orphaned job was never claimed a second time; lease-expiry recovery is unproven")
    print(
        f"[{_ts()}] lease-expiry recovery proven: job={victim_job['id']} "
        f"attempts={final_row['attempts']} (first claim was SIGKILLed mid-flight)",
        flush=True,
    )
    print(
        f"[{_ts()}] all {rooms} jobs finished; transient_retries={transient_retries}; "
        f"attempts_used={attempts_used}",
        flush=True,
    )

    summary = _assess(drill_data, rooms)
    summary["transient_retries"] = transient_retries
    summary["attempts_used"] = attempts_used
    summary["crash_leg"] = {
        "victim_label": victim_label,
        "victim_pid": victim_pid,
        "orphan_job": victim_job["id"],
        "orphan_attempts": final_row["attempts"],
        "replacement_label": "recoverer-0",
    }
    print(f"[{_ts()}] protocol part OK", flush=True)
    return summary


def _assess(drill_data: Path, rooms: int) -> dict[str, Any]:
    """Strict, reusable gate: exactly-once completion, unique room bindings,
    and per-room canary isolation read back from the live rooms."""
    from uuid import UUID as _UUID

    from ai_sre.storage import Store

    store = Store(drill_data)
    status = store.queue_jobs()
    complete = [row for row in status if row["status"] == "complete"]
    if len(complete) != rooms:
        _fail(f"expected {rooms} complete jobs, saw {len(complete)}")
    investigation_ids = {row["investigation_id"] for row in complete}
    if len(investigation_ids) != rooms:
        _fail("a job completed without exactly one investigation")

    # Exactly-once room binding: one distinct live room per incident. The
    # message-text canary is the investigation's 32-hex trace id: every
    # handoff carries "trace_id: <32hex>", while the investigation UUID only
    # appears (unique) in the room name binding.
    room_ids: set[str] = set()
    canaries: dict[str, str] = {}
    for raw_id in sorted(investigation_ids):
        investigation = store.get(_UUID(str(raw_id)))
        room_id = str(getattr(investigation, "switch_room_id", None) or "")
        room_name = str(getattr(investigation, "switch_room_name", None) or "")
        if not room_id or not room_name:
            _fail(f"{raw_id} never bound a Switch room")
        if room_id in room_ids:
            _fail(f"two investigations shared room {room_id}")
        room_ids.add(room_id)
        canaries[room_id] = str(investigation.trace_id)
        if not room_name.startswith(f"incident-{str(raw_id)[:8]}"):
            _fail(f"room {room_id} is not bound to this drill investigation: {room_name}")
    if len(room_ids) != rooms:
        _fail("expected one distinct live room per incident")
    if len(set(canaries.values())) != rooms:
        _fail("drill canary traces are not distinct; isolation check would be void")

    # Live read-back: each room's own canary must be present and every other
    # drill room's canary absent.
    from ai_sre.config import Settings as DrillSettings
    from ai_sre.switch.runtime import build_switch_runtime

    settings = DrillSettings(
        data_dir=drill_data,
        backend="fixture",
        scenario="selector-mismatch",
        reasoner="deterministic",
        switch_mode="required",
        switch_base_url="http://127.0.0.1:18000",
        switch_state_dir=REPO / ".data" / "switch",
    )
    runtime = build_switch_runtime(settings)
    if runtime is None:
        _fail("switch runtime unavailable for the isolation read-back")
    histories: dict[str, list[dict[str, Any]]] = {}
    try:
        for room_id in sorted(room_ids):
            histories[room_id] = runtime.history("incident-commander", room_id, limit=100)
    finally:
        runtime.close()

    leaks: list[tuple[str, str, str]] = []
    for room_id, rows in histories.items():
        text = json.dumps(rows)
        own_canary = canaries[room_id]
        if own_canary not in text:
            leaks.append((room_id, own_canary, "missing own trace"))
        for other_canary in sorted(canaries.values()):
            if other_canary != own_canary and other_canary in text:
                leaks.append((room_id, other_canary, "foreign trace present"))
    if leaks:
        _fail(f"room history cross-contamination: {leaks[:4]}")
    message_counts = {room_id: len(rows) for room_id, rows in histories.items()}
    if min(message_counts.values()) < 4:
        _fail(f"a drill room has too little history: {message_counts}")

    return {
        "data_dir": str(drill_data),
        "rooms": len(room_ids),
        "message_counts": message_counts,
        "checked": [
            "exactly-once completion",
            "unique room binding",
            "per-room trace-canary isolation",
            "live room history >= 4 messages",
        ],
    }


def part_switch_restart(data_dir: Path | None) -> dict[str, Any]:
    from ai_sre.config import Settings as DrillSettings
    from ai_sre.switch.runtime import build_switch_runtime

    settings = DrillSettings(
        data_dir=data_dir or Path(os.getenv("AI_SRE_DATA_DIR", ".data")),
        backend="fixture",
        scenario="selector-mismatch",
        reasoner="deterministic",
        switch_mode="required",
        switch_base_url="http://127.0.0.1:18000",
        switch_state_dir=REPO / ".data" / "switch",
    )
    runtime = build_switch_runtime(settings)
    if runtime is None:
        _fail("switch runtime unavailable")
    rooms = _run(["switch", "status", "--state-dir", settings.switch_state_dir, "--json"])
    room_ids = json.loads(rooms)["rooms"]
    if not room_ids:
        _fail("no core rooms found before the restart")
    incident_rooms: dict[str, list[dict[str, Any]]] = {}
    try:
        # Every core room whose history can be read is part of the
        # byte-for-byte contract, not a single sample.  The window must not
        # saturate, or "preserved" would silently mean "the newest 100".
        for room_id in room_ids.values():
            try:
                incident_rooms[room_id] = runtime.history(
                    "incident-commander", room_id, limit=HISTORY_LIMIT
                )
            except Exception:
                continue
        saturated = [
            room_id for room_id, rows in incident_rooms.items() if len(rows) >= HISTORY_LIMIT
        ]
        if saturated:
            _fail(
                f"history window saturated at {HISTORY_LIMIT} events for {saturated[:3]}; "
                "the drill cannot claim a full byte-for-byte comparison"
            )
        candidates = [rid for rid, rows in incident_rooms.items() if rows]
        if not candidates:
            _fail("could not read any room history before the restart")

        container = subprocess.run(
            ["docker", "ps", "--format", "{{.ID}} {{.Ports}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        target = None
        for line in container.stdout.splitlines():
            if "18000" in line:
                target = line.split(" ")[0]
                break
        if target is None:
            _fail("could not find the Switch API container on port 18000")
        subprocess.run(["docker", "restart", target], check=True, timeout=120)
        print(f"[{_ts()}] restarted Switch API container {target}", flush=True)

        def healthy() -> bool:
            output = _run(
                ["switch", "status", "--state-dir", settings.switch_state_dir, "--json"],
                check=False,
            )
            try:
                return bool(json.loads(output).get("healthy"))
            except ValueError:
                return False

        _wait_for(healthy, 120, "Switch API healthy after restart")
        for room_id, before_rows in incident_rooms.items():
            after_rows = runtime.history("incident-commander", room_id, limit=HISTORY_LIMIT)
            if json.dumps(after_rows, sort_keys=True) != json.dumps(before_rows, sort_keys=True):
                _fail(f"history changed across the Switch API restart: {room_id}")
        print(
            f"[{_ts()}] switch-restart part OK: {len(incident_rooms)} histories read, "
            "all preserved verbatim across the restart",
            flush=True,
        )
        return {
            "rooms_compared": len(incident_rooms),
            "rooms_seen": len(incident_rooms),
        }
    finally:
        runtime.close()


def _console_client():
    from ai_sre.switch.console import ConsoleSettings

    settings = ConsoleSettings.from_env(required=True)
    return settings


def _console_status_payload():
    from ai_sre.switch.console import ConsoleClient

    settings = _console_client()
    client = ConsoleClient(settings)
    try:
        agents = client.agents()
        rooms = client.rooms()
        return {"agents": agents, "rooms": rooms}
    finally:
        client.close()


def _console_serve_pids() -> list[tuple[int, int]]:
    """(pid, ppid) for every live opencode serve process and its sidecar parent."""
    listing = subprocess.run(
        ["ps", "-ax", "-o", "pid,ppid,command"], capture_output=True, text=True
    )
    pairs = []
    for line in listing.stdout.splitlines():
        if "opencode serve" not in line or "awk" in line:
            continue
        parts = line.split()
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        pairs.append((pid, ppid))
    return pairs


def _kickoff_console_incident() -> str | None:
    output = _run(
        [
            "switch",
            "console",
            "incident",
            "--cluster",
            SCOPE["cluster"],
            "--namespace",
            SCOPE["namespace"],
            "--service",
            SCOPE["service"],
            "--deployment",
            SCOPE["deployment"],
            "--scenario",
            "selector-mismatch",
            "--json",
        ]
    )
    return json.loads(output).get("room_id")


def part_console_agent(console_kickoff: bool) -> dict[str, Any]:
    pairs = _console_serve_pids()
    if not pairs:
        _fail("no Console-managed opencode serve process found to kill")
    before = _console_status_payload()
    agent_count_before = len(before["agents"])
    room_count_before = len(before["rooms"])
    before_pids = {pid for pid, _ in pairs}

    victim, victim_ppid = pairs[0]
    os.kill(victim, signal.SIGKILL)
    print(
        f"[{_ts()}] SIGKILLed Console-managed opencode serve pid={victim} (ppid={victim_ppid})",
        flush=True,
    )
    time.sleep(2)

    def respawn_check() -> bool:
        alive_now = {pid for pid, _ in _console_serve_pids()}
        # A surviving unrelated runtime does not count as a respawn: the
        # Console must have spawned a replacement process after the kill.
        return victim not in alive_now and bool(alive_now - before_pids)

    respawned_on_time = True
    kickoff_note: str | None = None
    try:
        _wait_for(respawn_check, 60, "Console to respawn the agent runtime on its own")
    except SystemExit:
        respawned_on_time = False
        if not console_kickoff:
            _fail(
                "Console did not respawn the runtime within 60s; rerun with "
                "--console-kickoff to force a kickoff-driven restart"
            )
        print("no automatic respawn; forcing a fresh Console incident room")
        kickoff_note = _kickoff_console_incident()
        _wait_for(respawn_check, 180, "Console to respawn the runtime after a kickoff")
        print(f"[{_ts()}] respawned after manual kickoff room {kickoff_note}", flush=True)

    after = _console_status_payload()
    if len(after["agents"]) != agent_count_before:
        _fail("Console agent registry changed across the agent restart")
    if len(after["rooms"]) < room_count_before:
        _fail("Console rooms lost across the agent restart")
    if all(pid == victim for pid, _ in _console_serve_pids()):
        _fail("the killed runtime is still the only opencode serve process")

    kickoff: str | None = None
    if console_kickoff and respawned_on_time:
        kickoff = _kickoff_console_incident()
        print(f"[{_ts()}] kickoff room (informational): {kickoff}", flush=True)
    elif console_kickoff:
        kickoff = kickoff_note

    print(
        f"[{_ts()}] console-agent part OK: agents={len(after['agents'])} "
        f"rooms={len(after['rooms'])} (was {agent_count_before}/{room_count_before})",
        flush=True,
    )
    return {
        "killed_pid": victim,
        "agents_before": agent_count_before,
        "agents_after": len(after["agents"]),
        "rooms_before": room_count_before,
        "rooms_after": len(after["rooms"]),
        "kickoff_room": kickoff,
    }


def part_console_rooms(rooms: int) -> dict[str, Any]:
    from ai_sre.switch.console import ConsoleClient
    from ai_sre.switch.specs import CORE_ROOMS, INCIDENT_KIT_NAME

    def create(_index: int) -> str:
        return str(_kickoff_console_incident())

    with ThreadPoolExecutor(max_workers=4) as pool:
        room_ids = list(pool.map(create, range(rooms)))
    if len(set(room_ids)) != rooms:
        _fail("Console returned duplicate room ids for concurrent incidents")
    after = _console_status_payload()
    registered = {str(room.get("id") or room.get("room_id")) for room in after["rooms"]}
    missing_rooms = [room_id for room_id in room_ids if room_id not in registered]
    if missing_rooms:
        _fail(f"Console did not register the incident rooms: {missing_rooms[:3]}")

    # Pack/link isolation is verified per room through the live Console API.
    client = ConsoleClient(_console_client())
    try:
        core_ids = {
            str(item.get("name") or item.get("room_name")): str(
                item.get("id") or item.get("room_id")
            )
            for item in client.rooms()
        }
        expected_targets = {
            name: core_ids[name]
            for name in (CORE_ROOMS["memory"], CORE_ROOMS["change"], CORE_ROOMS["governance"])
        }
        unresolved = [name for name, room_id in expected_targets.items() if not room_id]
        if unresolved:
            _fail(f"core rooms missing before link verification: {unresolved}")
        kit_ids = {
            str(item.get("id"))
            for item in client.packages()
            if str(item.get("name")) == INCIDENT_KIT_NAME
        }
        if not kit_ids:
            _fail(f"Console has no {INCIDENT_KIT_NAME} package to verify attachments for")
        for room_id in room_ids:
            packages = client.room_packages(room_id)
            attached_ids = {str(item.get("id")) for item in packages}
            attached_names = {
                str(item.get("name") or item.get("package_name") or "") for item in packages
            }
            names_sorted = sorted(name for name in attached_names if name) or sorted(attached_ids)
            if not (attached_ids & kit_ids or INCIDENT_KIT_NAME in attached_names):
                _fail(
                    f"incident room {room_id} is missing the {INCIDENT_KIT_NAME} package "
                    f"(attached: {names_sorted})"
                )
            targets = {str(item.get("target_room_id")) for item in client.linked_rooms(room_id)}
            if not targets.issubset(expected_targets.values()):
                _fail(
                    f"incident room {room_id} links outside the core rooms: "
                    f"{sorted(targets - expected_targets.values())}"
                )
            if targets != set(expected_targets.values()):
                _fail(
                    f"incident room {room_id} does not link all three core rooms: "
                    f"missing={sorted(set(expected_targets.values()) - targets)}"
                )
    finally:
        client.close()
    print(
        f"[{_ts()}] console-rooms part OK: {rooms} concurrent rooms created with the "
        f"{INCIDENT_KIT_NAME} package and core-only links (registry total {len(registered)})",
        flush=True,
    )
    return {"room_ids": room_ids, "registry_total": len(registered)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "part",
        choices=(
            "protocol",
            "verify-protocol",
            "switch-restart",
            "console-agent",
            "console-rooms",
        ),
    )
    parser.add_argument("--rooms", type=int, default=10)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--lease", type=int, default=20)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--console-kickoff", action="store_true")
    args = parser.parse_args()

    if args.part == "protocol":
        result = part_protocol(args.rooms, args.workers, args.lease)
    elif args.part == "verify-protocol":
        if args.data_dir is None:
            _fail("verify-protocol requires --data-dir")
        result = _assess(args.data_dir, args.rooms)
    elif args.part == "switch-restart":
        result = part_switch_restart(args.data_dir)
    elif args.part == "console-agent":
        result = part_console_agent(args.console_kickoff)
    else:
        result = part_console_rooms(args.rooms)
    print(json.dumps(result, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
