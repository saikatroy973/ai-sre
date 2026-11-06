"""Retention and space-reclamation for local investigation data."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from kube_investigator.audit import prune_archives
from kube_investigator.models import utc_now
from kube_investigator.storage import Store

AUDIT_PREFIXES = ("memory-audit", "tool-audit")
TERMINAL_STATES = frozenset({"reported", "failed"})


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def prune(
    data_dir: Path,
    *,
    investigation_days: int = 90,
    audit_days: int = 30,
    dry_run: bool = False,
    vacuum: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Delete old investigations, finished queue rows, and rotated audit files.

    The current ``memory-audit.jsonl``/``tool-audit.jsonl`` is never removed;
    only archived, rotated files older than ``audit_days`` are pruned.  A
    missing evidence store is not created, so ``--dry-run`` has no side
    effects, and queue rows plus their investigations are deleted in one
    locked transaction.  Only terminal investigations (``reported`` or
    ``failed``) are removed, so retention cannot delete work in flight.
    """
    if not 0 <= investigation_days <= 36500 or not 0 <= audit_days <= 36500:
        raise ValueError("retention days must be between 0 and 36500")
    current = now or utc_now()
    investigation_cutoff = current - timedelta(days=investigation_days)
    audit_cutoff = (current - timedelta(days=audit_days)).timestamp()
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "investigation_days": investigation_days,
        "audit_days": audit_days,
        "investigations": [],
        "queue_jobs": [],
        "audit_files": {},
        "vacuumed": False,
    }
    store = Store(data_dir) if (data_dir / "investigator.sqlite3").is_file() else None
    stale_investigations: list[str] = []
    stale_jobs: list[str] = []
    if store is not None:
        with store.connection() as connection:
            rows = connection.execute("SELECT id, record FROM investigations").fetchall()
            for investigation_id, raw in rows:
                try:
                    record = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("state") not in TERMINAL_STATES:
                    # Work in flight (or a stuck record) is never pruned: a
                    # running worker may still transition this investigation.
                    continue
                updated = _timestamp(record.get("updated_at") or record.get("created_at"))
                if updated is not None and updated.tzinfo is None:
                    updated = updated.replace(tzinfo=current.tzinfo)
                if updated is not None and updated < investigation_cutoff:
                    stale_investigations.append(str(investigation_id))

            queue_rows = connection.execute(
                "SELECT id, updated_at FROM investigation_queue "
                "WHERE status IN ('complete', 'failed')"
            ).fetchall()
            for job_id, updated_at in queue_rows:
                updated = _timestamp(updated_at)
                if updated is not None and updated.tzinfo is None:
                    updated = updated.replace(tzinfo=current.tzinfo)
                if updated is not None and updated < investigation_cutoff:
                    stale_jobs.append(str(job_id))
        if not dry_run:
            store.delete_investigations(stale_investigations, queue_job_ids=stale_jobs)
    report["investigations"] = stale_investigations
    report["queue_jobs"] = stale_jobs

    for prefix in AUDIT_PREFIXES:
        try:
            report["audit_files"][prefix] = prune_archives(
                data_dir, prefix, audit_cutoff, dry_run=dry_run
            )
        except OSError as exc:
            report["audit_files"][prefix] = []
            report.setdefault("errors", []).append(f"{prefix}: {exc}")

    leftovers = sorted(
        str(path) for prefix in AUDIT_PREFIXES for path in data_dir.glob(f"{prefix}.*.pruning")
    )
    if leftovers:
        report.setdefault("warnings", []).append(
            "hidden .pruning archives remain; verify or remove them manually: "
            + ", ".join(leftovers)
        )

    if vacuum and not dry_run and store is not None:
        connection = sqlite3.connect(store.path)
        try:
            connection.execute("VACUUM")
        finally:
            connection.close()
        report["vacuumed"] = True
    return report
