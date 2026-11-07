import json
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from ai_sre.coordinator import Coordinator
from ai_sre.errors import ConfigurationError, ReasoningError
from ai_sre.models import Candidate
from ai_sre.reasoners import ScaleReasoner


def scale_settings(settings):
    return replace(
        settings,
        reasoner="scale",
        sgp_api_base_url="https://scale.example.test/public",
        sgp_api_key="provider-secret-never-persist",
        sgp_account_id="test-account",
        sgp_model="openai/enabled-test-model",
    )


def response_body(evidence, **overrides):
    candidate = Candidate(
        category="service_selector_mismatch", evidence_ids=[item.id for item in evidence]
    )
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": candidate.model_dump_json()}}],
        "usage": {
            "prompt_tokens": 500,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 100},
        },
        **overrides,
    }


def test_scale_adapter_uses_real_contract_and_bounded_request(settings, request_data, evidence):
    configured = scale_settings(settings)
    calls = []

    def handler(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert str(request.url) == "https://scale.example.test/public/v5/chat/completions"
        assert request.headers["x-api-key"] == configured.sgp_api_key
        assert request.headers["x-selected-account-id"] == "test-account"
        assert payload["model"] == configured.sgp_model
        assert payload["max_completion_tokens"] == configured.max_model_output_tokens
        assert payload["response_format"] == {"type": "json_object"}
        assert configured.sgp_api_key not in request.content.decode()
        return httpx.Response(200, json=response_body(evidence))

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = ScaleReasoner(configured, http).propose(request_data, evidence)
    assert len(calls) == 1
    assert result.candidate.category == "service_selector_mismatch"
    assert result.usage.input_tokens == 500
    assert result.usage.output_tokens == 50
    assert result.usage.cached_input_tokens == 100


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"category":"memory_leak","evidence_ids":[]}',
        '{"category":"undetermined","evidence_ids":[],"summary":"invented claim"}',
    ],
)
def test_model_cannot_expand_the_diagnosis_or_publish_free_text(
    settings, request_data, evidence, content
):
    body = response_body(
        evidence, choices=[{"finish_reason": "stop", "message": {"content": content}}]
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as http:
        with pytest.raises(ReasoningError, match="model_invalid_response") as error:
            ScaleReasoner(scale_settings(settings), http).propose(request_data, evidence)
    assert error.value.usage.input_tokens == 500


def test_model_error_is_partial_not_silent_deterministic_fallback(
    coordinator, settings, request_data, principal
):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(500, text="provider-secret-never-persist")
        )
    ) as http:
        configured = scale_settings(settings)
        service = Coordinator(
            configured, coordinator.store, coordinator.collector, ScaleReasoner(configured, http)
        )
        result = service.run(request_data, principal)
    assert result.report.outcome == "partial"
    assert result.report.diagnosis == "undetermined"
    assert result.report.verification.claims == []
    records = result.model_dump_json() + "".join(
        message.model_dump_json() for message in service.store.messages(result.id)
    )
    assert "provider-secret-never-persist" not in records
    assert "model_unavailable" in records


def test_redirects_do_not_forward_credentials(settings, request_data, evidence):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "https://untrusted.example.test"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as http:
        with pytest.raises(ReasoningError, match="model_unavailable"):
            ScaleReasoner(scale_settings(settings), http).propose(request_data, evidence)
    assert len(calls) == 1


def test_truncated_model_output_cannot_be_used(settings, request_data, evidence):
    body = response_body(
        evidence, choices=[{"finish_reason": "length", "message": {"content": "{}"}}]
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as http:
        with pytest.raises(ReasoningError, match="model_incomplete_response"):
            ScaleReasoner(scale_settings(settings), http).propose(request_data, evidence)


def test_unknown_usage_is_not_reported_as_free(settings, request_data, evidence):
    body = response_body(evidence, usage=None)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as http:
        proposal = ScaleReasoner(scale_settings(settings), http).propose(request_data, evidence)
    assert proposal.usage.input_tokens is None
    assert proposal.usage.output_tokens is None


def test_oversized_response_is_rejected(settings, request_data, evidence):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 65_000))
    ) as http:
        with pytest.raises(ReasoningError, match="model_response_limit"):
            ScaleReasoner(scale_settings(settings), http).propose(request_data, evidence)


def test_timeout_does_not_expose_provider_details(settings, request_data, evidence):
    def timeout(request):
        raise httpx.ReadTimeout("private-provider-url-and-secret", request=request)

    with httpx.Client(transport=httpx.MockTransport(timeout)) as http:
        with pytest.raises(ReasoningError) as error:
            ScaleReasoner(scale_settings(settings), http).propose(request_data, evidence)
    assert str(error.value) == "model_unavailable"


def test_slow_response_has_a_wall_clock_budget(settings, request_data, evidence, monkeypatch):
    times = iter([0.0, settings.model_timeout_seconds + 1.0])
    monkeypatch.setattr("ai_sre.reasoners.monotonic", lambda: next(times))
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response_body(evidence)))
    ) as http:
        with pytest.raises(ReasoningError, match="model_deadline_exceeded"):
            ScaleReasoner(scale_settings(settings), http).propose(request_data, evidence)


def test_model_fabricated_citations_cannot_become_a_supported_report(
    coordinator, settings, request_data, principal
):
    candidate = Candidate(
        category="service_selector_mismatch", evidence_ids=[uuid4() for _ in range(5)]
    )
    body = {
        "choices": [{"finish_reason": "stop", "message": {"content": candidate.model_dump_json()}}]
    }
    configured = scale_settings(settings)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as http:
        service = Coordinator(
            configured, coordinator.store, coordinator.collector, ScaleReasoner(configured, http)
        )
        result = service.run(request_data, principal)
    assert result.report.outcome == "insufficient_evidence"
    assert result.report.diagnosis == "undetermined"
    assert result.report.verification.claims == []
    assert "invalid_citations" in result.report.verification.checks


def test_scale_configuration_requires_https_and_explicit_credentials(settings):
    with pytest.raises(ConfigurationError):
        replace(settings, reasoner="scale")
    configured = scale_settings(settings)
    for url in (
        "http://insecure.example.test",
        "https://user:password@scale.example.test",
        "https://scale.example.test?key=private",
    ):
        with pytest.raises(ConfigurationError):
            replace(configured, sgp_api_base_url=url)
    assert configured.sgp_api_key not in repr(configured)
