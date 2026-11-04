from kube_investigator.telemetry import configure_tracing, span


def test_tracing_is_optional_without_an_exporter(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert configure_tracing("test") is False
    with span("investigation.test", investigation_id="test-id") as current:
        assert current is not None
