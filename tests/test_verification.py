from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest

from kube_investigator.models import Candidate, EvidenceKind, utc_now
from kube_investigator.verification import EvidenceVerifier


def candidate_for(evidence):
    return Candidate(
        category="service_selector_mismatch", evidence_ids=[item.id for item in evidence]
    )


def change(evidence, kind, mutate, *, rehash=True):
    changed = []
    for item in evidence:
        if item.kind == kind:
            content = deepcopy(item.content)
            mutate(content)
            item = item.model_copy(update={"content": content})
            if rehash:
                item = item.model_copy(update={"sha256": item.digest()})
        changed.append(item)
    return changed


def test_complete_proof_is_supported(settings, investigation, evidence):
    result = EvidenceVerifier(settings).verify(investigation, candidate_for(evidence), evidence)
    assert result.passed
    assert len(result.claims) == 3


@pytest.mark.parametrize(
    ("kind", "mutate", "expected"),
    [
        (
            EvidenceKind.DEPLOYMENT,
            lambda body: body["status"].update(observedGeneration=0),
            "deployment_generation_not_observed",
        ),
        (
            EvidenceKind.PODS,
            lambda body: body["items"][0]["metadata"].update(ownerReferences=[]),
            "no_ready_deployment_owned_pods",
        ),
        (
            EvidenceKind.REPLICASETS,
            lambda body: body["items"][0]["metadata"]["ownerReferences"][0].update(uid="unrelated"),
            "no_ready_deployment_owned_pods",
        ),
        (
            EvidenceKind.PODS,
            lambda body: body["items"][0]["status"]["conditions"][0].update(status="False"),
            "no_ready_deployment_owned_pods",
        ),
        (
            EvidenceKind.PODS,
            lambda body: body["items"][0]["metadata"].update(namespace="finance"),
            "resource_provenance_invalid",
        ),
        (
            EvidenceKind.SERVICE,
            lambda body: body["spec"].update(selector={"app": "checkout"}),
            "service_selector_matches_template",
        ),
        (
            EvidenceKind.SERVICE,
            lambda body: body["spec"].update(selector={}),
            "invalid_evidence_shape",
        ),
        (
            EvidenceKind.SERVICE,
            lambda body: body["spec"].update(type="ExternalName"),
            "unsupported_service_type",
        ),
        (
            EvidenceKind.SERVICE,
            lambda body: body["metadata"].update(name="different"),
            "target_resource_invalid",
        ),
        (
            EvidenceKind.ENDPOINTSLICES,
            lambda body: body["items"][0]["metadata"]["ownerReferences"][0].update(uid="different"),
            "endpointslice_provenance_invalid",
        ),
        (
            EvidenceKind.ENDPOINTSLICES,
            lambda body: body["items"][0].update(endpoints=[{"conditions": {"ready": True}}]),
            "ready_or_unknown_service_endpoints",
        ),
        (
            EvidenceKind.ENDPOINTSLICES,
            lambda body: body["items"][0].update(endpoints=[{"conditions": {"ready": None}}]),
            "ready_or_unknown_service_endpoints",
        ),
    ],
)
def test_bad_proofs_are_rejected(settings, investigation, evidence, kind, mutate, expected):
    evidence = change(evidence, kind, mutate)
    result = EvidenceVerifier(settings).verify(investigation, candidate_for(evidence), evidence)
    assert result.passed is False
    assert expected in result.checks
    assert result.claims == []


def test_tampered_evidence_is_not_trusted(settings, investigation, evidence):
    evidence = change(
        evidence,
        EvidenceKind.SERVICE,
        lambda body: body["spec"].update(type="NodePort"),
        rehash=False,
    )
    result = EvidenceVerifier(settings).verify(investigation, candidate_for(evidence), evidence)
    assert result.passed is False
    assert "digest_mismatch" in result.checks


@pytest.mark.parametrize(
    ("field", "replacement", "expected"),
    [
        ("investigation_id", uuid4(), "scope_mismatch"),
        ("source", "kubernetes", "source_mismatch"),
        ("collected_at", utc_now() - timedelta(minutes=10), "stale_or_invalid_timestamp"),
        ("collected_at", utc_now() + timedelta(minutes=10), "stale_or_invalid_timestamp"),
    ],
)
def test_provenance_is_rechecked(settings, investigation, evidence, field, replacement, expected):
    item = evidence[0].model_copy(update={field: replacement})
    item = item.model_copy(update={"sha256": item.digest()})
    evidence = [item, *evidence[1:]]
    result = EvidenceVerifier(settings).verify(investigation, candidate_for(evidence), evidence)
    assert result.passed is False
    assert expected in result.checks


def test_wrong_tenant_is_rejected(settings, investigation, evidence):
    item = evidence[0].model_copy(
        update={"scope": evidence[0].scope.model_copy(update={"tenant": "other-tenant"})}
    )
    evidence[0] = item.model_copy(update={"sha256": item.digest()})
    result = EvidenceVerifier(settings).verify(investigation, candidate_for(evidence), evidence)
    assert "scope_mismatch" in result.checks


@pytest.mark.parametrize("citation_case", ["fabricated", "missing", "duplicate"])
def test_citations_are_not_just_decorative(settings, investigation, evidence, citation_case):
    ids = [item.id for item in evidence]
    if citation_case == "fabricated":
        ids[0] = uuid4()
    elif citation_case == "missing":
        ids.pop()
    else:
        ids[0] = ids[1]
    candidate = Candidate(category="service_selector_mismatch", evidence_ids=ids)
    result = EvidenceVerifier(settings).verify(investigation, candidate, evidence)
    assert result.passed is False
    assert result.claims == []


def test_missing_collection_is_rejected(settings, investigation, evidence):
    candidate = candidate_for(evidence)
    result = EvidenceVerifier(settings).verify(investigation, candidate, evidence[:-1])
    assert "missing_or_duplicate_evidence" in result.checks


def test_truncated_collection_cannot_prove_absence(settings, investigation, evidence):
    item = evidence[0].model_copy(
        update={"query": evidence[0].query.model_copy(update={"complete": False})}
    )
    evidence[0] = item.model_copy(update={"sha256": item.digest()})
    result = EvidenceVerifier(settings).verify(investigation, candidate_for(evidence), evidence)
    assert "incomplete_collection:service" in result.checks


def test_wrong_tool_query_is_rejected(settings, investigation, evidence):
    item = evidence[0].model_copy(
        update={"query": evidence[0].query.model_copy(update={"name": "unrelated"})}
    )
    evidence[0] = item.model_copy(update={"sha256": item.digest()})
    result = EvidenceVerifier(settings).verify(investigation, candidate_for(evidence), evidence)
    assert "query_mismatch" in result.checks
