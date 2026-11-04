#!/usr/bin/env python3
"""Fail a CI job when Switch Trust has findings at or above a severity.

Read-only: this script calls the Trust API and never writes to Trust.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

SEVERITIES = ("info", "low", "medium", "high", "critical")


def api_base(instance: str) -> str:
    parsed = urlsplit(instance)
    host = parsed.hostname or ""
    if host == "localhost":
        netloc = parsed.netloc
    else:
        head, _, rest = host.partition(".")
        base = rest if head in {"app", "api", "ingestion"} and rest else host
        netloc = f"api.{base}"
    return urlunsplit((parsed.scheme, netloc, "/api/v1", "", ""))


def _get(path: str, token: str, base: str) -> dict:
    request = Request(
        base + path,
        headers={"Authorization": f"ApiKey {token}", "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except HTTPError as exc:
        raise SystemExit(f"Trust API returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, ValueError) as exc:
        raise SystemExit(f"Trust API request failed: {type(exc).__name__}") from None
    if not isinstance(payload, dict):
        raise SystemExit("Trust API returned an invalid response")
    return payload


def finding_severity(row: dict) -> str | None:
    for key in ("severity", "ai_spm_severity", "cvss_v4_severity"):
        value = row.get(key)
        if isinstance(value, str) and value.lower() in SEVERITIES:
            return value.lower()
    return None


def failing_findings(rows: list[dict], threshold: str) -> list[dict]:
    cutoff = SEVERITIES.index(threshold)
    return [
        row
        for row in rows
        if (severity := finding_severity(row)) is None or SEVERITIES.index(severity) >= cutoff
    ]


def main() -> int:
    token = os.getenv("SWITCH_TRUST_TOKEN")
    if not token:
        print("SWITCH_TRUST_TOKEN is required", file=sys.stderr)
        return 1
    threshold = os.getenv("TRUST_GATE_SEVERITY", "high").lower()
    if threshold not in SEVERITIES:
        print(f"TRUST_GATE_SEVERITY must be one of {', '.join(SEVERITIES)}", file=sys.stderr)
        return 1
    base = api_base(os.getenv("SWITCH_TRUST_INSTANCE", "https://app.switchagents.ai"))
    identity = _get("/auth/api-key/identity", token, base)
    tenant = str(identity.get("tenant_id") or "")
    workspace = str(identity.get("workspace_id") or "")
    if not tenant or not workspace:
        print("Trust API key is not scoped to a tenant/workspace", file=sys.stderr)
        return 1
    issues = _get(
        f"/issues/tenants/{quote(tenant, safe='')}/workspaces/{quote(workspace, safe='')}",
        token,
        base,
    )
    rows = issues.get("rows") if isinstance(issues.get("rows"), list) else []
    failing = failing_findings([row for row in rows if isinstance(row, dict)], threshold)
    total = issues.get("total", len(rows))
    print(f"Trust findings gate: threshold={threshold}, total={total}, failing={len(failing)}")
    for row in failing:
        print(
            f"- {row.get('severity') or row.get('ai_spm_severity') or 'unknown'}: "
            f"{row.get('rule_name') or row.get('rule_id') or row.get('issue_id')}"
        )
    return 5 if failing else 0


if __name__ == "__main__":
    raise SystemExit(main())
