"""Read-only Switch Trust and ROI summary.

Switch Trust is a separate product from the Switch room server.  This module is
an intentionally small, read-only client for governance evidence: the API key's
identity, evaluation health, guardrail policies, and ROI cost summaries.  It
never sends prompts, evidence, or room content to Trust.

The client is optional.  When it is not configured it reports that plainly;
commands that claim a Trust or ROI result fail closed instead of inventing one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from kube_investigator.errors import ConfigurationError, SwitchError

ALLOWED_TRUST_HOSTS = ("switchagents.ai",)
_EDGE_LABELS = ("app", "api", "ingestion")
_LOCALHOST = "localhost"
API_PREFIX = "/api/v1"
ROI_ANALYSIS_TYPES = (
    "tool_bloat",
    "model_optimizer",
    "verbosity",
    "context_cache_waste",
    "retry_loop_waste",
)


def validate_instance(value: str | None) -> str:
    """Return a canonical Switch Trust instance origin, or fail closed.

    Production credentials are only sent to ``switchagents.ai`` over HTTPS.
    ``http://localhost:PORT`` is the single cleartext exception, for a local
    backend.  User info, paths, queries, and fragments are rejected so the URL
    validated here is exactly the origin the HTTP client uses.
    """
    if not value:
        raise ConfigurationError("INVESTIGATOR_TRUST_INSTANCE must be a Switch Trust origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError(
            "INVESTIGATOR_TRUST_INSTANCE must be a valid Switch Trust origin"
        ) from exc
    host = parsed.hostname
    if (
        not host
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError("INVESTIGATOR_TRUST_INSTANCE must be an origin with no path")
    netloc = host if port is None else f"{host}:{port}"
    canonical = urlunsplit((parsed.scheme, netloc, "", "", ""))
    if parsed.scheme == "http" and host == _LOCALHOST:
        return canonical
    if parsed.scheme != "https":
        raise ConfigurationError("INVESTIGATOR_TRUST_INSTANCE must use HTTPS")
    if not any(host == allowed or host.endswith(f".{allowed}") for allowed in ALLOWED_TRUST_HOSTS):
        raise ConfigurationError(
            "INVESTIGATOR_TRUST_INSTANCE must be a switchagents.ai host or http://localhost"
        )
    return canonical


def derive_api_base(instance: str) -> str:
    """Map a web-app instance URL to the REST API base, including ``/api/v1``.

    The Switch Trust OpenAPI document declares ``https://{host}/api/v1`` as
    the server URL.  The browser app lives on ``app.switchagents.ai``; the API
    is ``api.switchagents.ai/api/v1``.
    """
    parsed = urlsplit(instance)
    host = parsed.hostname or ""
    if host == _LOCALHOST:
        netloc = parsed.netloc
    else:
        head, _, rest = host.partition(".")
        base = rest if head in _EDGE_LABELS and rest else host
        netloc = f"api.{base}" if parsed.port is None else f"api.{base}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, API_PREFIX, "", ""))


@dataclass(frozen=True)
class TrustSettings:
    api_key: str = field(repr=False)
    instance: str = "https://app.switchagents.ai"
    tenant_id: str | None = None
    workspace_id: str | None = None
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ConfigurationError("INVESTIGATOR_TRUST_API_KEY is required")
        object.__setattr__(self, "instance", validate_instance(self.instance))
        if self.timeout_seconds <= 0:
            raise ConfigurationError("INVESTIGATOR_TRUST_TIMEOUT_SECONDS must be positive")
        for name, value in (
            ("INVESTIGATOR_TRUST_TENANT_ID", self.tenant_id),
            ("INVESTIGATOR_TRUST_WORKSPACE_ID", self.workspace_id),
        ):
            if value is not None and not value.strip():
                raise ConfigurationError(f"{name} cannot be empty")

    @property
    def api_base_url(self) -> str:
        return derive_api_base(self.instance)

    @classmethod
    def from_env(cls, *, required: bool = False) -> TrustSettings | None:
        api_key = os.getenv("INVESTIGATOR_TRUST_API_KEY")
        if not api_key:
            if required:
                raise ConfigurationError(
                    "Switch Trust is not configured; set INVESTIGATOR_TRUST_API_KEY"
                )
            return None
        raw_timeout = os.getenv("INVESTIGATOR_TRUST_TIMEOUT_SECONDS", "15")
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise ConfigurationError("INVESTIGATOR_TRUST_TIMEOUT_SECONDS must be a number") from exc
        return cls(
            api_key=api_key,
            instance=os.getenv("INVESTIGATOR_TRUST_INSTANCE", "https://app.switchagents.ai"),
            tenant_id=os.getenv("INVESTIGATOR_TRUST_TENANT_ID") or None,
            workspace_id=os.getenv("INVESTIGATOR_TRUST_WORKSPACE_ID") or None,
            timeout_seconds=timeout,
        )


class TrustClient:
    """A read-only, sanitized client for the Switch Trust platform API."""

    def __init__(self, settings: TrustSettings, *, http: httpx.Client | None = None):
        self.settings = settings
        self._http = http or httpx.Client(
            base_url=settings.api_base_url,
            headers={
                "Authorization": f"ApiKey {settings.api_key}",
                "Accept": "application/json",
            },
            timeout=settings.timeout_seconds,
            follow_redirects=False,
        )
        self._identity: dict[str, Any] | None = None

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise SwitchError("Switch Trust is unavailable") from exc
        if response.status_code in {401, 403}:
            raise SwitchError("Switch Trust rejected the API key")
        if response.status_code == 404:
            raise SwitchError("Switch Trust could not find the requested governance resource")
        if response.status_code == 429:
            raise SwitchError("Switch Trust rate limit exceeded")
        if response.status_code >= 400:
            raise SwitchError("Switch Trust request failed")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SwitchError("Switch Trust returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise SwitchError("Switch Trust returned an invalid response")
        return payload

    def identity(self) -> dict[str, Any]:
        if self._identity is None:
            payload = self._get("/auth/api-key/identity")
            if not payload.get("tenant_id") or not payload.get("workspace_id"):
                raise SwitchError("Switch Trust API key is not scoped to a tenant and workspace")
            self._identity = payload
        return self._identity

    def scope(self) -> tuple[str, str]:
        identity = self.identity()
        tenant = identity["tenant_id"]
        workspace = identity["workspace_id"]
        for name, configured, actual in (
            ("INVESTIGATOR_TRUST_TENANT_ID", self.settings.tenant_id, tenant),
            ("INVESTIGATOR_TRUST_WORKSPACE_ID", self.settings.workspace_id, workspace),
        ):
            if configured and configured != actual:
                raise ConfigurationError(
                    f"{name} does not match the tenant/workspace this API key is pinned to"
                )
        return tenant, workspace

    def evaluation_summary(self, agent_id: str) -> dict[str, Any]:
        tenant, workspace = self.scope()
        payload = self._get(
            f"/eval/agent-evaluation-summary/tenants/{quote(tenant, safe='')}"
            f"/workspaces/{quote(workspace, safe='')}",
            params={"agent_id": agent_id},
        )
        health = payload.get("health") if isinstance(payload.get("health"), dict) else {}
        return {
            "agent_id": agent_id,
            "score": health.get("score"),
            "previous_score": health.get("previous_score"),
            "coverage": payload.get("coverage", []),
            "trend": payload.get("trend", {}),
        }

    def _inventory_counts(self, tenant: str, workspace: str) -> dict[str, int]:
        def total(path: str) -> int:
            payload = self._get(path)
            value = payload.get("total")
            return value if isinstance(value, int) else 0

        prefix = f"/tenants/{quote(tenant, safe='')}/workspaces/{quote(workspace, safe='')}"
        return {
            "agents": total(f"/aispm-agents{prefix}"),
            "mcp_servers": total(f"/aispm-mcp-servers{prefix}"),
            "models": total(f"/aispm-models{prefix}"),
            "tools": total(f"/aispm-tools{prefix}"),
            "issues": total(f"/issues{prefix}"),
        }

    def governance_summary(self, *, agent_ids: tuple[str, ...] = ()) -> dict[str, Any]:
        tenant, workspace = self.scope()
        cost = self._get(
            f"/aispm-agents-dashboard/tenants/{quote(tenant, safe='')}"
            f"/workspaces/{quote(workspace, safe='')}"
        )
        activity = self._get(
            f"/roi/agents/tenants/{quote(tenant, safe='')}/workspaces/{quote(workspace, safe='')}",
            params={"page_size": 100},
        )
        policies = self._get(
            f"/guardrails/policies/tenants/{quote(tenant, safe='')}"
            f"/workspaces/{quote(workspace, safe='')}",
            params={"page_size": 100},
        )
        outcomes = self._get(
            f"/aispm-llm-interactions-dash/tenants/{quote(tenant, safe='')}"
            f"/workspaces/{quote(workspace, safe='')}/outcomes"
        )
        evaluations = [self.evaluation_summary(agent_id) for agent_id in agent_ids]
        policy_items = policies.get("items") if isinstance(policies.get("items"), list) else []
        return {
            "trust_base_url": self.settings.api_base_url,
            "identity": {
                "key_scope": self._identity.get("key_scope") if self._identity else None,
                "org_id": self._identity.get("org_id") if self._identity else None,
                "tenant_id": tenant,
                "workspace_id": workspace,
            },
            "cost": {
                "total_mtd_cost": cost.get("total_mtd_cost"),
                "total_cost_cap": cost.get("total_cost_cap"),
            },
            "inventory": self._inventory_counts(tenant, workspace),
            "roi_agents": activity.get("items", []),
            "roi_agent_count": activity.get("total_count"),
            "guardrails": {
                "policy_count": policies.get("total_count"),
                "policies": [
                    {"id": item.get("id"), "name": item.get("name")}
                    for item in policy_items
                    if isinstance(item, dict)
                ],
                "outcomes": {
                    key: outcomes.get(key)
                    for key in ("ok", "alerted", "blocked", "redacted", "errored", "total")
                },
            },
            "evaluations": evaluations,
            "advisory": (
                "Switch ROI caps are advisory and do not stop spending. "
                "Infrastructure cost attribution is external to Switch ROI."
            ),
        }

    def roi_summary(self, *, agent_id: str | None = None) -> dict[str, Any]:
        tenant, workspace = self.scope()
        cost = self._get(
            f"/aispm-agents-dashboard/tenants/{quote(tenant, safe='')}"
            f"/workspaces/{quote(workspace, safe='')}"
        )
        activity = self._get(
            f"/roi/agents/tenants/{quote(tenant, safe='')}/workspaces/{quote(workspace, safe='')}",
            params={"page_size": 100},
        )
        analyses: dict[str, Any] = {}
        if agent_id:
            analyses = self._get(
                f"/roi/analyses/latest/tenants/{quote(tenant, safe='')}"
                f"/workspaces/{quote(workspace, safe='')}",
                params={
                    "agent_id": agent_id,
                    "analysis_types": ",".join(ROI_ANALYSIS_TYPES),
                },
            )
        return {
            "total_mtd_cost": cost.get("total_mtd_cost"),
            "total_cost_cap": cost.get("total_cost_cap"),
            "agents": activity.get("items", []),
            "agent_count": activity.get("total_count"),
            "agent_id": agent_id,
            "latest_analyses": analyses,
            "advisory": (
                "Switch ROI caps are advisory and do not stop spending. "
                "Infrastructure cost attribution is external to Switch ROI."
            ),
        }


def render_governance_summary(summary: dict[str, Any]) -> str:
    """Render a Trust summary as a room document that cites what was read."""
    identity = summary.get("identity") or {}
    cost = summary.get("cost") or {}
    inventory = summary.get("inventory") or {}
    guardrails = summary.get("guardrails") or {}
    lines = [
        "# Switch Trust governance summary",
        "",
        f"Trust API: {summary.get('trust_base_url')}",
        f"Tenant: {identity.get('tenant_id')}",
        f"Workspace: {identity.get('workspace_id')}",
        f"Key scope: {identity.get('key_scope') or 'not reported'}",
        "",
        "## Cost (Switch ROI)",
        f"- Month-to-date: {cost.get('total_mtd_cost')}",
        f"- Cost cap: {cost.get('total_cost_cap')}",
        f"- Advisory note: {summary.get('advisory')}",
        "",
        "## Inventory",
        f"- Agents: {inventory.get('agents')}",
        f"- MCP servers: {inventory.get('mcp_servers')}",
        f"- Models: {inventory.get('models')}",
        f"- Tools: {inventory.get('tools')}",
        f"- Issues: {inventory.get('issues')}",
        "",
        "## Guardrails",
        f"- Policies: {guardrails.get('policy_count')}",
    ]
    outcomes = guardrails.get("outcomes") or {}
    lines.append(
        "- Outcomes: "
        + ", ".join(
            f"{key}={outcomes.get(key)}"
            for key in ("ok", "alerted", "blocked", "redacted", "errored", "total")
        )
    )
    for policy in guardrails.get("policies", []):
        lines.append(f"- {policy.get('name')} ({policy.get('id')})")
    lines.extend(["", "## Evaluations"])
    evaluations = summary.get("evaluations") or []
    if not evaluations:
        lines.append("- No agent IDs were supplied, so no evaluation health was read.")
    for evaluation in evaluations:
        lines.append(
            f"- {evaluation.get('agent_id')}: score={evaluation.get('score')} "
            f"previous={evaluation.get('previous_score')}"
        )
    lines.extend(
        [
            "",
            "Read-only: this document records Trust and ROI API results. It does not "
            "claim that a gate passed, and it does not stop spending.",
        ]
    )
    return "\n".join(lines) + "\n"
