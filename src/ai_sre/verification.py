from __future__ import annotations

from typing import Any

from ai_sre.collectors import query_for
from ai_sre.config import Settings
from ai_sre.models import (
    Candidate,
    Evidence,
    EvidenceKind,
    Investigation,
    Verification,
    VerifiedClaim,
    utc_now,
)


def _matches_labels(selector: dict[str, str], labels: dict[str, str]) -> bool:
    if not selector or not all(isinstance(value, str) for value in selector.values()):
        raise ValueError("Invalid label selector")
    return all(labels.get(key) == value for key, value in selector.items())


def _matches_workload_selector(selector: dict[str, Any], labels: dict[str, str]) -> bool:
    exact = selector.get("matchLabels") or {}
    expressions = selector.get("matchExpressions") or []
    if not exact and not expressions:
        raise ValueError("Empty Deployment selector")
    if exact and not _matches_labels(exact, labels):
        return False
    for expression in expressions:
        key = expression["key"]
        values = expression.get("values") or []
        match expression["operator"]:
            case "In":
                if not values or labels.get(key) not in values:
                    return False
            case "NotIn":
                if not values:
                    raise ValueError("Empty NotIn selector")
                if key in labels and labels[key] in values:
                    return False
            case "Exists":
                if values:
                    raise ValueError("Invalid Exists selector")
                if key not in labels:
                    return False
            case "DoesNotExist":
                if values:
                    raise ValueError("Invalid DoesNotExist selector")
                if key in labels:
                    return False
            case _:
                raise ValueError("Unsupported selector operator")
    return True


def _owned_by(item: dict[str, Any], kind: str, uid: str) -> bool:
    return any(
        owner.get("kind") == kind and owner.get("uid") == uid and owner.get("controller") is True
        for owner in item["metadata"].get("ownerReferences", [])
    )


def _ready(pod: dict[str, Any]) -> bool:
    return (
        not pod["metadata"].get("deletionTimestamp")
        and pod["status"].get("phase") == "Running"
        and any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in pod["status"].get("conditions", [])
        )
    )


