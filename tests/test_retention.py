"""Retention pruning for investigations, queue rows, and rotated audit files."""

import json
import os
from datetime import timedelta

from ai_sre.auth import Principal
from ai_sre.collectors import FixtureCollector
from ai_sre.config import Settings
from ai_sre.models import Investigation, InvestigationRequest, Scope, utc_now
from ai_sre.retention import prune
from ai_sre.storage import Store


def _old_investigation(settings, *, days=400, state="reported"):
    request = InvestigationRequest(
        question="Why is checkout failing?",
        cluster="demo-cluster",
        namespace="shop",
        service="checkout",
        deployment="checkout",
    )
    principal = Principal.operator(settings)
    investigation = Investigation(
        principal_id=principal.id,
        scope=Scope(tenant="local", cluster="demo-cluster", namespace="shop"),
        request=request,
        state=state,
        updated_at=utc_now() - timedelta(days=days),
    )
    store = Store(settings.data_dir)
    store.create(investigation)
    return store, investigation


def test_prune_removes_old_investigations_queue_rows_and_audit_archives(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", switch_mode="off")
    store, investigation = _old_investigation(settings)
    for evidence in FixtureCollector(settings).collect(investigation):
        store.append_evidence(evidence)
    request = investigation.request
    job_id = store.enqueue(request)
    with store.connection() as connection:
        connection.execute(
            "UPDATE investigation_queue SET status = 'complete', updated_at = ? WHERE id = ?",
            ((utc_now() - timedelta(days=400)).isoformat(), str(job_id)),
        )

    from ai_sre.audit import append_audit

    assert append_audit(settings.data_dir, "tool-audit", {"tool": "get"})
    archive = settings.data_dir / "tool-audit.20260101T000000000000Z.dead.jsonl"
    (settings.data_dir / "tool-audit.jsonl").rename(archive)
    assert append_audit(settings.data_dir, "tool-audit", {"tool": "verify"})
    current = settings.data_dir / "tool-audit.jsonl"
    old = (utc_now() - timedelta(days=400)).timestamp()
    os.utime(archive, (old, old))

    dry_run = prune(settings.data_dir, investigation_days=90, audit_days=30, dry_run=True)
    assert str(investigation.id) in dry_run["investigations"]
    assert str(job_id) in dry_run["queue_jobs"]
    assert archive.name in dry_run["audit_files"]["tool-audit"]
    assert store.get(investigation.id).id == investigation.id
    assert archive.exists()

    report = prune(settings.data_dir, investigation_days=90, audit_days=30)
    assert report["investigations"] == [str(investigation.id)]
    assert report["queue_jobs"] == [str(job_id)]
    assert archive.name in report["audit_files"]["tool-audit"]
    assert not archive.exists()
    assert current.exists()
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM investigations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM investigation_queue").fetchone()[0] == 0


def test_prune_cli_reports_dry_run(tmp_path, capsys):
    from ai_sre.cli import main

    settings = Settings(data_dir=tmp_path / "data", switch_mode="off")
    _old_investigation(settings)
    assert (
        main(
            [
                "maintenance",
                "prune",
                "--data-dir",
                str(settings.data_dir),
                "--investigation-days",
                "30",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert len(report["investigations"]) == 1


def test_pruning_audit_archives_preserves_chain_verification(tmp_path):
    from ai_sre.audit import append_audit, verify_audit

    data_dir = tmp_path / "data"
    assert append_audit(data_dir, "tool-audit", {"tool": "get", "actor": "a"})
    archive = data_dir / "tool-audit.20260101T000000000000Z.dead.jsonl"
    (data_dir / "tool-audit.jsonl").rename(archive)
    assert append_audit(data_dir, "tool-audit", {"tool": "verify", "actor": "a"})
    old = (utc_now() - timedelta(days=400)).timestamp()
    os.utime(archive, (old, old))

    report = prune(data_dir, investigation_days=90, audit_days=30)
    assert archive.name in report["audit_files"]["tool-audit"]
    assert not archive.exists()
    ok, errors = verify_audit(data_dir, "tool-audit")
    assert ok is True, errors


def test_pruning_audit_archives_updates_external_anchor(tmp_path, monkeypatch):
    from ai_sre.audit import append_audit, verify_audit

    data_dir = tmp_path / "data"
    anchor_dir = tmp_path / "anchor"
    monkeypatch.setenv("AI_SRE_AUDIT_ANCHOR_DIR", str(anchor_dir))
    assert append_audit(data_dir, "tool-audit", {"tool": "get", "actor": "a"})
    archive = data_dir / "tool-audit.20260101T000000000000Z.dead.jsonl"
    (data_dir / "tool-audit.jsonl").rename(archive)
    assert append_audit(data_dir, "tool-audit", {"tool": "verify", "actor": "a"})
    old = (utc_now() - timedelta(days=400)).timestamp()
    os.utime(archive, (old, old))

    prune(data_dir, investigation_days=90, audit_days=30)
    assert not archive.exists()
    ok, errors = verify_audit(data_dir, "tool-audit")
    assert ok is True, errors


def test_prune_dry_run_does_not_create_the_store(tmp_path):
    data_dir = tmp_path / "missing"
    report = prune(data_dir, dry_run=True)
    assert report["investigations"] == []
    assert report["queue_jobs"] == []
    assert not (data_dir / "investigator.sqlite3").exists()


def test_prune_rejects_negative_days_from_the_cli(tmp_path, capsys):
    from ai_sre.cli import main

    assert (
        main(
            [
                "maintenance",
                "prune",
                "--data-dir",
                str(tmp_path / "data"),
                "--investigation-days",
                "-1",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "retention days must be between 0 and 36500" in captured.err
    assert "Traceback" not in captured.err


def test_prune_keeps_pending_queue_rows(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", switch_mode="off")
    store, investigation = _old_investigation(settings)
    request = investigation.request
    pending = store.enqueue(request)
    complete = store.enqueue(request)
    with store.connection() as connection:
        connection.execute(
            "UPDATE investigation_queue SET status = 'complete', updated_at = ? WHERE id = ?",
            ((utc_now() - timedelta(days=400)).isoformat(), str(complete)),
        )

    report = prune(settings.data_dir, investigation_days=90)
    assert report["queue_jobs"] == [str(complete)]
    with store.connection() as connection:
        remaining = [row[0] for row in connection.execute("SELECT id FROM investigation_queue")]
    assert remaining == [str(pending)]


def test_anchor_tracks_rotation_and_is_rebuilt_when_missing(tmp_path, monkeypatch):
    from ai_sre.audit import append_audit, verify_audit

    data_dir = tmp_path / "data"
    anchor_dir = tmp_path / "anchor"
    monkeypatch.setenv("AI_SRE_MCP_AUDIT_MAX_BYTES", "1")
    monkeypatch.setenv("AI_SRE_AUDIT_ANCHOR_DIR", str(anchor_dir))
    for index in range(4):
        assert append_audit(data_dir, "tool-audit", {"tool": "get", "n": index})

    def anchor_file():
        return next(anchor_dir.glob("tool-audit.*.anchor.json"))

    anchor = json.loads(anchor_file().read_text())
    assert anchor["entry_count"] == 4
    ok, errors = verify_audit(data_dir, "tool-audit")
    assert ok is True, errors

    anchor_file().unlink()
    assert append_audit(data_dir, "tool-audit", {"tool": "verify"})
    assert json.loads(anchor_file().read_text())["entry_count"] == 5
    ok, errors = verify_audit(data_dir, "tool-audit")
    assert ok is True, errors


def test_prune_keeps_work_in_flight(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", switch_mode="off")
    store, investigation = _old_investigation(settings, state="verifying")
    job = store.enqueue(investigation.request)
    with store.connection() as connection:
        connection.execute(
            "UPDATE investigation_queue SET investigation_id = ?, status = 'running' WHERE id = ?",
            (str(investigation.id), str(job)),
        )

    report = prune(settings.data_dir, investigation_days=90)
    assert report["investigations"] == []
    assert store.get(investigation.id).id == investigation.id


def test_prune_rejects_excessive_days(tmp_path, capsys):
    from ai_sre.cli import main

    assert (
        main(
            [
                "maintenance",
                "prune",
                "--data-dir",
                str(tmp_path / "data"),
                "--audit-days",
                "1000000000",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "retention days must be between 0 and 36500" in captured.err
    assert "Traceback" not in captured.err


def test_prune_reports_a_locked_store(tmp_path, capsys):
    import sqlite3

    from ai_sre.cli import main

    settings = Settings(data_dir=tmp_path / "data", switch_mode="off")
    store, _investigation = _old_investigation(settings)
    blocker = sqlite3.connect(store.path, timeout=30)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        code = main(["maintenance", "prune", "--data-dir", str(settings.data_dir)])
    finally:
        blocker.rollback()
        blocker.close()
    captured = capsys.readouterr()
    assert code == 1
    assert "database is locked" in captured.err
    assert "Traceback" not in captured.err


def test_prune_failure_keeps_the_remaining_chain_verifiable(tmp_path, monkeypatch):
    import pathlib

    from ai_sre.audit import append_audit, verify_audit

    data_dir = tmp_path / "data"
    monkeypatch.setenv("AI_SRE_MCP_AUDIT_MAX_BYTES", "1")
    for index in range(4):
        assert append_audit(data_dir, "tool-audit", {"tool": f"a{index}"})
    archives = sorted(data_dir.glob("tool-audit.*.jsonl"))
    old = (utc_now() - timedelta(days=400)).timestamp()
    for archive in archives:
        os.utime(archive, (old, old))

    blocked = archives[1].name + ".pruning"
    real_unlink = pathlib.Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self.name == blocked:
            raise OSError("simulated delete failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", flaky_unlink)
    report = prune(data_dir, investigation_days=90, audit_days=30)
    assert report["errors"] and "hidden from verification" in report["errors"][0]
    ok, errors = verify_audit(data_dir, "tool-audit")
    assert ok is True, errors


def test_anchor_files_are_keyed_by_data_directory(tmp_path, monkeypatch):
    from ai_sre.audit import append_audit, verify_audit

    anchor_dir = tmp_path / "anchor"
    monkeypatch.setenv("AI_SRE_AUDIT_ANCHOR_DIR", str(anchor_dir))
    first = tmp_path / "tenant-a" / "data"
    second = tmp_path / "tenant-b" / "data"

    assert append_audit(first, "tool-audit", {"tool": "get", "tenant": "a"})
    assert append_audit(second, "tool-audit", {"tool": "get", "tenant": "b"})

    assert verify_audit(first, "tool-audit")[0] is True
    assert verify_audit(second, "tool-audit")[0] is True
    assert len(list(anchor_dir.glob("tool-audit.*.anchor.json"))) == 2


def test_verify_detects_a_forged_baseline_hiding_prefix_deletion(tmp_path, monkeypatch):
    from ai_sre.audit import append_audit, verify_audit

    data_dir = tmp_path / "data"
    monkeypatch.setenv("AI_SRE_AUDIT_ANCHOR_DIR", str(tmp_path / "anchor"))
    for index in range(5):
        assert append_audit(data_dir, "tool-audit", {"tool": f"t{index}"})

    log = data_dir / "tool-audit.jsonl"
    lines = log.read_text().splitlines()
    deleted_tail = json.loads(lines[1])["entry_sha256"]
    log.write_text("\n".join(lines[2:]) + "\n")
    (data_dir / "tool-audit.baseline.json").write_text(
        json.dumps({"name": "tool-audit", "prev_sha256": deleted_tail, "pruned_entries": 2})
    )

    ok, errors = verify_audit(data_dir, "tool-audit")
    assert ok is False
    assert "external audit anchor baseline mismatch" in errors


def test_prune_refuses_a_tampered_archive(tmp_path, monkeypatch):
    from ai_sre.audit import append_audit, verify_audit

    data_dir = tmp_path / "data"
    monkeypatch.setenv("AI_SRE_MCP_AUDIT_MAX_BYTES", "1")
    for index in range(3):
        assert append_audit(data_dir, "tool-audit", {"tool": f"a{index}"})
    archive = sorted(data_dir.glob("tool-audit.*.jsonl"))[0]
    entry = json.loads(archive.read_text().strip())
    entry["tool"] = "tampered"
    archive.write_text(json.dumps(entry) + "\n")
    old = (utc_now() - timedelta(days=400)).timestamp()
    os.utime(archive, (old, old))

    report = prune(data_dir, investigation_days=90, audit_days=30)
    assert report["errors"]
    assert "refusing to prune an invalid audit chain" in report["errors"][0]
    assert archive.exists()
    assert verify_audit(data_dir, "tool-audit")[0] is False


def test_append_refuses_a_torn_tail(tmp_path):
    from ai_sre.audit import append_audit

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    torn = '{"partial":true'
    (data_dir / "tool-audit.jsonl").write_text(torn)

    assert append_audit(data_dir, "tool-audit", {"tool": "get"}) is False
    assert (data_dir / "tool-audit.jsonl").read_text() == torn


def test_append_does_not_reanchor_a_truncated_log(tmp_path, monkeypatch):
    from ai_sre.audit import append_audit, verify_audit

    data_dir = tmp_path / "data"
    monkeypatch.setenv("AI_SRE_AUDIT_ANCHOR_DIR", str(tmp_path / "anchor"))
    for index in range(3):
        assert append_audit(data_dir, "tool-audit", {"tool": f"t{index}"})

    log = data_dir / "tool-audit.jsonl"
    log.write_text("\n".join(log.read_text().splitlines()[:2]) + "\n")

    assert append_audit(data_dir, "tool-audit", {"tool": "next"}) is False
    ok, errors = verify_audit(data_dir, "tool-audit")
    assert ok is False
    assert "external audit anchor entry count mismatch" in errors


def test_verify_reports_invalid_encoding_instead_of_raising(tmp_path):
    from ai_sre.audit import append_audit, verify_audit

    data_dir = tmp_path / "data"
    assert append_audit(data_dir, "tool-audit", {"tool": "get"})
    with open(data_dir / "tool-audit.jsonl", "ab") as handle:
        handle.write(b"\xff")

    ok, errors = verify_audit(data_dir, "tool-audit")
    assert ok is False
    assert any("UnicodeDecodeError" in error for error in errors)
