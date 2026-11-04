from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from kube_investigator.api import create_app
from kube_investigator.errors import ConfigurationError


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as connection:
        yield connection


@pytest.fixture
def headers(settings):
    return {"Authorization": f"Bearer {settings.api_token}"}


def test_api_fails_closed_without_a_strong_token(settings):
    for token in (None, "short"):
        with pytest.raises(ConfigurationError, match="API_TOKEN"):
            create_app(replace(settings, api_token=token))


def test_health_is_minimal_and_interactive_docs_are_disabled(client):
    assert client.get("/health").json() == {"status": "ok"}
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_all_investigation_routes_require_authentication(client, request_data):
    assert client.post("/v1/investigations", json=request_data.model_dump()).status_code == 401
    for suffix in ("", "/messages", "/evidence"):
        path = f"/v1/investigations/{uuid4()}{suffix}"
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_api_returns_persisted_report_handoffs_and_evidence(
    client, headers, request_data, settings
):
    response = client.post("/v1/investigations", headers=headers, json=request_data.model_dump())
    assert response.status_code == 201
    result = response.json()
    assert result["report"]["outcome"] == "supported"
    path = f"/v1/investigations/{result['id']}"
    assert client.get(path, headers=headers).json() == result
    messages = client.get(path + "/messages", headers=headers).json()
    evidence = client.get(path + "/evidence", headers=headers).json()
    assert len(messages) == len(evidence) == 5
    assert {message["trace_id"] for message in messages} == {result["trace_id"]}
    assert settings.api_token not in response.text
    assert client.get(f"/v1/investigations/{uuid4()}", headers=headers).status_code == 404


def test_api_scope_cannot_be_changed_by_request(client, headers, request_data):
    payload = {**request_data.model_dump(), "namespace": "finance"}
    response = client.post("/v1/investigations", headers=headers, json=payload)
    assert response.status_code == 403
    assert client.app.state.coordinator.store.path.exists()
    with client.app.state.coordinator.store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM investigations").fetchone()[0] == 0


@pytest.mark.parametrize(
    "extra",
    [
        {"tenant": "other-tenant"},
        {"kube_context": "admin-context"},
        {"api_key": "do-not-echo-this-secret"},
        {"service": "checkout; kubectl delete pods --all"},
        {"question": " "},
    ],
)
def test_invalid_requests_are_rejected_without_echoing_inputs(client, headers, request_data, extra):
    response = client.post(
        "/v1/investigations", headers=headers, json={**request_data.model_dump(), **extra}
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid investigation request"}


def test_request_body_is_bounded_even_without_content_length(client, headers):
    body = b'{"question":"' + b"x" * 9000 + b'"}'
    response = client.post(
        "/v1/investigations",
        headers={**headers, "Content-Type": "application/json"},
        content=iter([body[:4500], body[4500:]]),
    )
    assert response.status_code == 413


def test_api_capacity_is_explicit(client, headers, request_data):
    service = client.app.state.coordinator
    for _ in range(service.settings.max_concurrent_investigations):
        service.slots.acquire()
    try:
        response = client.post(
            "/v1/investigations", headers=headers, json=request_data.model_dump()
        )
        assert response.status_code == 429
        assert response.headers["retry-after"] == "1"
    finally:
        for _ in range(service.settings.max_concurrent_investigations):
            service.slots.release()


def test_api_hides_another_operators_record(client, headers, request_data, settings):
    from kube_investigator.auth import Principal

    service = client.app.state.coordinator
    record = service.run(request_data, Principal.operator(settings, subject="different-owner"))
    for suffix in ("", "/messages", "/evidence"):
        response = client.get(f"/v1/investigations/{record.id}{suffix}", headers=headers)
        assert response.status_code == 404
