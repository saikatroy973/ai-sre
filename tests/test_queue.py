import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from kube_investigator.cli import main
from kube_investigator.errors import NotFoundError
from kube_investigator.models import InvestigationRequest, utc_now
from kube_investigator.storage import Store


def request_data():
    return InvestigationRequest(
        question="Why is checkout failing?",
        cluster="demo-cluster",
        namespace="shop",
        service="checkout",
        deployment="checkout",
    )


def test_queue_persists_claims_and_completes_jobs(tmp_path):
    store = Store(tmp_path / "data")
    job_id = store.enqueue(request_data())
    assert store.queue_jobs()[0]["status"] == "pending"
    claimed = store.claim_next()
    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.attempts == 1
    investigation_id = uuid4()
    store.complete_job(job_id, investigation_id)
    assert store.queue_jobs()[0]["status"] == "complete"
    assert store.queue_jobs()[0]["investigation_id"] == str(investigation_id)
    assert store.claim_next() is None


def test_queue_retry_returns_a_failed_job_to_pending(tmp_path):
    store = Store(tmp_path / "data")
    job_id = store.enqueue(request_data())
    claimed = store.claim_next()
    assert claimed is not None
    store.fail_job(job_id, "worker failed")
    assert store.queue_jobs()[0]["status"] == "failed"
    store.retry_job(job_id)
    assert store.queue_jobs()[0]["status"] == "pending"
    again = store.claim_next()
    assert again is not None
    assert again.id == job_id
    assert again.attempts == 2


def test_queue_recovers_an_expired_worker_lease(tmp_path):
    store = Store(tmp_path / "data")
    job_id = store.enqueue(request_data())
    claimed = store.claim_next(worker_id="worker-1", lease_seconds=-1)
    assert claimed is not None
    assert store.queue_jobs()[0]["worker_id"] == "worker-1"
    restarted = Store(tmp_path / "data")
    recovered = restarted.recover_expired(now=(utc_now() + timedelta(seconds=1)).isoformat())
    assert recovered == 1
    assert restarted.queue_jobs()[0]["status"] == "pending"
    assert restarted.queue_jobs()[0]["error"] == "lease expired"
    again = restarted.claim_next(worker_id="worker-2")
    assert again is not None
    assert again.id == job_id
    assert again.attempts == 2
    assert restarted.queue_jobs()[0]["worker_id"] == "worker-2"


def test_queue_stale_worker_cannot_complete_a_recovered_job(tmp_path):
    store = Store(tmp_path / "data")
    job_id = store.enqueue(request_data())
    assert store.claim_next(worker_id="worker-1", lease_seconds=-1) is not None
    store.recover_expired(now=(utc_now() + timedelta(seconds=1)).isoformat())
    assert store.claim_next(worker_id="worker-2") is not None
    with pytest.raises(NotFoundError):
        store.complete_job(job_id, uuid4(), worker_id="worker-1")
    store.complete_job(job_id, uuid4(), worker_id="worker-2")
    assert store.queue_jobs()[0]["status"] == "complete"


def test_queue_lease_env_is_clamped_for_crash_drills(monkeypatch):
    from kube_investigator.cli import _queue_lease_seconds

    monkeypatch.delenv("INVESTIGATOR_QUEUE_LEASE_SECONDS", raising=False)
    assert _queue_lease_seconds() == 300

    monkeypatch.setenv("INVESTIGATOR_QUEUE_LEASE_SECONDS", "20")
    assert _queue_lease_seconds() == 20

    # A bad or extreme value cannot make leases undetectable or instantly recyclable.
    monkeypatch.setenv("INVESTIGATOR_QUEUE_LEASE_SECONDS", "2")
    assert _queue_lease_seconds() == 10
    monkeypatch.setenv("INVESTIGATOR_QUEUE_LEASE_SECONDS", "99999")
    assert _queue_lease_seconds() == 3600
    monkeypatch.setenv("INVESTIGATOR_QUEUE_LEASE_SECONDS", "not-a-number")
    assert _queue_lease_seconds() == 300


