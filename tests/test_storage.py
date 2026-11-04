import sqlite3

import pytest

from kube_investigator.models import InvestigationState
from kube_investigator.storage import Store


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
