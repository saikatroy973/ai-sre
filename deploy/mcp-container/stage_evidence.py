#!/usr/bin/env python3
"""Stage a read-only evidence snapshot and its integrity anchor.

The host-side staging step cuts a completed demo investigation into a
snapshot, records the snapshot SHA-256 in the container probe environment, and
refuses a missing report, a foreign principal, or malformed stored data.  The
in-container probe recomputes the digest before trusting the mount.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path


class StagingError(Exception):
    """A staging failure that must stop the isolation gate."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stage(source_path: Path, evidence_dir: Path) -> dict[str, str]:
    snapshot = evidence_dir / "investigator.sqlite3"
    if not source_path.is_file():
        raise StagingError(f"no evidence store at {source_path}")

    source = None
    target = None
    try:
        source = sqlite3.connect(f"file:{source_path.absolute()}?mode=ro", uri=True)
        target = sqlite3.connect(snapshot)
        source.backup(target)
        # A read-only SQLite connection cannot create the -shm file it needs on
        # an immutable mount, so convert the staged copy out of WAL mode.
        journal = str(target.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
        if journal != "delete":
            raise StagingError(f"staged snapshot stayed in journal mode {journal}")
        target.commit()
        row = source.execute(
            "SELECT id, state, record FROM investigations WHERE state = ? "
            "ORDER BY rowid DESC LIMIT 1",
            ("reported",),
        ).fetchone()
    except sqlite3.Error as exc:
        raise StagingError(f"could not stage the evidence database: {exc}") from exc
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()

    if row is None:
        raise StagingError(
            "the staging demo produced no reported investigation; "
            "the evidence probe refuses to guess"
        )
    try:
        record = json.loads(row[2])
    except (TypeError, ValueError) as exc:
        raise StagingError("the staged investigation record is corrupt") from exc
    if not isinstance(record, dict):
        raise StagingError("the staged investigation record is corrupt")

    scope = record.get("scope")
    report = record.get("report") or {}
    diagnosis = report.get("diagnosis") if isinstance(report, dict) else None
    principal = record.get("principal_id")
    if principal != "local-operator" or not diagnosis:
        raise StagingError(
            f"staged investigation has principal={principal!r} "
            f"diagnosis={diagnosis!r}; the probe refuses it"
        )
    if not isinstance(scope, dict):
        raise StagingError("the staged investigation has no scope")

    snapshot.chmod(0o444)
    digest = _sha256(snapshot)
    values = {
        "INVESTIGATOR_EVIDENCE_PROBE_ID": str(row[0]),
        "INVESTIGATOR_EVIDENCE_PROBE_DIAGNOSIS": str(diagnosis),
        "INVESTIGATOR_EVIDENCE_PROBE_SHA256": digest,
        "INVESTIGATOR_TENANT": str(scope["tenant"]),
        "INVESTIGATOR_CLUSTER": str(scope["cluster"]),
        "INVESTIGATOR_NAMESPACES": str(scope["namespace"]),
    }
    allowed = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ@._:-"
    for key, value in values.items():
        if not value or any(char not in allowed for char in value):
            raise StagingError(f"refusing unsafe staged env value for {key}")
    env_file = evidence_dir.parent / "probe.env"
    env_file.write_text("".join(f"{key}={value}\n" for key, value in sorted(values.items())))
    return values


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 2:
        print("usage: stage_evidence.py <source.sqlite3> <evidence-dir>", file=sys.stderr)
        return 2
    try:
        stage(Path(arguments[0]), Path(arguments[1]))
    except (StagingError, OSError, KeyError, ValueError) as exc:
        print(f"GATE FAILURE: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
