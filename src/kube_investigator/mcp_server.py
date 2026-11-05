"""Read-only MCP tools for Switch Console-managed investigation agents.

Switch Console runs the agents and gives them Switch's own room tools.  This
server is the other half of the split: the Kubernetes evidence work that Switch
does not provide.  It never creates a room, posts a message, or writes to a
cluster.  Switch remains the collaboration plane.

The server is deliberately small.  Every tool returns a JSON-serializable
result, every failure is sanitized, and no evidence content or credential is
logged.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Literal
from uuid import UUID

from mcp.server.fastmcp import FastMCP

from kube_investigator import __version__
from kube_investigator.auth import Principal
from kube_investigator.config import Settings
from kube_investigator.coordinator import build_coordinator
from kube_investigator.errors import InvestigatorError
from kube_investigator.mcp_auth import authorize_actor
from kube_investigator.models import Candidate, InvestigationRequest, Scope
from kube_investigator.storage import Store
from kube_investigator.switch.memory import MemoryStore
from kube_investigator.telemetry import configure_tracing
from kube_investigator.verification import EvidenceVerifier

mcp = FastMCP(
    "kube-investigator",
    instructions=(
        "Read-only Kubernetes evidence tools. Use Switch's own tools for room messages, "
        "handoffs, documents, and reports. These tools never mutate a cluster."
    ),
)


def _settings() -> Settings:
    """The MCP server is not a Switch runtime; it never opens a room itself."""
    return replace(Settings.from_env(), switch_mode="off")


def _error(exc: Exception) -> dict[str, Any]:
    message = str(exc) if isinstance(exc, InvestigatorError) else "investigation tool failed"
    return {"ok": False, "error": message}


def _authorize(settings: Settings) -> str | None:
    """Return the auth error for ``settings``; ``None`` when the actor is valid.

    Every tool except ``health`` is gated.  Without a configured public key
    the server runs in local development mode and admits the env-labeled
    actor, so ordinary local use keeps working.
    """
    _, auth_error = authorize_actor(settings)
    return auth_error


def _evidence_summary(evidence: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(item.id),
            "kind": item.kind.value,
            "status": item.status,
            "complete": item.query.complete,
            "sha256": item.sha256,
            "collected_at": item.collected_at.isoformat(),
        }
        for item in evidence
    ]


def _investigation_payload(investigation: Any, evidence: list[Any]) -> dict[str, Any]:
    report = investigation.report
    return {
        "ok": True,
        "investigation_id": str(investigation.id),
        "trace_id": investigation.trace_id,
        "state": investigation.state.value,
        "scope": investigation.scope.model_dump(mode="json"),
        "request": investigation.request.model_dump(mode="json"),
        "report": report.model_dump(mode="json") if report is not None else None,
        "evidence": _evidence_summary(evidence),
        "read_only": True,
    }


def run_investigation(
    *,
    question: str,
    cluster: str,
    namespace: str,
    service: str,
    deployment: str,
    scenario: Literal["selector-mismatch", "healthy", "ambiguous"] | None = None,
) -> dict[str, Any]:
    """Collect bounded evidence, propose, and verify one selector-mismatch case.

    This is the deterministic evidence path the incident commander calls.  It
    writes a local investigation record and evidence digests, but no Switch
    room and no cluster change.
    """
    settings = _settings()
    auth_error = _authorize(settings)
    if auth_error is not None:
        return {"ok": False, "error": auth_error}
    if scenario is not None and settings.backend == "fixture":
        settings = replace(settings, scenario=scenario)
    coordinator = None
    try:
        request = InvestigationRequest(
            question=question,
            cluster=cluster,
            namespace=namespace,
            service=service,
            deployment=deployment,
        )
        coordinator = build_coordinator(settings)
        investigation = coordinator.run(request, Principal.operator(settings))
        evidence = coordinator.store.evidence(investigation.id)
        return _investigation_payload(investigation, evidence)
    except Exception as exc:  # noqa: BLE001 - sanitized at the tool boundary
        return _error(exc)
    finally:
        if coordinator is not None:
            coordinator.close()


def get_investigation(investigation_id: str) -> dict[str, Any]:
    """Read back a persisted investigation and its evidence metadata.

    The evidence store is opened strictly read-only: this tool can never
    create, upgrade, or write an investigation record — even accidentally.
    """
    settings = _settings()
    auth_error = _authorize(settings)
    if auth_error is not None:
        return {"ok": False, "error": auth_error}
    try:
        started = UUID(investigation_id)
    except ValueError:
        return {"ok": False, "error": "investigation_id must be a UUID"}
    try:
        store = Store(settings.evidence_store_dir, readonly=True)
        investigation = store.get(started)
    except InvestigatorError as exc:
        return _error(exc)
    if not Principal.operator(settings).can_read(investigation):
        return {"ok": False, "error": "investigation not found"}
    return _investigation_payload(investigation, store.evidence(investigation.id))


def verify_candidate(
    investigation_id: str,
    diagnosis: Literal["service_selector_mismatch", "undetermined"],
    evidence_ids: list[str],
) -> dict[str, Any]:
    """Re-run the deterministic verifier against persisted evidence.

    The verifier never trusts the diagnosis string; it checks provenance,
    freshness, scope, and the selector proof against the stored snapshots.
    """
    settings = _settings()
    auth_error = _authorize(settings)
    if auth_error is not None:
        return {"ok": False, "error": auth_error}
    try:
        started = UUID(investigation_id)
        candidate = Candidate(
            category=diagnosis,
            evidence_ids=[UUID(value) for value in evidence_ids],
        )
    except (ValueError, TypeError):
        return {"ok": False, "error": "invalid investigation or evidence id"}
    try:
        store = Store(settings.evidence_store_dir, readonly=True)
        investigation = store.get(started)
    except InvestigatorError as exc:
        return _error(exc)
    if not Principal.operator(settings).can_read(investigation):
        return {"ok": False, "error": "investigation not found"}
    verification = EvidenceVerifier(settings).verify(
        investigation, candidate, store.evidence(investigation.id)
    )
    return {
        "ok": True,
        "investigation_id": str(investigation.id),
        "verification": verification.model_dump(mode="json"),
        "read_only": True,
    }


def search_memory(
    *,
    tenant: str | None = None,
    cluster: str,
    namespace: str,
    query: str,
    limit: int = 5,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Search human-approved historical leads for this exact scope.

    Results are leads, not evidence.  They are never passed to the reasoner.
    Omit tenant to use the configured server tenant.  Use ``next_cursor`` from
    a prior response to page through a stable result snapshot.
    """
    settings = _settings()
    store = Store(settings.data_dir)
    memory = MemoryStore(store)
    actor, auth_error = authorize_actor(settings)
    offset = 0
    leads: list[Any] = []

    if auth_error is not None:
        memory.audit_search(
            actor="unauthenticated",
            requested_tenant=tenant,
            effective_tenant=tenant or settings.tenant,
            cluster=cluster,
            namespace=namespace,
            query=query,
            limit=limit,
            cursor=cursor,
            offset=0,
            outcome="rejected",
            leads=[],
            error=auth_error,
        )
        return {"ok": False, "error": auth_error}

    assert actor is not None

    def audit(outcome: str, error: str | None = None) -> bool:
        return memory.audit_search(
            actor=actor,
            requested_tenant=tenant,
            effective_tenant=tenant or settings.tenant,
            cluster=cluster,
            namespace=namespace,
            query=query,
            limit=limit,
            cursor=cursor,
            offset=offset,
            outcome=outcome,
            leads=leads,
            error=error,
        )

    def fail(message: str) -> dict[str, Any]:
        audit("rejected", message)
        return {"ok": False, "error": message}

    if limit < 1 or limit > 20:
        return fail("limit must be between 1 and 20")
    if cursor is not None:
        _, separator, offset_text = cursor.partition(":")
        if not separator or not offset_text.isdigit():
            return fail("cursor must be a non-negative integer")
        offset = int(offset_text)
    if tenant is not None:
        tenant = tenant.strip()
        if not tenant:
            return fail("tenant must not be empty")
        if tenant != settings.tenant:
            return fail("tenant is outside the configured server scope")
    query = query.strip()
    if not query:
        return fail("query must not be empty")
    if len(query) > 4000:
        return fail("query must be at most 4000 characters")
    try:
        leads, next_cursor = memory.search_page(
            scope=Scope(
                tenant=tenant or settings.tenant,
                cluster=cluster,
                namespace=namespace,
            ),
            query=query,
            limit=limit,
            cursor=cursor,
        )
    except InvestigatorError as exc:
        return fail(str(exc))
    except ValueError:
        return fail("invalid memory search scope")
    if not audit("ok"):
        return {"ok": False, "error": "memory audit failed"}
    return {
        "ok": True,
        "count": len(leads),
        "leads": [lead.model_dump(mode="json") for lead in leads],
        "rendered": memory.render_leads(leads),
        "policy": "historical_leads_are_not_evidence",
        "used_as_evidence": False,
        "has_more": next_cursor is not None,
        "next_cursor": next_cursor,
    }


