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
    """Read back a persisted investigation and its evidence metadata."""
    settings = _settings()
    try:
        started = UUID(investigation_id)
    except ValueError:
        return {"ok": False, "error": "investigation_id must be a UUID"}
    store = Store(settings.data_dir)
    try:
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
    try:
        started = UUID(investigation_id)
        candidate = Candidate(
            category=diagnosis,
            evidence_ids=[UUID(value) for value in evidence_ids],
        )
    except (ValueError, TypeError):
        return {"ok": False, "error": "invalid investigation or evidence id"}
    store = Store(settings.data_dir)
    try:
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
) -> dict[str, Any]:
    """Search human-approved historical leads for this exact scope.

    Results are leads, not evidence.  They are never passed to the reasoner.
    Omit tenant to use the configured server tenant.
    """
    if limit < 1 or limit > 20:
        return {"ok": False, "error": "limit must be between 1 and 20"}
    settings = _settings()
    if tenant is not None:
        tenant = tenant.strip()
        if not tenant:
            return {"ok": False, "error": "tenant must not be empty"}
        if tenant != settings.tenant:
            return {"ok": False, "error": "tenant is outside the configured server scope"}
    query = query.strip()
    if not query:
        return {"ok": False, "error": "query must not be empty"}
    if len(query) > 4000:
        return {"ok": False, "error": "query must be at most 4000 characters"}
    store = Store(settings.data_dir)
    memory = MemoryStore(store)
    try:
        leads = memory.search(
            scope=Scope(
                tenant=tenant or settings.tenant,
                cluster=cluster,
                namespace=namespace,
            ),
            query=query,
            limit=limit,
        )
    except InvestigatorError as exc:
        return _error(exc)
    except ValueError:
        return {"ok": False, "error": "invalid memory search scope"}
    return {
        "ok": True,
        "count": len(leads),
        "leads": [lead.model_dump(mode="json") for lead in leads],
        "rendered": memory.render_leads(leads),
        "policy": "historical_leads_are_not_evidence",
        "used_as_evidence": False,
    }


def source_status(source: Literal["telemetry", "change", "kubernetes"]) -> dict[str, Any]:
    """Report whether a live source is configured, without inventing evidence."""
    settings = _settings()
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
) -> dict[str, Any]:
    """Read approved historical leads. These are never current evidence."""
    return search_memory(
        tenant=tenant,
        cluster=cluster,
        namespace=namespace,
        query=query,
        limit=limit,
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
