"""Append-only, hash-chained JSONL audit logs.

The evidence database is deliberately opened read-only by the MCP get/verify
tools.  Audit records therefore live beside it in the writable data directory:
reading a staged evidence snapshot still produces a durable, actor-attributed
record.  Each entry carries a SHA-256 chain over the previous entry so a
modified existing entry or a break in the middle of the chain is detectable.

Honest limit: without an external anchor a hash chain cannot detect truncation
of the newest entries or deletion of the whole file, and any process that can
write the data directory can rewrite the chain or the pruning baseline.  Set
``INVESTIGATOR_AUDIT_ANCHOR_DIR`` to a directory outside the writable data
volume (a separate account or mount the server can still write) to record the
entry count, tail digest, and baseline split; verification then detects
truncation, deletion, and a rewritten baseline.  The anchor must be
replaceable on every append, so an immutable (WORM) path cannot be the live
anchor; publish anchors to WORM storage out-of-band instead.  If the anchor is
writable by the same attacker, it is still tamper-evident rather than
tamper-proof; deleting the anchor file resets the guarantee, so keep the
anchor root away from the data-dir writer.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
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


def _baseline_path(data_dir: Path, name: str) -> Path:
    return data_dir / f"{name}.baseline.json"


def _read_baseline(data_dir: Path, name: str) -> tuple[int, str]:
    try:
        payload = json.loads(_baseline_path(data_dir, name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0, ""
    if not isinstance(payload, dict):
        return 0, ""
    count = payload.get("pruned_entries")
    previous = payload.get("prev_sha256")
    retained = count if isinstance(count, int) and count >= 0 else 0
    prior = previous if isinstance(previous, str) else ""
    return retained, prior


def write_baseline(data_dir: Path, name: str, *, previous: str, pruned_entries: int) -> bool:
    """Record the chain position before intentionally pruning old archives."""
    try:
        path = _baseline_path(data_dir, name)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "name": name,
                    "prev_sha256": previous,
                    "pruned_entries": pruned_entries,
                    "updated_at": utc_now().isoformat(),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError:
        return False
    return True


def _chain_state(files: list[Path], name: str = "") -> tuple[int, str]:
    """Return ``(entry_count, last_sha256)`` for an optional external anchor."""
    count = 0
    last = ""
    if files and name:
        pruned, _previous = _read_baseline(files[0].parent, name)
        count = pruned
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        except UnicodeDecodeError as exc:
            raise OSError(f"could not read {path.name}") from exc
        for line in lines:
            if not line.strip():
                continue
            count += 1
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            value = entry.get("entry_sha256")
            if isinstance(value, str):
                last = value
    return count, last


def _anchor_path(data_dir: Path, name: str) -> Path | None:
    """Anchor file for one log, keyed by the data directory it describes.

    Two data directories (for example two tenants on one host) may share an
    anchor root without overwriting each other's chain position.
    """
    raw = (os.getenv("INVESTIGATOR_AUDIT_ANCHOR_DIR") or "").strip()
    if not raw:
        return None
    key = hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()[:12]
    return Path(raw).expanduser() / f"{name}.{key}.anchor.json"


def _read_anchor(data_dir: Path, name: str) -> tuple[int, str, int, str] | None:
    """Return the anchored ``(count, tail, pruned_count, pruned_tail)`` position.

    The anchor records the baseline split as well as the total tail so that
    rewriting ``*.baseline.json`` (for example to hide a deleted prefix)
    cannot pass verification: the anchor's baseline fields would disagree.
    """
    path = _anchor_path(data_dir, name)
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    count = payload.get("entry_count")
    last = payload.get("last_sha256")
    pruned = payload.get("pruned_entries")
    pruned_prev = payload.get("pruned_prev_sha256")
    for value in (count, pruned):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
    if not isinstance(last, str) or not isinstance(pruned_prev, str):
        return None
    return count, last, pruned, pruned_prev


@contextmanager
def _audit_lock(data_dir: Path, name: str) -> Iterator[None]:
    """Serialize append, rotation, and pruning for one audit log."""
    descriptor = os.open(
        data_dir / f"{name}.lock",
        os.O_APPEND | os.O_CREAT | os.O_RDWR,
        0o600,
    )
    with os.fdopen(descriptor, "a+", encoding="utf-8") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _write_anchor(
    path: Path,
    name: str,
    count: int,
    last: str,
    pruned_entries: int,
    pruned_prev: str,
) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "name": name,
                    "entry_count": count,
                    "last_sha256": last,
                    "pruned_entries": pruned_entries,
                    "pruned_prev_sha256": pruned_prev,
                    "updated_at": utc_now().isoformat(),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError:
        return False
    return True


def append_audit(data_dir: Path, name: str, body: dict[str, Any]) -> bool:
    """Append one metadata-only entry to ``name`` and return whether it persisted.

    ``body`` must contain only JSON-serializable, secret-free metadata.  The
    function takes an exclusive lock, rotates the current file when it reaches
    ``INVESTIGATOR_MCP_AUDIT_MAX_BYTES``, and never creates the evidence store.

    Returns ``False`` when the entry cannot be written, when the current file
    does not end on a complete line (a torn tail would swallow this entry), or
    when an existing anchor disagrees with the log's tail or baseline split.
    In that last case the process fails closed instead of re-anchoring the
    possibly truncated log; a missing anchor (first use) is initialized from
    the current chain.  The anchor update happens after the append, so a
    ``False`` result can still leave one durable entry behind.
    """
    try:
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        max_bytes = int(os.getenv("INVESTIGATOR_MCP_AUDIT_MAX_BYTES", "5000000"))
    except (OSError, ValueError):
        return False
    try:
        with _audit_lock(data_dir, name):
            files = audit_files(data_dir, name)
            previous = _last_hash(files)
            current = data_dir / f"{name}.jsonl"
            if not _ends_with_newline(current):
                return False
            anchor = _anchor_path(data_dir, name)
            anchored = _read_anchor(data_dir, name) if anchor is not None else None
            pruned_count, pruned_prev = _read_baseline(data_dir, name)
            if anchored is not None and (
                anchored[1] != previous or (anchored[2], anchored[3]) != (pruned_count, pruned_prev)
            ):
                # The anchor is authoritative: never re-anchor a mismatched log.
                return False
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
            if anchor is not None:
                # The steady state is incremental: both anchored positions are
                # consistent with the log, so the count only grows by one and
                # the history is never re-read.  A missing anchor (first use)
                # is built from the current chain.
                if anchored is not None:
                    written = _write_anchor(
                        anchor,
                        name,
                        anchored[0] + 1,
                        entry["entry_sha256"],
                        pruned_count,
                        pruned_prev,
                    )
                else:
                    count, last = _chain_state(audit_files(data_dir, name), name)
                    written = _write_anchor(anchor, name, count, last, pruned_count, pruned_prev)
                if not written:
                    return False
    except (OSError, TypeError, ValueError):
        return False
    return True


def _ends_with_newline(path: Path) -> bool:
    """Return False for a non-empty file whose last line was never completed."""
    try:
        if not path.exists() or path.stat().st_size == 0:
            return True
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) == b"\n"
    except OSError:
        return False


def read_audit(data_dir: Path, name: str, limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent entries from the named log."""
    entries: list[dict[str, Any]] = []
    for path in audit_files(data_dir, name):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
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
    """Check every entry hash, its link, and any configured external anchor.

    The anchor pins two positions: the log tail (count plus last hash) and the
    baseline split (cumulative pruned count plus the last pruned hash).  A
    forged ``*.baseline.json`` used to hide a deleted prefix therefore fails
    with an "anchor baseline mismatch".
    """
    errors: list[str] = []
    pruned_entries, previous = _read_baseline(data_dir, name)
    baseline_previous = previous
    entry_count = pruned_entries
    seen_entries = 0
    for path in audit_files(data_dir, name):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{path.name}: {type(exc).__name__}")
            continue
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            entry_count += 1
            seen_entries += 1
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
    if seen_entries == 0:
        errors.append("audit log is empty")
    anchor = _anchor_path(data_dir, name)
    if anchor is not None:
        anchored = _read_anchor(data_dir, name)
        if anchored is None:
            errors.append("external audit anchor is missing or invalid")
        else:
            anchored_count, anchored_tail, anchored_pruned, anchored_pruned_prev = anchored
            if anchored_count != entry_count:
                errors.append("external audit anchor entry count mismatch")
            if anchored_tail != previous:
                errors.append("external audit anchor tail hash mismatch")
            if (anchored_pruned, anchored_pruned_prev) != (pruned_entries, baseline_previous):
                errors.append("external audit anchor baseline mismatch")
    return not errors, errors


