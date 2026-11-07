import json

import httpx
import pytest

from ai_sre.cli import main
from ai_sre.errors import ConfigurationError, SwitchError
from ai_sre.switch.runtime import FakeSwitchRuntime
from ai_sre.switch.trust import (
    ROI_ANALYSIS_TYPES,
    TrustClient,
    TrustSettings,
    derive_api_base,
    render_governance_summary,
    validate_instance,
)

INSTANCE = "https://app.switchagents.ai"
API_BASE = "https://api.switchagents.ai/api/v1"


def settings(**overrides):
    values = {"api_key": "sk_test_only_never_print", "instance": INSTANCE}
    values.update(overrides)
    return TrustSettings(**values)


def client(handler, **overrides):
    configuration = settings(**overrides)
    http = httpx.Client(
        base_url=configuration.api_base_url,
        headers={"Authorization": f"ApiKey {configuration.api_key}"},
        transport=httpx.MockTransport(handler),
    )
    return TrustClient(configuration, http=http)


def response(payload, status=200):
    return httpx.Response(status, json=payload)


def summary_handler(request):
    path = request.url.path.removeprefix("/api/v1")
    if path == "/auth/api-key/identity":
        return response(
            {
                "key_scope": "workspace",
                "org_id": "org-1",
                "tenant_id": "tenant-1",
                "workspace_id": "workspace-1",
            }
        )
    if path.startswith("/aispm-agents-dashboard/"):
        return response({"total_mtd_cost": 12.5, "total_cost_cap": 100.0})
    if path.startswith("/aispm-agents/"):
        return response({"total": 0, "rows": []})
    if path.startswith("/aispm-mcp-servers/"):
        return response({"total": 5, "rows": []})
    if path.startswith("/aispm-models/"):
        return response({"total": 2, "rows": []})
    if path.startswith("/aispm-tools/"):
        return response({"total": 3, "rows": []})
    if path.startswith("/issues/"):
        return response({"total": 1, "rows": []})
    if path.startswith("/roi/agents/"):
        return response(
            {
                "items": [
                    {
                        "agent_id": "agent-1",
                        "interaction_count": 4,
                        "latest_interaction_time": "2026-11-04T10:00:00Z",
                    }
                ],
                "total_count": 1,
            }
        )
    if path.startswith("/guardrails/policies/"):
        return response(
            {
                "items": [
                    {"id": "policy-1", "name": "Kubernetes investigator baseline"},
                ],
                "total_count": 1,
            }
        )
    if path.endswith("/outcomes"):
        return response(
            {
                "ok": 10,
                "alerted": 1,
                "blocked": 2,
                "redacted": 3,
                "errored": 0,
                "total": 16,
            }
        )
    if path.startswith("/eval/agent-evaluation-summary/"):
        assert request.url.params["agent_id"] == "agent-1"
        return response(
            {
                "health": {"score": 0.92, "previous_score": 0.9},
                "coverage": [{"category": "OWASP", "covered": True}],
                "trend": {"points": [0.9, 0.92]},
            }
        )
    raise AssertionError(f"unexpected path {path}")


def test_trust_settings_repr_never_contains_the_key():
    assert "sk_test_only" not in repr(settings())
    assert settings().api_key == "sk_test_only_never_print"


@pytest.mark.parametrize(
    ("instance", "expected"),
    [
        ("https://app.switchagents.ai", "https://app.switchagents.ai"),
        ("https://api.switchagents.ai", "https://api.switchagents.ai"),
        ("https://switchagents.ai", "https://switchagents.ai"),
        ("http://localhost:4000", "http://localhost:4000"),
    ],
)
def test_instance_validation_accepts_trust_hosts_and_localhost(instance, expected):
    assert validate_instance(instance) == expected


@pytest.mark.parametrize(
    "instance",
    [
        "http://app.switchagents.ai",
        "https://evilswitchagents.ai",
        "https://switchagents.ai.evil.test",
        "https://user:pass@app.switchagents.ai",
        "https://app.switchagents.ai/api",
        "https://app.switchagents.ai?x=1",
        "ftp://app.switchagents.ai",
        "not a url",
    ],
)
def test_instance_validation_fails_closed(instance):
    with pytest.raises(ConfigurationError):
        validate_instance(instance)


