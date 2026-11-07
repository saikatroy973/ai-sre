import sqlite3

import pytest

from ai_sre.models import InvestigationState
from ai_sre.storage import Store


def test_evidence_is_append_only(settings, investigation, evidence):
    store = Store(settings.data_dir)
    store.create(investigation)
    store.append_evidence(evidence[0])
    for statement in ("UPDATE evidence SET record = '{}'", "DELETE FROM evidence"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            with store.connection() as connection:
                connection.execute(statement)


def test_store_rejects_bad_digests_and_scope(settings, investigation, evidence):
    store = Store(settings.data_dir)
    store.create(investigation)
    for item in (
        evidence[0].model_copy(update={"sha256": "forged"}),
        evidence[0].model_copy(
            update={"scope": evidence[0].scope.model_copy(update={"namespace": "finance"})}
        ),
    ):
        with pytest.raises(ValueError, match="scope or digest"):
            store.append_evidence(item)


def test_state_changes_are_ordered_and_optimistic(settings, investigation):
    store = Store(settings.data_dir)
    store.create(investigation)
    with pytest.raises(ValueError, match="Invalid"):
        store.transition(investigation, InvestigationState.REPORTED)
    authorized = store.transition(investigation, InvestigationState.AUTHORIZED)
    with pytest.raises(ValueError, match="already advanced"):
        store.transition(investigation, InvestigationState.AUTHORIZED)
    assert store.get(investigation.id) == authorized


def test_local_database_is_not_world_readable(settings):
    store = Store(settings.data_dir)
    assert store.path.stat().st_mode & 0o077 == 0
    assert settings.data_dir.stat().st_mode & 0o077 == 0


def test_readonly_store_reads_without_creating(settings, investigation):
    store = Store(settings.data_dir)
    store.create(investigation)
    reader = Store(settings.data_dir, readonly=True)
    assert reader.path == store.path
    assert reader.get(investigation.id) == investigation


def test_readonly_store_fails_closed_when_the_store_is_missing(tmp_path):
    from ai_sre.errors import ConfigurationError

    missing = tmp_path / "no-such-store"
    with pytest.raises(ConfigurationError, match="does not exist"):
        Store(missing, readonly=True)
    # A reader that refuses must not create a directory or database either.
    assert not missing.exists()


def test_readonly_store_never_writes(settings, investigation):
    store = Store(settings.data_dir)
    store.create(investigation)
    reader = Store(settings.data_dir, readonly=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.create(investigation)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        with reader.connection() as connection:
            connection.execute(
                "INSERT INTO investigations (id, state, record) VALUES ('x', 'y', '{}')"
            )
