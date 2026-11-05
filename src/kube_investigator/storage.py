from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4

from kube_investigator.errors import ConfigurationError, NotFoundError
from kube_investigator.models import (
    AgentMessage,
    Evidence,
    Investigation,
    InvestigationRequest,
    InvestigationState,
    Report,
    utc_now,
)

_TRANSITIONS = {
    InvestigationState.RECEIVED: {InvestigationState.AUTHORIZED, InvestigationState.FAILED},
    InvestigationState.AUTHORIZED: {InvestigationState.COLLECTING, InvestigationState.FAILED},
    InvestigationState.COLLECTING: {InvestigationState.ANALYZING, InvestigationState.FAILED},
    InvestigationState.ANALYZING: {InvestigationState.VERIFYING, InvestigationState.FAILED},
    InvestigationState.VERIFYING: {InvestigationState.REPORTED, InvestigationState.FAILED},
    InvestigationState.REPORTED: set(),
    InvestigationState.FAILED: set(),
}


@dataclass(frozen=True)
class QueueJob:
    id: UUID
    request: InvestigationRequest
    attempts: int


class Store:
    """Local milestone storage; deliberately not a distributed workflow queue.

    With ``readonly=True`` the store opens the SQLite file in URI read-only
    mode and never creates, migrates, or upgrades anything: it fails closed
    when the file is missing and any write attempt becomes a SQLite
    "attempt to write a readonly database" error.  Used by the MCP tools that
    read persisted evidence, so an isolated runtime can mount a snapshot.
    """

    def __init__(self, data_dir: Path, *, readonly: bool = False):
        self.readonly = readonly
        self.path = data_dir / "investigator.sqlite3"
        if readonly:
            if not self.path.is_file():
                raise ConfigurationError(
                    f"evidence store {self.path} does not exist; "
                    "a read-only store never creates one"
                )
            return
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create with restrictive permissions before SQLite opens it.
        self.path.touch(mode=0o600, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS investigations (
                    id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    record TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY,
                    investigation_id TEXT NOT NULL REFERENCES investigations(id),
                    record TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS evidence_investigation
                    ON evidence(investigation_id);
                CREATE TABLE IF NOT EXISTS messages (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT UNIQUE NOT NULL,
                    investigation_id TEXT NOT NULL REFERENCES investigations(id),
                    record TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_investigation
                    ON messages(investigation_id);
                CREATE TABLE IF NOT EXISTS investigation_queue (
                    id TEXT PRIMARY KEY,
                    request TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','running','complete','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    investigation_id TEXT,
                    error TEXT,
                    worker_id TEXT,
                    lease_until TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS investigation_queue_status
                    ON investigation_queue(status);
                CREATE TRIGGER IF NOT EXISTS evidence_no_update
                    BEFORE UPDATE ON evidence BEGIN
                    SELECT RAISE(ABORT, 'evidence is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS evidence_no_delete
                    BEFORE DELETE ON evidence BEGIN
                    SELECT RAISE(ABORT, 'evidence is append-only'); END;
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(investigation_queue)")
            }
            for name, kind in (("worker_id", "TEXT"), ("lease_until", "TEXT")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE investigation_queue ADD COLUMN {name} {kind}")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        if self.readonly:
            connection = sqlite3.connect(
                f"file:{quote(str(self.path.absolute()))}?mode=ro", uri=True, timeout=5
            )
        else:
            connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create(self, investigation: Investigation) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO investigations (id, state, record) VALUES (?, ?, ?)",
                (str(investigation.id), investigation.state, investigation.model_dump_json()),
            )

    def get(self, investigation_id: UUID) -> Investigation:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT record FROM investigations WHERE id = ?", (str(investigation_id),)
            ).fetchone()
        if row is None:
            raise NotFoundError("Investigation not found")
        return Investigation.model_validate_json(row[0])

    def transition(
        self,
        investigation: Investigation,
        state: InvestigationState,
        *,
        report: Report | None = None,
        error_code: str | None = None,
    ) -> Investigation:
        if state not in _TRANSITIONS[investigation.state]:
            raise ValueError("Invalid investigation state transition")
        updated = investigation.model_copy(
            update={
                "state": state,
                "updated_at": utc_now(),
                "report": report,
                "error_code": error_code,
            }
        )
        with self.connection() as connection:
            result = connection.execute(
                "UPDATE investigations SET state = ?, record = ? WHERE id = ? AND state = ?",
                (state, updated.model_dump_json(), str(updated.id), investigation.state),
            )
            if result.rowcount != 1:
                raise ValueError("Investigation was already advanced by another caller")
        return updated

    def bind_switch_room(
        self,
        investigation: Investigation,
        *,
        room_id: str,
        room_name: str,
        base_url: str,
    ) -> Investigation:
        updated = investigation.model_copy(
            update={
                "switch_room_id": room_id,
                "switch_room_name": room_name,
                "switch_base_url": base_url,
                "updated_at": utc_now(),
            }
        )
        with self.connection() as connection:
            result = connection.execute(
                "UPDATE investigations SET record = ? WHERE id = ? AND state = ?",
                (updated.model_dump_json(), str(updated.id), investigation.state),
            )
            if result.rowcount != 1:
                raise ValueError("Investigation was already advanced by another caller")
        return updated

    def append_evidence(self, evidence: Evidence) -> None:
        investigation = self.get(evidence.investigation_id)
        if evidence.scope != investigation.scope or evidence.sha256 != evidence.digest():
            raise ValueError("Evidence scope or digest is invalid")
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO evidence (id, investigation_id, record) VALUES (?, ?, ?)",
                (str(evidence.id), str(evidence.investigation_id), evidence.model_dump_json()),
            )

    def evidence(self, investigation_id: UUID) -> list[Evidence]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT record FROM evidence WHERE investigation_id = ? ORDER BY rowid",
                (str(investigation_id),),
            ).fetchall()
        return [Evidence.model_validate_json(row[0]) for row in rows]

    def append_message(self, message: AgentMessage) -> None:
        investigation = self.get(message.investigation_id)
        if message.trace_id != investigation.trace_id:
            raise ValueError("Message belongs to a different trace")
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO messages (id, investigation_id, record) VALUES (?, ?, ?)",
                (str(message.id), str(message.investigation_id), message.model_dump_json()),
            )

    def messages(self, investigation_id: UUID) -> list[AgentMessage]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT record FROM messages WHERE investigation_id = ? ORDER BY sequence",
                (str(investigation_id),),
            ).fetchall()
        return [AgentMessage.model_validate_json(row[0]) for row in rows]

    def enqueue(self, request: InvestigationRequest) -> UUID:
        """Persist a new incident request for a worker to claim."""
        job_id = uuid4()
        now = utc_now().isoformat()
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO investigation_queue "
                "(id, request, status, attempts, created_at, updated_at) "
                "VALUES (?, ?, 'pending', 0, ?, ?)",
                (str(job_id), request.model_dump_json(), now, now),
            )
        return job_id

    def queue_jobs(self, *, status: str | None = None) -> list[dict[str, object]]:
        query = (
            "SELECT id, request, status, attempts, investigation_id, error, "
            "worker_id, lease_until, created_at, updated_at FROM investigation_queue"
        )
        parameters: tuple[object, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            parameters = (status,)
        query += " ORDER BY rowid"
        with self.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            {
                "id": row[0],
                "request": InvestigationRequest.model_validate_json(row[1]).model_dump(mode="json"),
                "status": row[2],
                "attempts": row[3],
                "investigation_id": row[4],
                "error": row[5],
                "worker_id": row[6],
                "lease_until": row[7],
                "created_at": row[8],
                "updated_at": row[9],
            }
            for row in rows
        ]

    def claim_next(
        self,
        *,
        worker_id: str | None = None,
        lease_seconds: int = 300,
    ) -> QueueJob | None:
        """Atomically move the oldest pending job to running with a lease."""
        now = utc_now()
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.connection() as connection:
            # Serialize the select-and-claim pair so two workers cannot choose
            # the same row and let the loser exit while other jobs remain.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, request, attempts FROM investigation_queue "
                "WHERE status = 'pending' ORDER BY rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                "UPDATE investigation_queue "
                "SET status = 'running', attempts = attempts + 1, worker_id = ?, "
                "lease_until = ?, updated_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (worker_id, lease_until, now.isoformat(), row[0]),
            )
            if updated.rowcount != 1:
                return None
        return QueueJob(
            id=UUID(str(row[0])),
            request=InvestigationRequest.model_validate_json(row[1]),
            attempts=int(row[2]) + 1,
        )

    def recover_expired(self, *, now: str | None = None) -> int:
        """Return running jobs whose lease expired to pending."""
        timestamp = now or utc_now().isoformat()
        with self.connection() as connection:
            result = connection.execute(
                "UPDATE investigation_queue "
                "SET status = 'pending', worker_id = NULL, lease_until = NULL, "
                "error = 'lease expired', updated_at = ? "
                "WHERE status = 'running' "
                "AND (lease_until IS NULL OR lease_until < ?)",
                (timestamp, timestamp),
            )
        return result.rowcount

    def complete_job(
        self,
        job_id: UUID,
        investigation_id: UUID,
        *,
        worker_id: str | None = None,
    ) -> None:
        query = (
            "UPDATE investigation_queue SET status = 'complete', investigation_id = ?, "
            "error = NULL, worker_id = NULL, lease_until = NULL, updated_at = ? "
            "WHERE id = ? AND status = 'running'"
        )
        parameters: list[object] = [
            str(investigation_id),
            utc_now().isoformat(),
            str(job_id),
        ]
        if worker_id is not None:
            query += " AND worker_id = ?"
            parameters.append(worker_id)
        with self.connection() as connection:
            result = connection.execute(query, tuple(parameters))
            if result.rowcount != 1:
                raise NotFoundError("Queue job is not running for this worker")

    def fail_job(
        self,
        job_id: UUID,
        error: str,
        *,
        worker_id: str | None = None,
    ) -> None:
        query = (
            "UPDATE investigation_queue SET status = 'failed', error = ?, "
            "worker_id = NULL, lease_until = NULL, updated_at = ? "
            "WHERE id = ? AND status = 'running'"
        )
        parameters: list[object] = [error[:2000], utc_now().isoformat(), str(job_id)]
        if worker_id is not None:
            query += " AND worker_id = ?"
            parameters.append(worker_id)
        with self.connection() as connection:
            result = connection.execute(query, tuple(parameters))
            if result.rowcount != 1:
                raise NotFoundError("Queue job is not running for this worker")

    def retry_job(self, job_id: UUID) -> None:
        with self.connection() as connection:
            result = connection.execute(
                "UPDATE investigation_queue SET status = 'pending', error = NULL, "
                "updated_at = ? WHERE id = ? AND status = 'failed'",
                (utc_now().isoformat(), str(job_id)),
            )
            if result.rowcount != 1:
                raise NotFoundError("Queue job is not failed")