def test_api_base_derivation_replaces_the_edge_label():
    assert derive_api_base("https://app.switchagents.ai") == API_BASE
    assert derive_api_base("https://api.switchagents.ai") == API_BASE
    assert derive_api_base("https://switchagents.ai") == API_BASE
    assert derive_api_base("http://localhost:4000") == "http://localhost:4000/api/v1"


def test_settings_require_a_key_only_when_required(monkeypatch):
    monkeypatch.delenv("AI_SRE_TRUST_API_KEY", raising=False)
    assert TrustSettings.from_env() is None
    with pytest.raises(ConfigurationError, match="AI_SRE_TRUST_API_KEY"):
        TrustSettings.from_env(required=True)


def test_identity_discovers_scope_and_sends_the_api_key_scheme():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        return summary_handler(request)

    client(handler).identity()
    assert seen["auth"] == "ApiKey sk_test_only_never_print"


def test_governance_summary_uses_read_only_endpoints_and_keeps_scope():
    trust = client(summary_handler)
    result = trust.governance_summary(agent_ids=("agent-1",))
    assert result["identity"] == {
        "key_scope": "workspace",
        "org_id": "org-1",
        "tenant_id": "tenant-1",
        "workspace_id": "workspace-1",
    }
    assert result["cost"] == {"total_mtd_cost": 12.5, "total_cost_cap": 100.0}
    assert result["inventory"] == {
        "agents": 0,
        "mcp_servers": 5,
        "models": 2,
        "tools": 3,
        "issues": 1,
    }
    assert result["roi_agents"][0]["agent_id"] == "agent-1"
    assert result["guardrails"]["policy_count"] == 1
    assert result["guardrails"]["policies"] == [
        {"id": "policy-1", "name": "Kubernetes investigator baseline"}
    ]
    assert result["guardrails"]["outcomes"]["blocked"] == 2
    assert result["guardrails"]["outcomes"]["total"] == 16
    assert result["evaluations"][0]["score"] == 0.92
    assert "advisory" in result
    assert "do not stop spending" in result["advisory"]


def test_roi_summary_reads_latest_analyses_for_one_agent():
    seen = {}

    def handler(request):
        if request.url.path.removeprefix("/api/v1").startswith("/roi/analyses/latest/"):
            seen["agent_id"] = request.url.params["agent_id"]
            seen["types"] = request.url.params["analysis_types"]
            return response({"model_optimizer": {"estimated_savings_usd": 3.0}})
        return summary_handler(request)

    result = client(handler).roi_summary(agent_id="agent-1")
    assert seen["agent_id"] == "agent-1"
    assert seen["types"] == ",".join(ROI_ANALYSIS_TYPES)
    assert result["latest_analyses"]["model_optimizer"]["estimated_savings_usd"] == 3.0
    assert result["advisory"]


def test_configured_scope_must_match_the_api_key():
    trust = client(summary_handler, tenant_id="tenant-2")
    with pytest.raises(ConfigurationError, match="TENANT_ID"):
        trust.scope()


def test_unscoped_api_key_fails_closed():
    trust = client(lambda request: response({"key_scope": "org", "tenant_id": "tenant-1"}))
    with pytest.raises(SwitchError, match="not scoped"):
        trust.identity()


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "rejected the API key"),
        (403, "rejected the API key"),
        (404, "could not find"),
        (429, "rate limit"),
        (500, "request failed"),
    ],
)
def test_trust_errors_are_sanitized(status, message):
    def handler(request):
        return httpx.Response(status, text="secret-key and backend internals")

    with pytest.raises(SwitchError, match=message) as caught:
        client(handler).identity()
    assert "secret-key" not in str(caught.value)
    assert "internals" not in str(caught.value)


