import json
from uuid import UUID, uuid4

from kube_investigator.cli import main
from kube_investigator.models import InvestigationRequest
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
