"""Field allowlists. Never serialize full Kubernetes objects into model context."""

from __future__ import annotations

from typing import Any

from kube_investigator.models import EvidenceKind


def metadata(raw: dict[str, Any]) -> dict[str, Any]:
    source = raw.get("metadata") or {}
    result = {
        key: source[key]
        for key in (
            "name",
            "namespace",
            "uid",
            "resourceVersion",
            "generation",
            "deletionTimestamp",
        )
        if source.get(key) is not None
    }
    result["labels"] = dict(source.get("labels") or {})
    result["ownerReferences"] = [
        {key: owner[key] for key in ("kind", "name", "uid", "controller") if key in owner}
        for owner in source.get("ownerReferences") or []
    ]
    return result


def project(kind: EvidenceKind, raw: dict[str, Any]) -> dict[str, Any]:
    if kind == EvidenceKind.SERVICE:
        spec = raw.get("spec") or {}
        return {
            "metadata": metadata(raw),
            "spec": {
                "selector": spec.get("selector") or {},
                "type": spec.get("type"),
                "publishNotReadyAddresses": spec.get("publishNotReadyAddresses", False),
            },
        }
    if kind == EvidenceKind.DEPLOYMENT:
        spec = raw.get("spec") or {}
        template = (spec.get("template") or {}).get("metadata") or {}
        status = raw.get("status") or {}
        return {
            "metadata": metadata(raw),
            "spec": {
                "replicas": spec.get("replicas"),
                "selector": spec.get("selector") or {},
                "template": {"metadata": {"labels": template.get("labels") or {}}},
            },
            "status": {
                key: status.get(key)
                for key in ("observedGeneration", "replicas", "readyReplicas", "updatedReplicas")
            },
        }
    items = raw.get("items")
    if not isinstance(items, list):
        raise ValueError("Expected a Kubernetes list response")
    result = []
    for item in items:
        entry: dict[str, Any] = {"metadata": metadata(item)}
        if kind == EvidenceKind.PODS:
            status = item.get("status") or {}
            entry["status"] = {
                "phase": status.get("phase"),
                "conditions": [
                    {"type": condition.get("type"), "status": condition.get("status")}
                    for condition in status.get("conditions") or []
                ],
            }
        elif kind == EvidenceKind.ENDPOINTSLICES:
            entry["endpoints"] = [
                {
                    "conditions": {
                        key: (endpoint.get("conditions") or {}).get(key)
                        for key in ("ready", "serving", "terminating")
                    },
                    "targetRef": {
                        key: (endpoint.get("targetRef") or {}).get(key)
                        for key in ("kind", "name", "namespace", "uid")
                    },
                }
                for endpoint in item.get("endpoints") or []
            ]
        result.append(entry)
    return {"items": result}
