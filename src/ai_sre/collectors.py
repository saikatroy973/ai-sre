from __future__ import annotations

import json
from collections.abc import Iterable
from importlib.resources import files
from typing import Any, Protocol

from ai_sre.config import Settings
from ai_sre.errors import ConfigurationError
from ai_sre.models import Evidence, EvidenceKind, Investigation, Query
from ai_sre.projections import project


class Collector(Protocol):
    def collect(self, investigation: Investigation) -> Iterable[Evidence]: ...

    def close(self) -> None: ...


class ReadError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def query_for(kind: EvidenceKind, investigation: Investigation, limit: int) -> Query:
    if kind == EvidenceKind.SERVICE:
        return Query(resource=kind, verb="get", name=investigation.request.service)
    if kind == EvidenceKind.DEPLOYMENT:
        return Query(resource=kind, verb="get", name=investigation.request.deployment)
    return Query(
        resource=kind,
        verb="list",
        limit=limit,
        label_selector=(
            f"kubernetes.io/service-name={investigation.request.service}"
            if kind == EvidenceKind.ENDPOINTSLICES
            else None
        ),
    )


def capture(
    kind: EvidenceKind,
    investigation: Investigation,
    query: Query,
    source: str,
    raw: dict[str, Any] | None,
    *,
    max_resources: int,
    error_code: str | None = None,
) -> Evidence:
    content: dict[str, Any] = {}
    if raw is not None:
        if query.verb == "list":
            items = raw.get("items")
            if not isinstance(items, list):
                raise ValueError("Expected list response")
            truncated = len(items) > max_resources or bool(
                (raw.get("metadata") or {}).get("continue")
            )
            query = query.model_copy(update={"complete": not truncated})
            raw = {**raw, "items": items[:max_resources]}
        content = project(kind, raw)
        if len(json.dumps(content).encode()) > 256_000:
            raise ReadError("evidence_too_large")
    return Evidence.capture(
        investigation_id=investigation.id,
        scope=investigation.scope,
        kind=kind,
        source=source,
        query=query.model_copy(update={"complete": False}) if error_code else query,
        status="error" if error_code else "ok",
        error_code=error_code,
        content=content,
    )


class FixtureCollector:
    def __init__(self, settings: Settings):
        self.max_resources = settings.max_resources
        path = files("ai_sre").joinpath("fixtures", f"{settings.scenario}.json")
        self.fixture = json.loads(path.read_text(encoding="utf-8"))

    def collect(self, investigation: Investigation) -> Iterable[Evidence]:
        request = investigation.request
        matches = self.fixture["target"] == {
            "cluster": request.cluster,
            "namespace": request.namespace,
            "service": request.service,
            "deployment": request.deployment,
        }
        for kind in EvidenceKind:
            query = query_for(kind, investigation, self.max_resources + 1)
            yield capture(
                kind,
                investigation,
                query,
                "fixture",
                self.fixture["resources"][kind] if matches else None,
                max_resources=self.max_resources,
                error_code=None if matches else "not_found",
            )

    def close(self) -> None:
        pass


class KubernetesReader(Protocol):
    def read(self, namespace: str, query: Query) -> dict[str, Any]: ...

    def close(self) -> None: ...


class SDKKubernetesReader:
    """Only these five SDK operations exist; no generic shell or mutation interface."""

    def __init__(self, settings: Settings):
        try:
            from kubernetes import client, config
        except ImportError:
            raise ConfigurationError("Install the kubernetes extra for live reads") from None
        configuration = client.Configuration()
        try:
            config.load_kube_config(
                context=settings.kube_context,
                client_configuration=configuration,
                persist_config=False,
            )
        except Exception:
            raise ConfigurationError(
                "Could not load the explicitly configured kube context"
            ) from None
        if not configuration.verify_ssl or not configuration.host.startswith("https://"):
            raise ConfigurationError("Live Kubernetes reads require HTTPS with TLS verification")
        configuration.retries = 0
        self.api_client = client.ApiClient(configuration)
        self.core = client.CoreV1Api(self.api_client)
        self.apps = client.AppsV1Api(self.api_client)
        self.discovery = client.DiscoveryV1Api(self.api_client)
        self.timeout = (min(3, settings.tool_timeout_seconds), settings.tool_timeout_seconds)

    def read(self, namespace: str, query: Query) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"namespace": namespace, "_request_timeout": self.timeout}
        if query.verb == "get":
            kwargs["name"] = query.name
        else:
            kwargs["limit"] = query.limit
            if query.label_selector:
                kwargs["label_selector"] = query.label_selector
        operations = {
            EvidenceKind.SERVICE: self.core.read_namespaced_service,
            EvidenceKind.DEPLOYMENT: self.apps.read_namespaced_deployment,
            EvidenceKind.REPLICASETS: self.apps.list_namespaced_replica_set,
            EvidenceKind.PODS: self.core.list_namespaced_pod,
            EvidenceKind.ENDPOINTSLICES: self.discovery.list_namespaced_endpoint_slice,
        }
        try:
            result = operations[query.resource](**kwargs)
            return self.api_client.sanitize_for_serialization(result)
        except Exception as exc:
            # Do not propagate API response bodies, headers, URLs, or credentials.
            code = {401: "unauthorized", 403: "forbidden", 404: "not_found"}.get(
                getattr(exc, "status", None), "unavailable"
            )
            raise ReadError(code) from None

    def close(self) -> None:
        self.api_client.close()


class KubernetesCollector:
    def __init__(self, settings: Settings, reader: KubernetesReader | None = None):
        self.settings = settings
        self.reader = reader if reader is not None else SDKKubernetesReader(settings)

    def collect(self, investigation: Investigation) -> Iterable[Evidence]:
        if (
            investigation.scope.cluster != self.settings.cluster
            or investigation.scope.namespace not in self.settings.namespaces
            or investigation.scope.tenant != self.settings.tenant
        ):
            raise ValueError("Collector scope is not authorized")
        for kind in EvidenceKind:
            query = query_for(kind, investigation, self.settings.max_resources + 1)
            try:
                raw = self.reader.read(investigation.scope.namespace, query)
                yield capture(
                    kind,
                    investigation,
                    query,
                    "kubernetes",
                    raw,
                    max_resources=self.settings.max_resources,
                )
            except (ReadError, ValueError, TypeError, KeyError, AttributeError) as exc:
                yield capture(
                    kind,
                    investigation,
                    query,
                    "kubernetes",
                    None,
                    max_resources=self.settings.max_resources,
                    error_code=exc.code if isinstance(exc, ReadError) else "invalid_response",
                )

    def close(self) -> None:
        self.reader.close()
