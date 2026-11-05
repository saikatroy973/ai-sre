import asyncio
import json
import os
import sys
from uuid import UUID

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from kube_investigator.config import Settings
from kube_investigator.mcp_server import (
    get_investigation,
    mcp,
    run_investigation,
    search_memory,
    source_status,
    verify_candidate,
)
from kube_investigator.models import Scope
from kube_investigator.storage import Store
from kube_investigator.switch.memory import MemoryStore


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


def test_mcp_memory_tenant_defaults_and_isolates(monkeypatch, tmp_path):
    configure_fixture(monkeypatch, tmp_path)
    result = run_investigation(
        question="Why is checkout failing?",
        cluster="demo-cluster",
        namespace="shop",
        service="checkout",
        deployment="checkout",
    )
    assert result["ok"] is True
    settings = Settings.from_env()
    store = Store(settings.data_dir)
    investigation = store.get(UUID(result["investigation_id"]))
    MemoryStore(store).approve(
        investigation,
        investigation.report,
        reviewer="alice",
        title="Checkout selector mismatch",
        summary="The checkout Service selector excluded the ready Deployment pods.",
    )
    foreign = investigation.model_copy(
        update={"scope": Scope(tenant="other", cluster="demo-cluster", namespace="shop")}
    )
    MemoryStore(store).approve(
        foreign,
        foreign.report,
        reviewer="alice",
        title="Foreign tenant checkout record",
        summary="This record must never be visible outside the other tenant.",
    )
    foreign_leads = MemoryStore(store).search(
        scope=Scope(tenant="other", cluster="demo-cluster", namespace="shop"),
        query="foreign tenant",
    )
    assert len(foreign_leads) == 1

    defaulted = search_memory(
        cluster="demo-cluster",
        namespace="shop",
        query="checkout selector",
    )
    assert defaulted["ok"] is True
    assert defaulted["count"] == 1

    explicit = search_memory(
        tenant="local",
        cluster="demo-cluster",
        namespace="shop",
        query="checkout selector",
    )
    assert explicit["count"] == 1

    null_tenant = search_memory(
        tenant=None,
        cluster="demo-cluster",
        namespace="shop",
        query="checkout selector",
    )
    assert null_tenant["count"] == 1

    other_tenant = search_memory(
        tenant="other",
        cluster="demo-cluster",
        namespace="shop",
        query="checkout selector",
    )
    assert other_tenant["ok"] is False
    assert "outside the configured server scope" in other_tenant["error"]

    other_namespace = search_memory(
        tenant=None,
        cluster="demo-cluster",
        namespace="other",
        query="checkout selector",
    )
    assert other_namespace["count"] == 0

    trimmed = search_memory(
        tenant="  local  ",
        cluster="demo-cluster",
        namespace="shop",
        query="checkout selector",
    )
    assert trimmed["count"] == 1

    empty_tenant = search_memory(
        tenant="",
        cluster="demo-cluster",
        namespace="shop",
        query="checkout selector",
    )
    assert empty_tenant["ok"] is False
    assert "tenant" in empty_tenant["error"]

    blank_tenant = search_memory(
        tenant="   ",
        cluster="demo-cluster",
        namespace="shop",
        query="checkout selector",
    )
    assert blank_tenant["ok"] is False

    empty_query = search_memory(
        cluster="demo-cluster",
        namespace="shop",
        query="   ",
    )
    assert empty_query["ok"] is False
    assert "query" in empty_query["error"]

    long_query = search_memory(
        cluster="demo-cluster",
        namespace="shop",
        query="x" * 4001,
    )
    assert long_query["ok"] is False
    assert "4000" in long_query["error"]

    bad_scope = search_memory(
        cluster="demo-cluster",
        namespace="Bad_NS",
        query="checkout selector",
    )
    assert bad_scope["ok"] is False
    assert bad_scope["error"] == "invalid memory search scope"