def _stale_archives(data_dir: Path, name: str, cutoff_timestamp: float) -> list[Path]:
    """Return the oldest contiguous rotated files older than the cutoff."""
    current = data_dir / f"{name}.jsonl"
    stale: list[Path] = []
    for path in audit_files(data_dir, name):
        if path == current:
            continue
        try:
            modified = path.stat().st_mtime
        except OSError:
            break
        if modified >= cutoff_timestamp:
            break
        stale.append(path)
    return stale


def _verify_prefix(files: list[Path], start_previous: str) -> tuple[bool, str, str, int]:
    """Verify one contiguous chain prefix before it is removed.

    Returns ``(ok, error, last_hash, entry_count)``.  Pruning an already
    tampered archive would erase the only evidence of that tampering, so
    retention refuses instead.
    """
    previous = start_previous
    count = 0
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            return False, f"{path.name}: {type(exc).__name__}", previous, count
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            count += 1
            try:
                entry = json.loads(line)
            except ValueError:
                return False, f"{path.name}:{line_number}: invalid JSON", previous, count
            if not isinstance(entry, dict):
                return False, f"{path.name}:{line_number}: invalid entry", previous, count
            stored = entry.pop("entry_sha256", None)
            if entry.get("prev_sha256") != previous:
                return False, f"{path.name}:{line_number}: chain break", previous, count
            serialized = json.dumps(entry, sort_keys=True, separators=(",", ":"))
            if stored != hashlib.sha256(serialized.encode()).hexdigest():
                return False, f"{path.name}:{line_number}: hash mismatch", previous, count
            if isinstance(stored, str):
                previous = stored
    return True, "", previous, count