def test_trust_connection_failure_is_sanitized():
    def refuse(request):
        raise httpx.ConnectError("secret-key", request=request)

    with pytest.raises(SwitchError, match="unavailable") as caught:
        client(refuse).identity()
    assert "secret-key" not in str(caught.value)


def test_invalid_json_is_rejected_without_echoing_the_body():
    with pytest.raises(SwitchError, match="invalid JSON"):
        client(lambda request: httpx.Response(200, text="<html>secret</html>")).identity()


def test_governance_document_states_the_limits_of_the_snapshot():
    result = client(summary_handler).governance_summary(agent_ids=("agent-1",))
    rendered = render_governance_summary(result)
    assert "# Switch Trust governance summary" in rendered
    assert "Tenant: tenant-1" in rendered
    assert "Workspace: workspace-1" in rendered
    assert "MCP servers: 5" in rendered
    assert "Issues: 1" in rendered
    assert "agent-1: score=0.92" in rendered
    assert "Outcomes: ok=10, alerted=1, blocked=2" in rendered
    assert "does not stop spending" in rendered
    assert "does not claim that a gate passed" in rendered


def test_trust_scan_wraps_the_switch_trust_cli(monkeypatch):
    calls = []

    class Result:
        returncode = 0

    def fake_run(command, check):
        calls.append(command)
        return Result()

    monkeypatch.setattr("ai_sre.cli.shutil.which", lambda name: "/opt/bin/switch-trust")
    monkeypatch.setattr("ai_sre.cli.subprocess.run", fake_run)
    assert (
        main(
            [
                "switch",
                "trust",
                "scan",
                "--path",
                "src",
                "--format",
                "sarif",
                "--output",
                "findings.sarif",
            ]
        )
        == 0
    )
    assert calls == [
        [
            "/opt/bin/switch-trust",
            "scan",
            "src",
            "--format",
            "sarif",
            "--output",
            "findings.sarif",
        ]
    ]


def test_trust_scan_fails_closed_without_the_cli(monkeypatch, capsys):
    monkeypatch.setattr("ai_sre.cli.shutil.which", lambda name: None)
    assert main(["switch", "trust", "scan", "--path", "src"]) == 1
    assert "Switch Trust CLI is not installed" in capsys.readouterr().err


def test_trust_status_fails_closed_without_configuration(monkeypatch, capsys):
    monkeypatch.delenv("AI_SRE_TRUST_API_KEY", raising=False)
    assert main(["switch", "trust", "status", "--json"]) == 1
    assert "AI_SRE_TRUST_API_KEY" in capsys.readouterr().err