def test_mcp_memory_pagination_and_audit(monkeypatch, tmp_path):
    configure_fixture(monkeypatch, tmp_path)
    settings = Settings.from_env()
    store = Store(settings.data_dir)
    for index in range(3):
        result = run_investigation(
            question="Why is checkout failing?",
            cluster="demo-cluster",
            namespace="shop",
            service="checkout",
            deployment="checkout",
        )
        investigation = store.get(UUID(result["investigation_id"]))
        MemoryStore(store).approve(
            investigation,
            investigation.report,
            reviewer="alice",
            title=f"Checkout selector mismatch {index}",
            summary="The checkout Service selector excluded the ready Deployment pods.",
        )

    first = search_memory(
        cluster="demo-cluster",
        namespace="shop",
        query="checkout selector",
        limit=1,
    )
    assert first["count"] == 1
    assert first["has_more"] is True
    assert first["next_cursor"] == "1"

    second = search_memory(
        cluster="demo-cluster",
        namespace="shop",
        query="checkout selector",
        limit=1,
        cursor=first["next_cursor"],
    )
    assert second["count"] == 1
    assert second["has_more"] is True
    assert second["next_cursor"] == "2"

    third = search_memory(
        cluster="demo-cluster",
        namespace="shop",
        query="checkout selector",
        limit=1,
        cursor=second["next_cursor"],
    )
    assert third["count"] == 1
    assert third["has_more"] is False
    assert third["next_cursor"] is None

    record_ids = {
        first["leads"][0]["record_id"],
        second["leads"][0]["record_id"],
        third["leads"][0]["record_id"],
    }
    assert len(record_ids) == 3

    bad_cursor = search_memory(
        cluster="demo-cluster",
        namespace="shop",
        query="checkout selector",
        cursor="not-a-number",
    )
    assert bad_cursor["ok"] is False
    assert "cursor" in bad_cursor["error"]

    rejected = search_memory(
        tenant="other",
        cluster="demo-cluster",
        namespace="shop",
        query="checkout selector",
    )
    assert rejected["ok"] is False

    audit_path = settings.data_dir / "memory-audit.jsonl"
    entries = [json.loads(line) for line in audit_path.read_text().splitlines() if line]
    assert len(entries) == 5
    assert [entry["outcome"] for entry in entries] == [
        "ok",
        "ok",
        "ok",
        "rejected",
        "rejected",
    ]
    assert entries[0]["count"] == 1
    assert entries[0]["actor"] == "mcp"
    assert entries[0]["query_sha256"]
    assert entries[3]["error"] == "cursor must be a non-negative integer"
    assert entries[4]["error"] == "tenant is outside the configured server scope"


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


def test_mcp_exposes_only_read_only_investigation_tools():
    tools = asyncio.run(mcp.list_tools())
    names = {tool.name for tool in tools}
    assert names == {
        "kube_investigator_health",
        "kube_investigator_run",
        "kube_investigator_get",
        "kube_investigator_verify",
        "kube_investigator_memory",
        "kube_investigator_source_status",
    }
    forbidden = ("apply", "delete", "exec", "patch", "scale", "secret", "write")
    assert not [name for name in names if any(word in name for word in forbidden)]


def test_mcp_protocol_memory_schema_and_tenant_contract(monkeypatch, tmp_path):
    configure_fixture(monkeypatch, tmp_path)
    result = run_investigation(
        question="Why is checkout failing?",
        cluster="demo-cluster",
        namespace="shop",
        service="checkout",
        deployment="checkout",
    )
    assert result["ok"] is True
    settings = Settings.from_env()
    store = Store(settings.data_dir)
    investigation = store.get(UUID(result["investigation_id"]))
    MemoryStore(store).approve(
        investigation,
        investigation.report,
        reviewer="alice",
        title="Checkout selector mismatch",
        summary="The checkout Service selector excluded the ready Deployment pods.",
    )

    async def check() -> None:
        env = dict(os.environ)
        env["INVESTIGATOR_DATA_DIR"] = str(settings.data_dir)
        env["INVESTIGATOR_TENANT"] = "local"
        env["INVESTIGATOR_SWITCH_MODE"] = "off"
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "kube_investigator.mcp_server"],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                memory = next(
                    tool for tool in tools.tools if tool.name == "kube_investigator_memory"
                )
                schema = memory.inputSchema
                assert schema["properties"]["tenant"]["default"] is None
                assert "tenant" not in schema["required"]
                assert schema["properties"]["cursor"]["default"] is None
                assert "cursor" not in schema["required"]

                async def call(args: dict) -> dict:
                    response = await session.call_tool("kube_investigator_memory", args)
                    return json.loads(response.content[0].text)

                base = {
                    "cluster": "demo-cluster",
                    "namespace": "shop",
                    "query": "checkout selector",
                }
                assert (await call(base))["count"] == 1
                assert (await call({**base, "tenant": None}))["count"] == 1
                assert (await call({**base, "tenant": "  local  "}))["count"] == 1
                assert (await call({**base, "tenant": "other"}))["ok"] is False
                assert (await call({**base, "tenant": "other"}))["error"] == (
                    "tenant is outside the configured server scope"
                )
                assert (await call({**base, "tenant": ""}))["ok"] is False
                assert (await call({**base, "query": "   "}))["ok"] is False
                assert (await call({**base, "query": "x" * 4001}))["ok"] is False
                assert (await call({**base, "namespace": "Bad_NS"}))["error"] == (
                    "invalid memory search scope"
                )
                cursor_page = await call({**base, "limit": 1, "cursor": "1"})
                assert cursor_page["ok"] is True
                assert cursor_page["count"] == 0
                assert cursor_page["has_more"] is False

    asyncio.run(check())
    audit_path = settings.data_dir / "memory-audit.jsonl"
    entries = [json.loads(line) for line in audit_path.read_text().splitlines() if line]
    assert entries
    assert entries[-1]["actor"] == "mcp"
    assert entries[-1]["outcome"] == "ok"
    assert entries[-1]["cursor"] == "1"


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
