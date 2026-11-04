"""Optional OpenTelemetry tracing for investigation and MCP work.

The API is a no-op unless an SDK provider is installed and an OTLP endpoint is
configured.  That keeps the default offline demo and tests free of exporters
while allowing a Switch Trust / OTel deployment to receive spans.

Standard OpenTelemetry environment variables are used:
``OTEL_EXPORTER_OTLP_ENDPOINT`` and ``OTEL_SERVICE_NAME``.  Install the
optional dependencies with ``pip install -e '.[otel]'``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

_tracer = trace.get_tracer("kube_investigator")
_configured = False


def configure_tracing(service_name: str | None = None) -> bool:
    """Install an OTLP/HTTP exporter when one is configured.

    Returns True when a provider was installed.  Import failures are reported
    as False, not raised: tracing is observability, never a prerequisite for a
    read-only investigation.
    """
    global _configured, _tracer
    if _configured:
        return True
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return False
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        return False
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name
                or os.getenv("OTEL_SERVICE_NAME")
                or "kube-investigator",
            }
        )
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("kube_investigator")
    _configured = True
    return True


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """A span that is a no-op when no provider is configured."""
    with _tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        yield current