def test_trust_publish_console_attaches_the_summary_to_console_governance(monkeypatch, capsys):
    class FakeTrust:
        def __init__(self, configuration):
            pass

        def governance_summary(self, *, agent_ids=()):
            return {
                "trust_base_url": API_BASE,
                "identity": {
                    "key_scope": "workspace",
                    "org_id": "org-1",
                    "tenant_id": "tenant-1",
                    "workspace_id": "workspace-1",
                },
                "cost": {"total_mtd_cost": 0, "total_cost_cap": None},
                "inventory": {
                    "agents": 0,
                    "mcp_servers": 5,
                    "models": 0,
                    "tools": 0,
                    "issues": 0,
                },
                "roi_agents": [],
                "roi_agent_count": 0,
                "guardrails": {
                    "policy_count": 0,
                    "policies": [],
                    "outcomes": {"ok": 0, "total": 0},
                },
                "evaluations": [],
                "advisory": "Caps are advisory.",
            }

        def close(self):
            pass

    class FakeConsole:
        def __init__(self, configuration):
            self.attached = None

        def rooms(self):
            return [{"id": "console-governance", "name": "k8s-governance"}]

        def create_document(self, **kwargs):
            assert "Switch Trust governance summary" in kwargs["content"]
            return {"id": "console-doc"}

        def attach_document_to_room(self, room_id, document_id):
            self.attached = (room_id, document_id)

        def close(self):
            pass

    monkeypatch.setenv("AI_SRE_TRUST_API_KEY", "sk_test_only_never_print")
    monkeypatch.setattr("ai_sre.cli.TrustClient", FakeTrust)

    class Settings:
        server_url = "http://console.test"

    monkeypatch.setattr(
        "ai_sre.cli.ConsoleSettings.from_env",
        lambda required=False: Settings(),
    )
    monkeypatch.setattr("ai_sre.cli.ConsoleClient", FakeConsole)
    assert main(["switch", "trust", "publish", "--console", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["switch_room_id"] == "console-governance"
    assert payload["switch_document_id"] == "console-doc"
    assert payload["switch_console_url"] == "http://console.test"


def test_trust_publish_writes_a_read_only_document_to_governance_room(
    tmp_path, monkeypatch, capsys
):
    class FakeTrust:
        def __init__(self, configuration):
            self.configuration = configuration

        def governance_summary(self, *, agent_ids=()):
            assert agent_ids == ()
            return {
                "trust_base_url": API_BASE,
                "identity": {
                    "key_scope": "workspace",
                    "org_id": "org-1",
                    "tenant_id": "tenant-1",
                    "workspace_id": "workspace-1",
                },
                "cost": {"total_mtd_cost": 1.0, "total_cost_cap": 10.0},
                "roi_agents": [],
                "roi_agent_count": 0,
                "guardrails": {"policy_count": 0, "policies": []},
                "evaluations": [],
                "advisory": "Caps are advisory and do not stop spending.",
            }

        def close(self):
            pass

    runtime = FakeSwitchRuntime()
    runtime.bootstrap()
    monkeypatch.setenv("AI_SRE_TRUST_API_KEY", "sk_test_only_never_print")
    monkeypatch.setattr("ai_sre.cli.TrustClient", FakeTrust)
    monkeypatch.setattr("ai_sre.cli.build_switch_runtime", lambda settings: runtime)

    assert (
        main(
            [
                "switch",
                "trust",
                "publish",
                "--state-dir",
                str(tmp_path / "switch"),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["switch_room_id"] == runtime.room_ids["governance"]
    room = runtime.room_by_id(payload["switch_room_id"])
    document = next(
        item for item in room.docs.values() if item["id"] == payload["switch_document_id"]
    )
    assert document["creator"] == "incident-commander"
    assert "Switch Trust governance summary" in document["content"]
    assert room.messages[-1].sender == "incident-commander"
    assert "[governance]" in room.messages[-1].body


def test_trust_publish_fails_closed_when_switch_is_off(tmp_path, monkeypatch, capsys):
    class FakeTrust:
        def __init__(self, configuration):
            pass

        def governance_summary(self, *, agent_ids=()):
            return {}

        def close(self):
            pass

    monkeypatch.setenv("AI_SRE_TRUST_API_KEY", "sk_test_only_never_print")
    monkeypatch.setenv("AI_SRE_SWITCH_MODE", "off")
    monkeypatch.setattr("ai_sre.cli.TrustClient", FakeTrust)
    assert main(["switch", "trust", "publish", "--state-dir", str(tmp_path)]) == 1
    assert "Switch is off" in capsys.readouterr().err


def test_roi_command_reports_advisory_caps(monkeypatch, capsys):
    class FakeTrust:
        def __init__(self, configuration):
            pass

        def roi_summary(self, *, agent_id=None):
            assert agent_id == "agent-1"
            return {
                "total_mtd_cost": 2.5,
                "total_cost_cap": 20.0,
                "agents": [],
                "agent_count": 0,
                "agent_id": agent_id,
                "latest_analyses": {},
                "advisory": "Caps are advisory.",
            }

        def close(self):
            pass

    monkeypatch.setenv("AI_SRE_TRUST_API_KEY", "sk_test_only_never_print")
    monkeypatch.setattr("ai_sre.cli.TrustClient", FakeTrust)
    assert (
        main(
            [
                "switch",
                "roi",
                "--agent-id",
                "agent-1",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["advisory"] == "Caps are advisory."