def source_status(source: Literal["telemetry", "change", "kubernetes"]) -> dict[str, Any]:
    """Report whether a live source is configured, without inventing evidence."""
    settings = _settings()
    auth_error = _authorize(settings)
    if auth_error is not None:
        return {"ok": False, "error": auth_error}
    if source == "kubernetes":
        return {
            "ok": True,
            "source": source,
            "configured": settings.backend == "kubernetes",
            "backend": settings.backend,
            "read_only": True,
        }
    return {
        "ok": True,
        "source": source,
        "configured": False,
        "status": "abstained",
        "reason": (
            "no live logs, events, or metrics source is configured"
            if source == "telemetry"
            else "no live rollout-history or repository-change source is configured"
        ),
        "evidence_ids": [],
        "read_only": True,
    }


@mcp.tool()
def kube_investigator_health() -> dict[str, Any]:
    """Report the read-only investigation backend and version."""
    settings = _settings()
    return {
        "ok": True,
        "version": __version__,
        "backend": settings.backend,
        "scenario": settings.scenario,
        "reasoner": settings.reasoner,
        "tenant": settings.tenant,
        "cluster": settings.cluster,
        "namespaces": list(settings.namespaces),
        "switch_writes": False,
        "read_only": True,
    }


@mcp.tool()
def kube_investigator_run(
    question: str,
    cluster: str,
    namespace: str,
    service: str,
    deployment: str,
    scenario: Literal["selector-mismatch", "healthy", "ambiguous"] | None = None,
) -> dict[str, Any]:
    """Investigate a Service/Deployment selector mismatch with cited evidence.

    Returns a supported, insufficient_evidence, or partial report. It never
    changes the cluster. The incident commander should publish the result
    through Switch, not through this tool.
    """
    return run_investigation(
        question=question,
        cluster=cluster,
        namespace=namespace,
        service=service,
        deployment=deployment,
        scenario=scenario,
    )


