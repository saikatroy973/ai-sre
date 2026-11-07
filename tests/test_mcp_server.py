import asyncio
import json
import os
import sqlite3
import sys
from uuid import UUID

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ai_sre.config import Settings
from ai_sre.mcp_server import (
    get_investigation,
    mcp,
    run_investigation,
    search_memory,
    source_status,
    verify_candidate,
)
from ai_sre.models import Scope
from ai_sre.storage import Store
from ai_sre.switch.memory import MemoryStore


def configure_fixture(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_SRE_DATA_DIR", str(tmp_path / "mcp-data"))
    monkeypatch.setenv("AI_SRE_BACKEND", "fixture")
    monkeypatch.setenv("AI_SRE_SCENARIO", "selector-mismatch")
    monkeypatch.setenv("AI_SRE_REASONER", "deterministic")


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
    first_cursor = first["next_cursor"]
    assert first_cursor is not None
    token, _, offset = first_cursor.partition(":")
    assert token
    assert offset == "1"

    late_result = run_investigation(
        question="Why is checkout failing?",
        cluster="demo-cluster",
        namespace="shop",
        service="checkout",
        deployment="checkout",
    )
    late_investigation = store.get(UUID(late_result["investigation_id"]))
    late_record = MemoryStore(store).approve(
        late_investigation,
        late_investigation.report,
        reviewer="alice",
        title="Late checkout selector record",
        summary="A new record inserted after the first page snapshot.",
    )

    second = search_memory(
        cluster="demo-cluster",
        namespace="shop",
        query="checkout selector",
        limit=1,
        cursor=first_cursor,
    )
    assert second["count"] == 1
    assert second["has_more"] is True
    assert second["next_cursor"] == f"{token}:2"

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
    assert str(late_record.id) not in record_ids

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
    assert entries[0]["prev_sha256"] == ""
    assert entries[1]["prev_sha256"] == entries[0]["entry_sha256"]
    assert entries[3]["error"] == "cursor must be a non-negative integer"
    assert entries[4]["error"] == "tenant is outside the configured server scope"

    memory = MemoryStore(store)
    ok, errors = memory.verify_audit()
    assert ok is True
    assert errors == []

    fresh = search_memory(
        cluster="demo-cluster",
        namespace="shop",
        query="checkout selector",
        limit=5,
    )
    assert fresh["count"] == 4
    assert str(late_record.id) in {lead["record_id"] for lead in fresh["leads"]}


def test_memory_audit_rotates_and_verifies_across_files(monkeypatch, tmp_path):
    configure_fixture(monkeypatch, tmp_path)
    monkeypatch.setenv("AI_SRE_MCP_AUDIT_MAX_BYTES", "1")
    settings = Settings.from_env()
    store = Store(settings.data_dir)
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
        title="Checkout selector mismatch",
        summary="The checkout Service selector excluded the ready Deployment pods.",
    )
    for _ in range(3):
        assert (
            search_memory(
                cluster="demo-cluster",
                namespace="shop",
                query="checkout selector",
            )["count"]
            == 1
        )
    memory = MemoryStore(store)
    archived = list(settings.data_dir.glob("memory-audit.*.jsonl"))
    assert len(archived) == 2
    entries = memory.read_audit(10)
    assert len(entries) == 3
    ok, errors = memory.verify_audit()
    assert ok is True
    assert errors == []


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
        "ai_sre_health",
        "ai_sre_run",
        "ai_sre_get",
        "ai_sre_verify",
        "ai_sre_memory",
        "ai_sre_source_status",
        "ai_sre_docs",
        "ai_sre_logs",
        "ai_sre_metrics",
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
    second_result = run_investigation(
        question="Why is checkout failing?",
        cluster="demo-cluster",
        namespace="shop",
        service="checkout",
        deployment="checkout",
    )
    second = store.get(UUID(second_result["investigation_id"]))
    MemoryStore(store).approve(
        second,
        second.report,
        reviewer="alice",
        title="Checkout selector mismatch second",
        summary="A second local record for pagination.",
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

    async def check() -> str:
        env = dict(os.environ)
        env["AI_SRE_DATA_DIR"] = str(settings.data_dir)
        env["AI_SRE_TENANT"] = "local"
        env["AI_SRE_SWITCH_MODE"] = "off"
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ai_sre.mcp_server"],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                memory = next(
                    tool for tool in tools.tools if tool.name == "ai_sre_memory"
                )
                schema = memory.inputSchema
                assert schema["properties"]["tenant"]["default"] is None
                assert "tenant" not in schema["required"]
                assert schema["properties"]["cursor"]["default"] is None
                assert "cursor" not in schema["required"]

                async def call(args: dict) -> dict:
                    response = await session.call_tool("ai_sre_memory", args)
                    return json.loads(response.content[0].text)

                base = {
                    "cluster": "demo-cluster",
                    "namespace": "shop",
                    "query": "checkout selector",
                }
                assert (await call(base))["count"] == 2
                assert (await call({**base, "tenant": None}))["count"] == 2
                assert (await call({**base, "tenant": "  local  "}))["count"] == 2
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
                page_one = await call({**base, "limit": 1})
                assert page_one["ok"] is True
                assert page_one["count"] == 1
                assert page_one["has_more"] is True
                cursor = page_one["next_cursor"]
                assert cursor
                page_two = await call({**base, "limit": 1, "cursor": cursor})
                assert page_two["ok"] is True
                assert page_two["count"] == 1
                assert page_two["has_more"] is False
                return cursor

    cursor = asyncio.run(check())
    audit_path = settings.data_dir / "memory-audit.jsonl"
    entries = [json.loads(line) for line in audit_path.read_text().splitlines() if line]
    assert entries
    assert entries[-1]["actor"] == "mcp"
    assert entries[-1]["outcome"] == "ok"
    assert entries[-1]["cursor"] == cursor