def prune_archives(
    data_dir: Path,
    name: str,
    cutoff_timestamp: float,
    *,
    dry_run: bool = False,
) -> list[str]:
    """Remove the oldest contiguous rotated files and move the chain baseline.

    The current log is never removed, and the archive prefix is verified
    before anything is touched: pruning an already tampered chain would erase
    the evidence.  Stale archives are then renamed to ``*.pruning`` (atomic,
    reversible) before the baseline records exactly the removed prefix.  If a
    later delete fails, the chain still verifies because the baseline matches
    what is hidden; a failure before the baseline is written renames the files
    back.

    A crash between the renames and the baseline write can leave ``*.pruning``
    files hidden from verification; rename them back before verifying.  Files
    that cannot be unlinked after the baseline is written are reported as an
    error and are safe to remove manually.
    """
    if not data_dir.is_dir():
        return []
    if dry_run:
        # A dry run is read-only: no lock file, no baseline, no deletions.
        return [path.name for path in _stale_archives(data_dir, name, cutoff_timestamp)]
    with _audit_lock(data_dir, name):
        stale = _stale_archives(data_dir, name, cutoff_timestamp)
        if not stale:
            return []
        names = [path.name for path in stale]
        previous_count, baseline_previous = _read_baseline(data_dir, name)
        valid, error, last_hash, entry_count = _verify_prefix(stale, baseline_previous)
        if not valid:
            raise OSError(f"refusing to prune an invalid audit chain: {error}")
        hidden: list[tuple[Path, Path]] = []
        try:
            for path in stale:
                target = path.with_name(f"{path.name}.pruning")
                path.rename(target)
                hidden.append((path, target))
            if not write_baseline(
                data_dir,
                name,
                previous=last_hash or baseline_previous,
                pruned_entries=previous_count + entry_count,
            ):
                raise OSError("could not write the audit baseline")
        except BaseException:
            for original, target in hidden:
                if target.exists():
                    target.rename(original)
            raise
        leftover: list[str] = []
        for _original, target in hidden:
            try:
                target.unlink()
            except OSError:
                leftover.append(target.name)
        anchor = _anchor_path(data_dir, name)
        if anchor is not None:
            anchored_pruned, anchored_prev = _read_baseline(data_dir, name)
            count, last = _chain_state(audit_files(data_dir, name), name)
            if not _write_anchor(anchor, name, count, last, anchored_pruned, anchored_prev):
                raise OSError("could not update the external audit anchor")
        if leftover:
            raise OSError(
                "could not delete pruned archives (hidden from verification): "
                + ", ".join(leftover)
            )
        return names