@mcp.tool()
def kube_investigator_get(investigation_id: str) -> dict[str, Any]:
    """Read a persisted investigation and its evidence IDs and digests."""
    return get_investigation(investigation_id)


@mcp.tool()
def kube_investigator_verify(
    investigation_id: str,
    diagnosis: Literal["service_selector_mismatch", "undetermined"],
    evidence_ids: list[str],
) -> dict[str, Any]:
    """Independently verify a proposed diagnosis against persisted evidence."""
    return verify_candidate(investigation_id, diagnosis, evidence_ids)


@mcp.tool()
def kube_investigator_memory(
    cluster: str,
    namespace: str,
    query: str,
    tenant: str | None = None,
    limit: int = 5,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Read approved historical leads. These are never current evidence."""
    return search_memory(
        tenant=tenant,
        cluster=cluster,
        namespace=namespace,
        query=query,
        limit=limit,
        cursor=cursor,
    )


@mcp.tool()
def kube_investigator_source_status(
    source: Literal["telemetry", "change", "kubernetes"],
) -> dict[str, Any]:
    """Report whether a live telemetry, change, or Kubernetes source is configured."""
    return source_status(source)


def main() -> None:
    configure_tracing("kube-investigator-mcp")
    mcp.run()


if __name__ == "__main__":
    main()
