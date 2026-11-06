import socket

import pytest

from kube_investigator.auth import Principal
from kube_investigator.collectors import FixtureCollector
from kube_investigator.config import Settings
from kube_investigator.coordinator import build_coordinator
from kube_investigator.models import Investigation, InvestigationRequest

API_TOKEN = "test-only-token-not-for-deployment-123456789"


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch):
    for variable in (
        "INVESTIGATOR_BACKEND",
        "INVESTIGATOR_SCENARIO",
        "INVESTIGATOR_DATA_DIR",
        "INVESTIGATOR_EVIDENCE_STORE_DIR",
        "INVESTIGATOR_TENANT",
        "INVESTIGATOR_CLUSTER",
        "INVESTIGATOR_NAMESPACES",
        "INVESTIGATOR_API_TOKEN",
        "INVESTIGATOR_KUBE_CONTEXT",
        "INVESTIGATOR_REASONER",
        "SGP_API_BASE_URL",
        "SGP_API_KEY",
        "SGP_ACCOUNT_ID",
        "SGP_MODEL",
        "INVESTIGATOR_SWITCH_MODE",
        "INVESTIGATOR_SWITCH_BASE_URL",
        "INVESTIGATOR_SWITCH_REGISTRATION_TOKEN",
        "INVESTIGATOR_SWITCH_STATE_DIR",
        "INVESTIGATOR_SWITCH_REQUEST_TIMEOUT_SECONDS",
        "INVESTIGATOR_SWITCH_MEMORY_MAX_RECORDS",
        "INVESTIGATOR_SWITCH_MEMORY_EXPIRY_DAYS",
        "INVESTIGATOR_SWITCH_ROOM_HISTORY_LIMIT",
        "INVESTIGATOR_TRUST_API_KEY",
        "INVESTIGATOR_TRUST_INSTANCE",
        "INVESTIGATOR_TRUST_TENANT_ID",
        "INVESTIGATOR_TRUST_WORKSPACE_ID",
        "INVESTIGATOR_TRUST_TIMEOUT_SECONDS",
        "INVESTIGATOR_MCP_ACTOR",
        "INVESTIGATOR_MCP_ACTOR_TOKEN",
        "INVESTIGATOR_MCP_PUBLIC_KEY",
        "INVESTIGATOR_MCP_TOOLS",
        "INVESTIGATOR_MCP_AUDIT_MAX_BYTES",
        "INVESTIGATOR_DOCS_INDEX",
        "INVESTIGATOR_AUDIT_ANCHOR_DIR",
        "INVESTIGATOR_MEMORY_SEMANTIC_MODEL",
        "INVESTIGATOR_MEMORY_SEMANTIC_THRESHOLD",
        "INVESTIGATOR_CONSOLE_SERVER_URL",
        "INVESTIGATOR_CONSOLE_EMAIL",
        "INVESTIGATOR_CONSOLE_PASSWORD",
        "INVESTIGATOR_CONSOLE_ENV_FILE",
        "INVESTIGATOR_CONSOLE_TIMEOUT_SECONDS",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_SERVICE_NAME",
    ):
        monkeypatch.delenv(variable, raising=False)

    def deny_network(*args, **kwargs):
        raise AssertionError("Tests must not make network connections")

    monkeypatch.setattr(socket.socket, "connect", deny_network)


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", api_token=API_TOKEN)


@pytest.fixture
def principal(settings):
    return Principal.operator(settings)


@pytest.fixture
def request_data():
    return InvestigationRequest(
        question="Why is checkout failing?",
        cluster="demo-cluster",
        namespace="shop",
        service="checkout",
        deployment="checkout",
    )


@pytest.fixture
def investigation(request_data, principal):
    return Investigation(
        principal_id=principal.id,
        scope=principal.authorize(request_data),
        request=request_data,
    )


@pytest.fixture
def evidence(settings, investigation):
    return list(FixtureCollector(settings).collect(investigation))


@pytest.fixture
def coordinator(settings):
    service = build_coordinator(settings)
    try:
        yield service
    finally:
        service.close()