def test_queue_run_passes_the_drill_worker_label_and_lease(monkeypatch, tmp_path):
    """The harness label and lease override reach `claim_next` verbatim.

    Completed rows intentionally blank their worker_id, so the label is verified
    where it matters: the atomic claim.
    """
    from kube_investigator.storage import Store as RealStore

    captured: dict[str, object] = {}

    def spy(self, *, worker_id, lease_seconds=300):
        captured["worker_id"] = worker_id
        captured["lease_seconds"] = lease_seconds
        return None

    monkeypatch.setattr(RealStore, "claim_next", spy)
    monkeypatch.setenv("INVESTIGATOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INVESTIGATOR_SWITCH_MODE", "off")
    monkeypatch.setenv("INVESTIGATOR_QUEUE_WORKER_LABEL", "drill-worker-0")
    monkeypatch.setenv("INVESTIGATOR_QUEUE_LEASE_SECONDS", "25")
    assert main(["queue", "run", "--data-dir", str(tmp_path / "data"), "--json"]) == 0
    assert captured == {"worker_id": "drill-worker-0", "lease_seconds": 25}


def test_queue_two_workers_never_claim_the_same_job(tmp_path):
    store = Store(tmp_path / "data")
    job_ids = {store.enqueue(request_data()) for _ in range(12)}

    def claim(worker):
        claimed = []
        while job := store.claim_next(worker_id=worker):
            claimed.append(job.id)
        return claimed

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("worker-1", "worker-2")))

    claimed_ids = [job_id for batch in results for job_id in batch]
    assert len(claimed_ids) == len(job_ids)
    assert set(claimed_ids) == job_ids
    assert len(set(claimed_ids)) == len(claimed_ids)
    workers = {job["worker_id"] for job in store.queue_jobs()}
    assert workers <= {"worker-1", "worker-2"}


def test_queue_four_workers_claim_and_complete_thirty_jobs(tmp_path):
    store = Store(tmp_path / "data")
    job_ids = {store.enqueue(request_data()) for _ in range(30)}

    def work(worker):
        completed = []
        while job := store.claim_next(worker_id=worker):
            store.complete_job(job.id, uuid4(), worker_id=worker)
            completed.append(job.id)
        return completed

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(work, (f"worker-{index}" for index in range(4))))

    completed_ids = [job_id for batch in results for job_id in batch]
    assert len(completed_ids) == len(job_ids)
    assert set(completed_ids) == job_ids
    assert len(set(completed_ids)) == len(completed_ids)
    jobs = store.queue_jobs()
    assert {job["status"] for job in jobs} == {"complete"}
    assert {job["worker_id"] for job in jobs} == {None}


def test_queue_add_and_list_are_durable(tmp_path, capsys):
    data_dir = tmp_path / "data"
    assert (
        main(
            [
                "queue",
                "add",
                "--question",
                "Why is checkout failing?",
                "--cluster",
                "demo-cluster",
                "--namespace",
                "shop",
                "--service",
                "checkout",
                "--deployment",
                "checkout",
                "--data-dir",
                str(data_dir),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pending"
    assert main(["queue", "list", "--data-dir", str(data_dir), "--json"]) == 0
    jobs = json.loads(capsys.readouterr().out)
    assert len(jobs) == 1
    assert jobs[0]["request"]["service"] == "checkout"


def test_queue_worker_runs_the_offline_fixture(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("INVESTIGATOR_SWITCH_MODE", "off")
    store = Store(data_dir)
    job_id = store.enqueue(request_data())
    assert main(["queue", "run", "--data-dir", str(data_dir), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result[0]["queue_id"] == str(job_id)
    assert result[0]["status"] == "complete"
    investigation_id = result[0]["investigation_id"]
    saved = Store(data_dir).get(UUID(investigation_id))
    assert saved.report is not None
    assert saved.report.outcome == "supported"
    assert saved.switch_room_id is None