def test_two_mcp_servers_isolate_tenants(monkeypatch, tmp_path):
    data_a = tmp_path / "tenant-a"
    data_b = tmp_path / "tenant-b"
    for data_dir, tenant in ((data_a, "tenant-a"), (data_b, "tenant-b")):
        monkeypatch.setenv("AI_SRE_DATA_DIR", str(data_dir))
        monkeypatch.setenv("AI_SRE_BACKEND", "fixture")
        monkeypatch.setenv("AI_SRE_SCENARIO", "selector-mismatch")
        monkeypatch.setenv("AI_SRE_REASONER", "deterministic")
        result = run_investigation(
            question="Why is checkout failing?",
            cluster="demo-cluster",
            namespace="shop",
            service="checkout",
            deployment="checkout",
        )
        settings = Settings.from_env()
        store = Store(settings.data_dir)
        investigation = store.get(UUID(result["investigation_id"]))
        scoped = investigation.model_copy(
            update={
                "scope": Scope(
                    tenant=tenant,
                    cluster="demo-cluster",
                    namespace="shop",
                )
            }
        )
        MemoryStore(store).approve(
            scoped,
            scoped.report,
            reviewer="alice",
            title=f"{tenant} checkout selector mismatch",
            summary="A tenant-scoped record.",
        )

    async def call(tenant: str, data_dir, args: dict) -> dict:
        env = dict(os.environ)
        env["AI_SRE_DATA_DIR"] = str(data_dir)
        env["AI_SRE_TENANT"] = tenant
        env["AI_SRE_SWITCH_MODE"] = "off"
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ai_sre.mcp_server"],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                response = await session.call_tool("ai_sre_memory", args)
                return json.loads(response.content[0].text)

    base = {"cluster": "demo-cluster", "namespace": "shop", "query": "checkout selector"}
    page_a = asyncio.run(call("tenant-a", data_a, base))
    page_b = asyncio.run(call("tenant-b", data_b, base))
    assert page_a["count"] == 1
    assert page_b["count"] == 1
    assert page_a["leads"][0]["record_id"] != page_b["leads"][0]["record_id"]

    foreign = asyncio.run(call("tenant-a", data_a, {**base, "tenant": "tenant-b"}))
    assert foreign["ok"] is False
    assert foreign["error"] == "tenant is outside the configured server scope"


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


def test_get_and_verify_read_the_configured_evidence_store_end_to_end(monkeypatch, tmp_path):
    configure_fixture(monkeypatch, tmp_path)
    result = run_investigation(
        question="Why is checkout failing?",
        cluster="demo-cluster",
        namespace="shop",
        service="checkout",
        deployment="checkout",
    )
    monkeypatch.setenv("AI_SRE_EVIDENCE_STORE_DIR", str(tmp_path / "mcp-data"))
    record = get_investigation(result["investigation_id"])
    assert record["ok"] is True
    assert record["state"] == "reported"

    ids = [item["id"] for item in record["evidence"]]
    passed = verify_candidate(result["investigation_id"], "service_selector_mismatch", ids)
    assert passed["ok"] is True
    assert passed["verification"]["passed"] is True


def test_missing_evidence_store_fails_closed_without_inventing(tmp_path, monkeypatch):
    """Read-only stores never create their file, so a bad mount is a clean refusal."""
    configure_fixture(monkeypatch, tmp_path)
    monkeypatch.setenv("AI_SRE_EVIDENCE_STORE_DIR", str(tmp_path / "missing-mount"))
    assert not (tmp_path / "missing-mount").exists()
    missing = get_investigation("00000000-0000-0000-0000-000000000001")
    assert missing["ok"] is False
    assert "does not exist" in missing["error"]
    missing = verify_candidate(
        "00000000-0000-0000-0000-000000000001", "service_selector_mismatch", []
    )
    assert missing["ok"] is False
    assert "does not exist" in missing["error"]
    # The refused store directory was never created either.
    assert not (tmp_path / "missing-mount").exists()


def test_foreign_evidence_store_is_sanitized(monkeypatch, tmp_path):
    """A file that is not an evidence DB is a clean refusal, not a stack trace."""
    configure_fixture(monkeypatch, tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    connection = sqlite3.connect(foreign / "investigator.sqlite3")
    connection.execute("CREATE TABLE unrelated (x)")
    connection.commit()
    connection.close()
    monkeypatch.setenv("AI_SRE_EVIDENCE_STORE_DIR", str(foreign))

    missing = get_investigation("00000000-0000-0000-0000-000000000001")
    assert missing == {"ok": False, "error": "investigation tool failed"}
    missing = verify_candidate(
        "00000000-0000-0000-0000-000000000001", "service_selector_mismatch", []
    )
    assert missing == {"ok": False, "error": "investigation tool failed"}


def test_unavailable_memory_store_is_sanitized(monkeypatch, tmp_path):
    blocking = tmp_path / "data-is-a-file"
    blocking.write_text("not a directory")
    monkeypatch.setenv("AI_SRE_DATA_DIR", str(blocking))
    result = search_memory(cluster="demo-cluster", namespace="shop", query="checkout")
    assert result == {"ok": False, "error": "memory store unavailable"}
