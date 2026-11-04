from kube_investigator.mcp_server import (
    get_investigation,
    run_investigation,
    search_memory,
    source_status,
    verify_candidate,
)


def configure_fixture(monkeypatch, tmp_path):
    monkeypatch.setenv("INVESTIGATOR_DATA_DIR", str(tmp_path / "mcp-data"))
    monkeypatch.setenv("INVESTIGATOR_BACKEND", "fixture")
    monkeypatch.setenv("INVESTIGATOR_SCENARIO", "selector-mismatch")
    monkeypatch.setenv("INVESTIGATOR_REASONER", "deterministic")


def test_mcp_investigation_returns_cited_read_only_report(monkeypatch, tmp_path):
    configure_fixture(monkeypatch, tmp_path)
    result = run_investigation(
        question="Why is checkout failing?",
        cluster="demo-cluster",
        namespace="shop",
        service="checkout",
        deployment="checkout",
    )
    assert result["ok"] is True
    assert result["report"]["outcome"] == "supported"
    assert result["report"]["diagnosis"] == "service_selector_mismatch"
    assert result["report"]["read_only"] is True
    assert len(result["evidence"]) == 5
    assert all(item["sha256"] for item in result["evidence"])
    assert all(item["complete"] is True for item in result["evidence"])

    read_back = get_investigation(result["investigation_id"])
    assert read_back["ok"] is True
    assert read_back["report"]["outcome"] == "supported"


def test_mcp_verifier_rechecks_the_persisted_evidence(monkeypatch, tmp_path):
    configure_fixture(monkeypatch, tmp_path)
    result = run_investigation(
        question="Why is checkout failing?",
        cluster="demo-cluster",
        namespace="shop",
        service="checkout",
        deployment="checkout",
    )
    ids = [item["id"] for item in result["evidence"]]
    passed = verify_candidate(result["investigation_id"], "service_selector_mismatch", ids)
    assert passed["ok"] is True
    assert passed["verification"]["passed"] is True

    failed = verify_candidate(result["investigation_id"], "service_selector_mismatch", ids[:2])
    assert failed["verification"]["passed"] is False
    assert "incomplete_citations" in failed["verification"]["checks"]


def test_mcp_memory_is_scoped_and_never_evidence(monkeypatch, tmp_path):
    configure_fixture(monkeypatch, tmp_path)
    result = search_memory(
        tenant="local",
        cluster="demo-cluster",
        namespace="shop",
        query="checkout selector",
    )
    assert result["ok"] is True
    assert result["count"] == 0
    assert result["used_as_evidence"] is False
    assert result["policy"] == "historical_leads_are_not_evidence"
    defaulted = search_memory(
        cluster="demo-cluster",
        namespace="shop",
        query="checkout selector",
    )
    assert defaulted["ok"] is True
    assert defaulted["count"] == 0


def test_mcp_source_status_abstains_without_inventing_evidence(monkeypatch, tmp_path):
    configure_fixture(monkeypatch, tmp_path)
    telemetry = source_status("telemetry")
    assert telemetry["status"] == "abstained"
    assert telemetry["configured"] is False
    assert telemetry["evidence_ids"] == []
    change = source_status("change")
    assert change["status"] == "abstained"
    kubernetes = source_status("kubernetes")
    assert kubernetes["configured"] is False
    assert kubernetes["backend"] == "fixture"


def test_mcp_rejects_invalid_ids_and_limits(monkeypatch, tmp_path):
    configure_fixture(monkeypatch, tmp_path)
    assert get_investigation("not-a-uuid")["ok"] is False
    assert verify_candidate("not-a-uuid", "service_selector_mismatch", [])["ok"] is False
    assert (
        search_memory(
            tenant="local",
            cluster="demo-cluster",
            namespace="shop",
            query="x",
            limit=100,
        )["ok"]
        is False
    )
