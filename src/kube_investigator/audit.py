"""Append-only, hash-chained JSONL audit logs.

The evidence database is deliberately opened read-only by the MCP get/verify
tools.  Audit records therefore live beside it in the writable data directory:
reading a staged evidence snapshot still produces a durable, actor-attributed
record.  Each entry carries a SHA-256 chain over the previous entry so a
modified existing entry or a break in the middle of the chain is detectable.

Honest limit: a hash chain without an external anchor cannot detect truncation
of the newest entries or deletion of the whole file, and any process that can
write the data directory can rewrite the chain.  The chain is tamper-evident
against accidental edits, not tamper-proof against a same-uid attacker.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix fallback
    fcntl = None

from kube_investigator.models import utc_now


def audit_files(data_dir: Path, name: str) -> list[Path]:
    """Return archived and current log files in chain order."""
    archived = sorted(data_dir.glob(f"{name}.*.jsonl"))
    current = data_dir / f"{name}.jsonl"
    if current.exists():
        archived.append(current)
    return archived


def _last_hash(files: list[Path]) -> str:
    for path in reversed(files):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            value = entry.get("entry_sha256")
            if isinstance(value, str):
                return value
    return ""


def append_audit(data_dir: Path, name: str, body: dict[str, Any]) -> bool:
    """Append one metadata-only entry to ``name`` and return whether it persisted.

    ``body`` must contain only JSON-serializable, secret-free metadata.  The
    function takes an exclusive lock, rotates the current file when it reaches
    ``INVESTIGATOR_MCP_AUDIT_MAX_BYTES``, and never creates the evidence store.
    """
    try:
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        max_bytes = int(os.getenv("INVESTIGATOR_MCP_AUDIT_MAX_BYTES", "5000000"))
    except (OSError, ValueError):
        return False
    try:
        lock_descriptor = os.open(
            data_dir / f"{name}.lock",
            os.O_APPEND | os.O_CREAT | os.O_RDWR,
            0o600,
        )
        with os.fdopen(lock_descriptor, "a+", encoding="utf-8") as lock:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            files = audit_files(data_dir, name)
            previous = _last_hash(files)
            current = data_dir / f"{name}.jsonl"
            if max_bytes > 0 and current.exists() and current.stat().st_size >= max_bytes:
                archive = data_dir / (
                    f"{name}."
                    + utc_now().strftime("%Y%m%dT%H%M%S%fZ")
                    + "."
                    + secrets.token_hex(4)
                    + ".jsonl"
                )
                current.rename(archive)
            entry_body = {
                **body,
                "ts": utc_now().isoformat(),
                "prev_sha256": previous,
            }
            serialized = json.dumps(entry_body, sort_keys=True, separators=(",", ":"))
            entry = {
                **entry_body,
                "entry_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
            }
            descriptor = os.open(
                current,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except (OSError, TypeError, ValueError):
        return False
    return True


def read_audit(data_dir: Path, name: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent entries from the named log."""
    entries: list[dict[str, Any]] = []
    for path in audit_files(data_dir, name):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return entries[-max(1, limit) :]


def verify_audit(data_dir: Path, name: str) -> tuple[bool, list[str]]:
    """Check every entry hash and its link to the previous entry."""
    errors: list[str] = []
    previous = ""
    for path in audit_files(data_dir, name):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            errors.append(f"{path.name}: {type(exc).__name__}")
            continue
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                errors.append(f"{path.name}:{line_number}: invalid JSON")
                continue
            if not isinstance(entry, dict):
                errors.append(f"{path.name}:{line_number}: invalid entry")
                continue
            stored = entry.pop("entry_sha256", None)
            if entry.get("prev_sha256") != previous:
                errors.append(f"{path.name}:{line_number}: chain break")
            serialized = json.dumps(entry, sort_keys=True, separators=(",", ":"))
            expected = hashlib.sha256(serialized.encode()).hexdigest()
            if stored != expected:
                errors.append(f"{path.name}:{line_number}: hash mismatch")
            if isinstance(stored, str):
                previous = stored
    return not errors, errors