class EvidenceVerifier:
    """Rechecks provenance and a fixed proof; never trusts the proposed diagnosis itself."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def verify(
        self, investigation: Investigation, candidate: Candidate, evidence: list[Evidence]
    ) -> Verification:
        errors = self._integrity_errors(investigation, evidence)
        known_ids = {item.id for item in evidence}
        if len(candidate.evidence_ids) != len(set(candidate.evidence_ids)) or not set(
            candidate.evidence_ids
        ).issubset(known_ids):
            errors.append("invalid_citations")
        if errors:
            return Verification(passed=False, checks=sorted(set(errors)))
        if candidate.category == "undetermined":
            return Verification(passed=False, checks=["candidate_undetermined"])
        if set(candidate.evidence_ids) != known_ids:
            return Verification(passed=False, checks=["incomplete_citations"])
        by_kind = {item.kind: item for item in evidence}
        try:
            return self._verify_mismatch(investigation, by_kind)
        except (KeyError, TypeError, ValueError, AttributeError):
            return Verification(passed=False, checks=["invalid_evidence_shape"])

    def _integrity_errors(
        self, investigation: Investigation, evidence: list[Evidence]
    ) -> list[str]:
        errors = []
        if len(evidence) != len(EvidenceKind) or {item.kind for item in evidence} != set(
            EvidenceKind
        ):
            errors.append("missing_or_duplicate_evidence")
        if len({item.id for item in evidence}) != len(evidence):
            errors.append("duplicate_evidence_ids")
        for item in evidence:
            if item.investigation_id != investigation.id or item.scope != investigation.scope:
                errors.append("scope_mismatch")
            if item.sha256 != item.digest():
                errors.append("digest_mismatch")
            if item.source != self.settings.backend:
                errors.append("source_mismatch")
            if item.status != "ok" or item.error_code:
                errors.append(f"collection_error:{item.kind}:{item.error_code or 'unknown'}")
            if not item.query.complete:
                errors.append(f"incomplete_collection:{item.kind}")
            expected = query_for(item.kind, investigation, self.settings.max_resources + 1)
            if (
                item.query.resource != item.kind
                or item.query.verb != expected.verb
                or item.query.name != expected.name
                or item.query.label_selector != expected.label_selector
            ):
                errors.append("query_mismatch")
            if item.collected_at.tzinfo is None:
                errors.append("invalid_timestamp")
            else:
                age = (utc_now() - item.collected_at).total_seconds()
                if (
                    age < -5
                    or age > self.settings.max_evidence_age_seconds
                    or item.collected_at < investigation.created_at
                ):
                    errors.append("stale_or_invalid_timestamp")
        return errors

    def _verify_mismatch(
        self, investigation: Investigation, evidence: dict[EvidenceKind, Evidence]
    ) -> Verification:
        request = investigation.request
        service = evidence[EvidenceKind.SERVICE].content
        deployment = evidence[EvidenceKind.DEPLOYMENT].content
        replicasets = evidence[EvidenceKind.REPLICASETS].content["items"]
        pods = evidence[EvidenceKind.PODS].content["items"]
        slices = evidence[EvidenceKind.ENDPOINTSLICES].content["items"]
        for item in [service, deployment, *replicasets, *pods, *slices]:
            meta = item["metadata"]
            if (
                meta.get("namespace") != request.namespace
                or not meta.get("uid")
                or not meta.get("resourceVersion")
            ):
                return Verification(passed=False, checks=["resource_provenance_invalid"])
        if (
            service["metadata"].get("name") != request.service
            or deployment["metadata"].get("name") != request.deployment
            or service["metadata"].get("deletionTimestamp")
            or deployment["metadata"].get("deletionTimestamp")
        ):
            return Verification(passed=False, checks=["target_resource_invalid"])
        generation = deployment["metadata"].get("generation")
        if (
            not isinstance(generation, int)
            or deployment["status"].get("observedGeneration") != generation
        ):
            return Verification(passed=False, checks=["deployment_generation_not_observed"])
        if service["spec"].get("type") not in {"ClusterIP", "NodePort", "LoadBalancer"}:
            return Verification(passed=False, checks=["unsupported_service_type"])

        selector = service["spec"]["selector"]
        template_labels = deployment["spec"]["template"]["metadata"]["labels"]
        workload_selector = deployment["spec"]["selector"]
        if not _matches_workload_selector(workload_selector, template_labels):
            return Verification(passed=False, checks=["deployment_selector_inconsistent"])
        if _matches_labels(selector, template_labels):
            return Verification(passed=False, checks=["service_selector_matches_template"])
        owned_rs = {
            item["metadata"]["uid"]
            for item in replicasets
            if not item["metadata"].get("deletionTimestamp")
            and _owned_by(item, "Deployment", deployment["metadata"]["uid"])
        }
        ready_owned_pods = [
            pod
            for pod in pods
            if any(_owned_by(pod, "ReplicaSet", uid) for uid in owned_rs)
            and _ready(pod)
            and _matches_workload_selector(workload_selector, pod["metadata"].get("labels", {}))
        ]
        if not ready_owned_pods:
            return Verification(passed=False, checks=["no_ready_deployment_owned_pods"])
        if any(
            _matches_labels(selector, pod["metadata"].get("labels", {})) for pod in ready_owned_pods
        ):
            return Verification(passed=False, checks=["service_selects_ready_workload_pods"])
        for item in slices:
            if item["metadata"].get("labels", {}).get(
                "kubernetes.io/service-name"
            ) != request.service or not _owned_by(item, "Service", service["metadata"]["uid"]):
                return Verification(passed=False, checks=["endpointslice_provenance_invalid"])
            if any(
                endpoint["conditions"].get("ready") is not False for endpoint in item["endpoints"]
            ):
                return Verification(passed=False, checks=["ready_or_unknown_service_endpoints"])

        return Verification(
            passed=True,
            checks=[
                "scope_and_provenance_valid",
                "fresh_complete_snapshots",
                "deployment_owned_pods_ready",
                "selector_excludes_target_workload",
                "no_ready_service_endpoints",
            ],
            claims=[
                VerifiedClaim(
                    text=(
                        f"Service {request.service}'s selector does not match the specified "
                        f"Deployment {request.deployment}'s pod template."
                    ),
                    evidence_ids=[
                        evidence[EvidenceKind.SERVICE].id,
                        evidence[EvidenceKind.DEPLOYMENT].id,
                    ],
                ),
                VerifiedClaim(
                    text=(
                        f"Observed {len(ready_owned_pods)} ready Pod(s) owned by Deployment "
                        f"{request.deployment}; none match the Service selector."
                    ),
                    evidence_ids=[
                        evidence[EvidenceKind.SERVICE].id,
                        evidence[EvidenceKind.DEPLOYMENT].id,
                        evidence[EvidenceKind.REPLICASETS].id,
                        evidence[EvidenceKind.PODS].id,
                    ],
                ),
                VerifiedClaim(
                    text="No ready endpoints were observed in the Service's EndpointSlices.",
                    evidence_ids=[
                        evidence[EvidenceKind.SERVICE].id,
                        evidence[EvidenceKind.ENDPOINTSLICES].id,
                    ],
                ),
            ],
        )
