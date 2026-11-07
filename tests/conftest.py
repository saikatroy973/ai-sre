import socket

import pytest

from ai_sre.auth import Principal
from ai_sre.collectors import FixtureCollector
from ai_sre.config import Settings
from ai_sre.coordinator import build_coordinator
from ai_sre.models import Investigation, InvestigationRequest

API_TOKEN = "test-only-token-not-for-deployment-123456789"


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch):
    for variable in (
        "AI_SRE_BACKEND",
        "AI_SRE_SCENARIO",
        "AI_SRE_DATA_DIR",
        "AI_SRE_EVIDENCE_STORE_DIR",
        "AI_SRE_TENANT",
        "AI_SRE_CLUSTER",
        "AI_SRE_NAMESPACES",
        "AI_SRE_API_TOKEN",
        "AI_SRE_KUBE_CONTEXT",
        "AI_SRE_REASONER",
        "SGP_API_BASE_URL",
        "SGP_API_KEY",
        "SGP_ACCOUNT_ID",
        "SGP_MODEL",
        "AI_SRE_SWITCH_MODE",
        "AI_SRE_SWITCH_BASE_URL",
        "AI_SRE_SWITCH_REGISTRATION_TOKEN",
        "AI_SRE_SWITCH_STATE_DIR",
        "AI_SRE_SWITCH_REQUEST_TIMEOUT_SECONDS",
        "AI_SRE_SWITCH_MEMORY_MAX_RECORDS",
        "AI_SRE_SWITCH_MEMORY_EXPIRY_DAYS",
        "AI_SRE_SWITCH_ROOM_HISTORY_LIMIT",
        "AI_SRE_TRUST_API_KEY",
        "AI_SRE_TRUST_INSTANCE",
        "AI_SRE_TRUST_TENANT_ID",
        "AI_SRE_TRUST_WORKSPACE_ID",
        "AI_SRE_TRUST_TIMEOUT_SECONDS",
        "AI_SRE_MCP_ACTOR",
        "AI_SRE_MCP_ACTOR_TOKEN",
        "AI_SRE_MCP_PUBLIC_KEY",
        "AI_SRE_MCP_TOOLS",
        "AI_SRE_MCP_AUDIT_MAX_BYTES",
        "AI_SRE_DOCS_INDEX",
        "AI_SRE_AUDIT_ANCHOR_DIR",
        "AI_SRE_MEMORY_SEMANTIC_MODEL",
        "AI_SRE_MEMORY_SEMANTIC_THRESHOLD",
        "AI_SRE_CONSOLE_SERVER_URL",
        "AI_SRE_CONSOLE_EMAIL",
        "AI_SRE_CONSOLE_PASSWORD",
        "AI_SRE_CONSOLE_ENV_FILE",
        "AI_SRE_CONSOLE_TIMEOUT_SECONDS",
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
