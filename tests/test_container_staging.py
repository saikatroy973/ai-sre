"""Fail-closed staging for the isolated evidence-store probe."""

import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path

from ai_sre.auth import Principal
from ai_sre.config import Settings
from ai_sre.coordinator import build_coordinator
from ai_sre.models import InvestigationRequest
from ai_sre.storage import Store

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "mcp-container" / "stage_evidence.py"


def _completed_store(tmp_path, *, subject="local-operator"):
    settings = Settings(data_dir=tmp_path / "demo", switch_mode="off")
    coordinator = build_coordinator(settings)
    try:
        request = InvestigationRequest(
            question="Why is checkout failing?",
            cluster="demo-cluster",
            namespace="shop",
            service="checkout",
            deployment="checkout",
        )
        coordinator.run(request, Principal.operator(settings, subject=subject))
    finally:
        coordinator.close()
    return settings.data_dir / "investigator.sqlite3"


def _stage(source: Path, evidence: Path) -> subprocess.CompletedProcess:
    evidence.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(source), str(evidence)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_staging_records_the_snapshot_digest(tmp_path):
    source = _completed_store(tmp_path)
    evidence = tmp_path / "evidence"
    result = _stage(source, evidence)
    assert result.returncode == 0, result.stderr
    values = dict(
        line.split("=", 1) for line in (evidence.parent / "probe.env").read_text().splitlines()
    )
    snapshot = evidence / "investigator.sqlite3"
    assert (
        values["AI_SRE_EVIDENCE_PROBE_SHA256"]
        == hashlib.sha256(snapshot.read_bytes()).hexdigest()
    )
    assert snapshot.stat().st_mode & 0o777 == 0o444


def test_staging_refuses_a_missing_report(tmp_path):
    empty = tmp_path / "empty"
    Store(empty)
    result = _stage(empty / "investigator.sqlite3", tmp_path / "evidence")
    assert result.returncode != 0
    assert "no reported investigation" in result.stderr
    assert not (tmp_path / "probe.env").exists()


def test_staging_refuses_a_foreign_principal(tmp_path):
    source = _completed_store(tmp_path, subject="foreign-operator")
    result = _stage(source, tmp_path / "evidence")
    assert result.returncode != 0
    assert "principal='foreign-operator'" in result.stderr
    assert not (tmp_path / "probe.env").exists()


def test_staging_refuses_a_corrupt_record(tmp_path):
    source = _completed_store(tmp_path)
    connection = sqlite3.connect(source)
    try:
        connection.execute("UPDATE investigations SET record = '{'")
        connection.commit()
    finally:
        connection.close()
    result = _stage(source, tmp_path / "evidence")
    assert result.returncode != 0
    assert "corrupt" in result.stderr
    assert not (tmp_path / "probe.env").exists()


def test_container_probe_checks_the_snapshot_digest():
    runtime = (REPO / "deploy" / "mcp-container" / "verify_runtime.py").read_text()
    assert "AI_SRE_EVIDENCE_PROBE_SHA256" in runtime
    assert "snapshot digest does not match" in runtime
    staging = (REPO / "deploy" / "mcp-container" / "stage_evidence.py").read_text()
    assert "AI_SRE_EVIDENCE_PROBE_SHA256" in staging
