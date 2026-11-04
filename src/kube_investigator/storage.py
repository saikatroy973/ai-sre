from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from kube_investigator.errors import NotFoundError
from kube_investigator.models import (
    AgentMessage,
    Evidence,
    Investigation,
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


class Store:
    """Local milestone storage; deliberately not a distributed workflow queue."""

    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = data_dir / "investigator.sqlite3"
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
                CREATE TRIGGER IF NOT EXISTS evidence_no_update
                    BEFORE UPDATE ON evidence BEGIN
                    SELECT RAISE(ABORT, 'evidence is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS evidence_no_delete
                    BEFORE DELETE ON evidence BEGIN
                    SELECT RAISE(ABORT, 'evidence is append-only'); END;
                """
            )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
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
