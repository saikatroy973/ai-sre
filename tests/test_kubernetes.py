from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ai_sre.collectors import (
    FixtureCollector,
    KubernetesCollector,
    ReadError,
    SDKKubernetesReader,
    query_for,
)
from ai_sre.coordinator import Coordinator
from ai_sre.errors import ConfigurationError
from ai_sre.models import EvidenceKind
from ai_sre.reasoners import DeterministicReasoner
from ai_sre.storage import Store


class FakeReader:
    def __init__(self, resources):
        self.resources = deepcopy(resources)
        self.calls = []
        self.error = None

    def read(self, namespace, query):
        self.calls.append((namespace, query))
        if self.error:
            raise self.error
        return deepcopy(self.resources[query.resource])

    def close(self):
        pass


def live_settings(settings):
    return replace(settings, backend="kubernetes", kube_context="explicit-read-only-test")


def test_live_adapter_only_uses_five_bounded_read_operations(settings, investigation):
    resources = FixtureCollector(settings).fixture["resources"]
    reader = FakeReader(resources)
    evidence = list(KubernetesCollector(live_settings(settings), reader).collect(investigation))
    assert len(evidence) == len(reader.calls) == 5
    assert {query.resource for _, query in reader.calls} == set(EvidenceKind)
    assert {namespace for namespace, _ in reader.calls} == {"shop"}
    assert all(query.verb in {"get", "list"} for _, query in reader.calls)
    assert all(query.limit == 101 for _, query in reader.calls if query.verb == "list")
    assert reader.calls[-1][1].label_selector == "kubernetes.io/service-name=checkout"
    assert all(item.source == "kubernetes" for item in evidence)


def test_projection_drops_secrets_annotations_env_and_endpoint_addresses(settings, investigation):
    resources = deepcopy(FixtureCollector(settings).fixture["resources"])
    resources["deployment"]["metadata"]["annotations"] = {"private": "secret-marker"}
    resources["deployment"]["spec"]["template"]["spec"] = {
        "containers": [{"env": [{"name": "PASSWORD", "value": "secret-marker"}]}]
    }
    resources["pods"]["items"][0]["spec"] = {"secret": "secret-marker"}
    resources["endpointslices"]["items"][0]["endpoints"] = [
        {"addresses": ["secret-marker"], "conditions": {"ready": False}}
    ]
    captured = list(
        KubernetesCollector(live_settings(settings), FakeReader(resources)).collect(investigation)
    )
    assert "secret-marker" not in "".join(item.model_dump_json() for item in captured)


def test_paginated_reads_cannot_prove_absence(settings, investigation):
    resources = deepcopy(FixtureCollector(settings).fixture["resources"])
    resources["endpointslices"]["metadata"]["continue"] = "next-page-token"
    captured = list(
        KubernetesCollector(live_settings(settings), FakeReader(resources)).collect(investigation)
    )
    assert captured[-1].query.complete is False
    assert "next-page-token" not in captured[-1].model_dump_json()


def test_oversized_lists_are_truncated_and_marked_incomplete(settings, investigation):
    configured = replace(live_settings(settings), max_resources=1)
    resources = deepcopy(FixtureCollector(settings).fixture["resources"])
    resources["pods"]["items"] *= 2
    captured = list(KubernetesCollector(configured, FakeReader(resources)).collect(investigation))
    pods = next(item for item in captured if item.kind == EvidenceKind.PODS)
    assert len(pods.content["items"]) == 1
    assert pods.query.complete is False


def test_permission_errors_are_evidence_not_a_diagnosis(settings, request_data, principal):
    configured = live_settings(settings)
    reader = FakeReader({})
    reader.error = ReadError("forbidden")
    service = Coordinator(
        configured,
        Store(settings.data_dir),
        KubernetesCollector(configured, reader),
        DeterministicReasoner(),
    )
    result = service.run(request_data, principal)
    assert result.report.outcome == "partial"
    assert result.report.diagnosis == "undetermined"
    assert all(item.error_code == "forbidden" for item in service.store.evidence(result.id))


def test_default_kube_context_is_never_used(settings):
    with pytest.raises(ConfigurationError, match="explicit"):
        replace(settings, backend="kubernetes")


def test_sdk_configuration_failures_do_not_leak_credentials(settings, monkeypatch):
    config = pytest.importorskip("kubernetes.config")

    def fail(**kwargs):
        assert kwargs["context"] == "explicit-read-only-test"
        assert kwargs["persist_config"] is False
        raise RuntimeError("secret-credential-in-config")

    monkeypatch.setattr(config, "load_kube_config", fail)
    with pytest.raises(ConfigurationError) as error:
        SDKKubernetesReader(live_settings(settings))
    assert "secret-credential-in-config" not in str(error.value)


def test_sdk_refuses_insecure_cluster_connections(settings, monkeypatch):
    config = pytest.importorskip("kubernetes.config")

    def insecure(**kwargs):
        kwargs["client_configuration"].host = "http://cluster.example.test"
        kwargs["client_configuration"].verify_ssl = False

    monkeypatch.setattr(config, "load_kube_config", insecure)
    with pytest.raises(ConfigurationError, match="TLS"):
        SDKKubernetesReader(live_settings(settings))


def test_malformed_tool_response_is_an_explicit_collection_error(settings, investigation):
    resources = deepcopy(FixtureCollector(settings).fixture["resources"])
    resources["service"]["metadata"] = "unexpected-response"
    captured = list(
        KubernetesCollector(live_settings(settings), FakeReader(resources)).collect(investigation)
    )
    assert captured[0].status == "error"
    assert captured[0].error_code == "invalid_response"
    assert captured[0].content == {}


def test_sdk_dispatches_only_allowlisted_methods_with_timeouts(settings, investigation):
    calls = []

    def method(name):
        def read(**kwargs):
            calls.append((name, kwargs))
            return {"metadata": {"resourceVersion": "1"}}

        return read

    reader = SDKKubernetesReader.__new__(SDKKubernetesReader)
    reader.timeout = (3, 10)
    reader.api_client = SimpleNamespace(sanitize_for_serialization=lambda value: value)
    reader.core = SimpleNamespace(
        read_namespaced_service=method("service"), list_namespaced_pod=method("pods")
    )
    reader.apps = SimpleNamespace(
        read_namespaced_deployment=method("deployment"),
        list_namespaced_replica_set=method("replicasets"),
    )
    reader.discovery = SimpleNamespace(list_namespaced_endpoint_slice=method("endpointslices"))
    for kind in EvidenceKind:
        reader.read("shop", query_for(kind, investigation, 101))
    assert {name for name, _ in calls} == {kind.value for kind in EvidenceKind}
    assert all(kwargs["_request_timeout"] == (3, 10) for _, kwargs in calls)
    assert all(kwargs["namespace"] == "shop" for _, kwargs in calls)


def test_sdk_api_errors_do_not_expose_the_response_body(settings, investigation):
    reader = SDKKubernetesReader.__new__(SDKKubernetesReader)
    reader.timeout = (3, 10)

    class APIError(Exception):
        status = 403

    def denied(**kwargs):
        raise APIError("credential-and-private-response-body")

    reader.core = SimpleNamespace(read_namespaced_service=denied, list_namespaced_pod=denied)
    reader.apps = SimpleNamespace(
        read_namespaced_deployment=denied, list_namespaced_replica_set=denied
    )
    reader.discovery = SimpleNamespace(list_namespaced_endpoint_slice=denied)
    with pytest.raises(ReadError) as error:
        reader.read("shop", query_for(EvidenceKind.SERVICE, investigation, 101))
    assert str(error.value) == "forbidden"
