"""Read-only live telemetry: Kubernetes pod logs and metrics-server usage.

Both readers use the explicitly configured kube context with TLS verification.
Log lines are bounded and obvious credentials are redacted before they can reach
an agent's context.
"""

from __future__ import annotations

import re
from typing import Any

from kube_investigator.config import Settings
from kube_investigator.errors import ConfigurationError

_SECRET = re.compile(
    r"(?i)\b(password|passwd|token|secret|authorization|api[_-]?key)\b(\s*[=:]\s*)(\S+)"
)


def _client(settings: Settings):
    if settings.backend != "kubernetes" or not settings.kube_context:
        raise ConfigurationError(
            "live telemetry requires INVESTIGATOR_BACKEND=kubernetes and INVESTIGATOR_KUBE_CONTEXT"
        )
    try:
        from kubernetes import client, config
    except ImportError:
        raise ConfigurationError("Install the kubernetes extra for live telemetry") from None
    configuration = client.Configuration()
    try:
        config.load_kube_config(
            context=settings.kube_context,
            client_configuration=configuration,
            persist_config=False,
        )
    except Exception:
        raise ConfigurationError("Could not load the configured kube context") from None
    if not configuration.verify_ssl or not configuration.host.startswith("https://"):
        raise ConfigurationError("Live telemetry requires HTTPS with TLS verification")
    configuration.retries = 0
    api = client.ApiClient(configuration)
    return api, client


def redact(line: str) -> str:
    """Hide obvious credential values while keeping the surrounding text."""
    return _SECRET.sub(r"\1\2[redacted]", line)


def read_logs(
    settings: Settings,
    *,
    namespace: str,
    pod: str | None = None,
    container: str | None = None,
    selector: str | None = None,
    tail_lines: int = 100,
    since_seconds: int = 900,
    previous: bool = False,
) -> dict[str, Any]:
    """Return bounded, redacted log lines for one pod or up to three unhealthy pods."""
    api, client = _client(settings)
    core = client.CoreV1Api(api)
    try:
        if pod:
            try:
                pods = [core.read_namespaced_pod(pod, namespace, _request_timeout=10)]
            except Exception as exc:
                raise ConfigurationError(f"pod {namespace}/{pod} is not readable") from exc
        else:
            kwargs: dict[str, Any] = {
                "namespace": namespace,
                "limit": 50,
                "_request_timeout": 10,
            }
            if selector:
                kwargs["label_selector"] = selector
            try:
                pods = core.list_namespaced_pod(**kwargs).items
            except Exception as exc:
                raise ConfigurationError(f"pods in {namespace} are not readable") from exc
            pods = [
                item
                for item in pods
                if sum(status.restart_count for status in item.status.container_statuses or [])
                or not all(status.ready for status in item.status.container_statuses or [])
            ][:3]
        results: list[dict[str, Any]] = []
        for item in pods:
            names = [entry.name for entry in item.spec.containers]
            chosen = container if container in names else (names[0] if names else None)
            if chosen is None:
                continue
            try:
                text = core.read_namespaced_pod_log(
                    name=item.metadata.name,
                    namespace=namespace,
                    container=chosen,
                    tail_lines=min(max(int(tail_lines), 1), 200),
                    since_seconds=min(max(int(since_seconds), 1), 3600),
                    previous=bool(previous),
                    _request_timeout=15,
                )
            except Exception as exc:
                results.append(
                    {
                        "pod": item.metadata.name,
                        "container": chosen,
                        "error": type(exc).__name__,
                    }
                )
                continue
            lines = [redact(line.rstrip()) for line in str(text).splitlines() if line.strip()][
                -200:
            ]
            results.append({"pod": item.metadata.name, "container": chosen, "lines": lines})
        return {"namespace": namespace, "results": results}
    finally:
        api.close()


def read_metrics(
    settings: Settings,
    *,
    namespace: str | None = None,
    scope: str = "pods",
    pod: str | None = None,
) -> dict[str, Any]:
    """Return metrics-server usage for pods (optionally one namespace/pod) or nodes."""
    api, client = _client(settings)
    custom = client.CustomObjectsApi(api)
    try:
        if scope == "nodes":
            payload = custom.list_cluster_custom_object(
                "metrics.k8s.io", "v1beta1", "nodes", _request_timeout=10
            )
            items = [
                {
                    "node": item["metadata"]["name"],
                    "cpu": (item.get("usage") or {}).get("cpu"),
                    "memory": (item.get("usage") or {}).get("memory"),
                }
                for item in payload.get("items", [])
            ]
            return {"scope": "nodes", "items": items}
        if namespace:
            payload = custom.list_namespaced_custom_object(
                "metrics.k8s.io", "v1beta1", namespace, "pods", _request_timeout=10
            )
        else:
            payload = custom.list_cluster_custom_object(
                "metrics.k8s.io", "v1beta1", "pods", _request_timeout=10
            )
        items = []
        for item in payload.get("items", []):
            name = item["metadata"]["name"]
            if pod and name != pod:
                continue
            items.append(
                {
                    "namespace": item["metadata"]["namespace"],
                    "pod": name,
                    "containers": [
                        {
                            "name": entry.get("name"),
                            "cpu": (entry.get("usage") or {}).get("cpu"),
                            "memory": (entry.get("usage") or {}).get("memory"),
                        }
                        for entry in item.get("containers", [])
                    ],
                }
            )
        return {"scope": "pods", "items": items[:200]}
    except Exception as exc:
        raise ConfigurationError("metrics are unavailable (is metrics-server installed?)") from exc
    finally:
        api.close()
