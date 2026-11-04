import json

import httpx
import pytest

from kube_investigator.cli import main
from kube_investigator.errors import ConfigurationError, SwitchError
from kube_investigator.switch.console import (
    ConsoleClient,
    ConsoleSettings,
    validate_console_url,
)


def settings(**overrides):
    values = {
        "server_url": "http://localhost:63398",
        "email": "admin@switch.local",
        "password": "console-test-password",
    }
    values.update(overrides)
    return ConsoleSettings(**values)


def client(handler, **overrides):
    configuration = settings(**overrides)
    http = httpx.Client(
        base_url=configuration.server_url,
        transport=httpx.MockTransport(handler),
    )
    return ConsoleClient(configuration, http=http)


def response(payload, status=200):
    return httpx.Response(status, json=payload)


def test_console_settings_repr_hides_the_password():
    assert "console-test-password" not in repr(settings())
    assert settings().password == "console-test-password"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:63398", "http://localhost:63398"),
        ("http://127.0.0.1:63398", "http://127.0.0.1:63398"),
        ("https://console.example.test", "https://console.example.test"),
    ],
)
def test_console_url_validation(url, expected):
    assert validate_console_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://console.example.test",
        "ftp://localhost:63398",
        "https://console.example.test/path",
        "https://user:pass@console.example.test",
        "not a url",
    ],
)
def test_console_url_validation_fails_closed(url):
    with pytest.raises(ConfigurationError):
        validate_console_url(url)


def test_console_settings_load_from_a_local_server_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / "switch-core.env"
    env_file.write_text(
        "GATEWAY_ADMIN_EMAIL=admin@switch.local\n"
        "GATEWAY_ADMIN_PASSWORD=file-password\n"
        "GATEWAY_PUBLIC_URL=http://localhost:63398\n"
    )
    monkeypatch.setenv("INVESTIGATOR_CONSOLE_ENV_FILE", str(env_file))
    result = ConsoleSettings.from_env(required=True)
    assert result.server_url == "http://localhost:63398"
    assert result.email == "admin@switch.local"
    assert result.password == "file-password"


def test_console_login_and_create_agent_use_the_gateway_management_api():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/gateway/auth/login":
            assert json.loads(request.content) == {
                "email": "admin@switch.local",
                "password": "console-test-password",
            }
            return response({"id": "admin"}, status=200)
        if request.url.path == "/gateway/management/agents":
            body = json.loads(request.content)
            assert body["name"] == "incident-commander"
            assert body["controller_id"] == "controller-1"
            assert body["desired_state"] == "running"
            assert body["definition"]["provider"] == "opencode"
            assert body["definition"]["auto_mode"] is True
            assert body["definition"]["auto_approve"] is False
            assert body["definition"]["directory"] == "/repo"
            return response({"agent_id": "agent-1", "name": "incident-commander"}, status=201)
        raise AssertionError(f"unexpected path {request.url.path}")

    result = client(handler).create_agent(
        name="incident-commander",
        description="Commander",
        display_name="Incident Commander",
        controller_id="controller-1",
        provider="opencode",
        instructions="Read only.",
        directory="/repo",
    )
    assert result["agent_id"] == "agent-1"
    assert [request.url.path for request in seen] == [
        "/gateway/auth/login",
        "/gateway/management/agents",
    ]


def test_console_package_document_and_room_link_routes():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/gateway/auth/login":
            return response({"id": "admin"})
        if request.url.path == "/gateway/documents" and request.method == "POST":
            return response({"id": "doc-1", "name": "kit-doc"}, status=201)
        if request.url.path == "/gateway/packages" and request.method == "POST":
            return response({"id": "pkg-1", "name": "kit"}, status=201)
        if request.url.path == "/gateway/packages/pkg-1/documents":
            return response([{"id": "doc-1"}])
        if request.url.path == "/gateway/rooms/room-1/packages":
            return response([{"id": "pkg-1"}])
        if request.url.path == "/gateway/rooms/room-1/linked-rooms":
            assert request.method == "POST"
            return response(
                {"target_room_id": "memory-1", "label": "approved incidents"},
                status=201,
            )
        if request.url.path.endswith("/documents/doc-1"):
            return response({"ok": True}, status=201)
        if request.url.path == "/gateway/rooms/room-1/documents/doc-1":
            assert request.method == "POST"
            return response({"ok": True}, status=201)
        if request.url.path.endswith("/packages/pkg-1"):
            return response({"ok": True}, status=201)
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    console = client(handler)
    console.create_document(
        name="kit-doc",
        description="Kit",
        instructions="Read",
        content="Rules",
    )
    console.create_package(name="kit", description="Kit", instructions="Read")
    assert console.package_documents("pkg-1") == [{"id": "doc-1"}]
    console.add_document_to_package("pkg-1", "doc-1")
    console.attach_package_to_room("room-1", "pkg-1")
    console.attach_document_to_room("room-1", "doc-1")
    console.link_room("room-1", "memory-1", "approved incidents")
    assert ("POST", "/gateway/packages/pkg-1/documents/doc-1") in seen
    assert ("POST", "/gateway/rooms/room-1/packages/pkg-1") in seen
    assert ("POST", "/gateway/rooms/room-1/documents/doc-1") in seen
    assert ("POST", "/gateway/rooms/room-1/linked-rooms") in seen


def test_console_errors_are_sanitized():
    def handler(request):
        return httpx.Response(500, text="secret-cookie and internals")

    with pytest.raises(SwitchError, match="failed") as caught:
        client(handler).controllers()
    assert "secret-cookie" not in str(caught.value)
    assert "internals" not in str(caught.value)


class FakeConsoleClient:
    instances = []

    def __init__(self, configuration):
        self.settings = configuration
        self.agent_calls = []
        self.room_calls = []
        self.closed = False
        self._agents = []
        self._rooms = []
        self._documents = []
        self._packages = []
        self._package_documents = {}
        self._room_packages = {}
        self._links = {}
        FakeConsoleClient.instances.append(self)

    def close(self):
        self.closed = True

    def controllers(self):
        return [
            {
                "id": "controller-1",
                "name": "Test machine",
                "state": "online",
            }
        ]

    def agents(self):
        return list(self._agents)

    def rooms(self):
        return list(self._rooms)

    def documents(self):
        return list(self._documents)

    def create_document(self, **kwargs):
        item = {"id": f"{kwargs['name']}-doc", "name": kwargs["name"]}
        self._documents.append(item)
        return item

    def packages(self):
        return list(self._packages)

    def create_package(self, **kwargs):
        item = {"id": f"{kwargs['name']}-pkg", "name": kwargs["name"]}
        self._packages.append(item)
        self._package_documents.setdefault(item["id"], [])
        return item

    def package_documents(self, package_id):
        return list(self._package_documents.get(package_id, []))

    def add_document_to_package(self, package_id, document_id):
        self._package_documents.setdefault(package_id, []).append({"id": document_id})

    def room_packages(self, room_id):
        return [{"id": value} for value in self._room_packages.get(room_id, [])]

    def attach_package_to_room(self, room_id, package_id):
        self._room_packages.setdefault(room_id, []).append(package_id)

    def linked_rooms(self, room_id):
        return [{"target_room_id": target} for target in self._links.get(room_id, [])]

    def link_room(self, room_id, target_room_id, label):
        del label
        self._links.setdefault(room_id, []).append(target_room_id)

    def create_agent(self, **kwargs):
        self.agent_calls.append(kwargs)
        agent = {
            "agent_id": f"{kwargs['name']}-id",
            "name": kwargs["name"],
            "definition": {"provider": kwargs["provider"]},
            "desired_state": "running",
        }
        self._agents.append(agent)
        return agent

    def update_agent(self, agent_id, **kwargs):
        return {"agent_id": agent_id, **kwargs}

    def create_rooms_from_yaml(self, template):
        self.room_calls.append(template)
        if "group:" in template:
            rooms = [
                {"room_name": name, "room_id": f"{name}-id"}
                for name in {
                    "k8s-incidents",
                    "k8s-change",
                    "k8s-memory",
                    "k8s-governance",
                }
            ]
            return {"group_id": "group-1", "rooms": rooms}
        return {
            "room_id": "incident-room-id",
            "room_name": "incident-test-shop-checkout",
        }


def _configure(monkeypatch, tmp_path):
    FakeConsoleClient.instances.clear()
    monkeypatch.setattr(
        "kube_investigator.cli.ConsoleSettings.from_env",
        lambda required=False: settings(),
    )
    monkeypatch.setattr("kube_investigator.cli.ConsoleClient", FakeConsoleClient)
    monkeypatch.chdir(tmp_path)


def test_console_setup_creates_six_agents_and_core_rooms(tmp_path, monkeypatch, capsys):
    _configure(monkeypatch, tmp_path)
    assert main(["switch", "console", "setup", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    fake = FakeConsoleClient.instances[-1]
    assert [call["name"] for call in fake.agent_calls] == [
        "incident-commander",
        "cluster-investigator",
        "telemetry-analyst",
        "change-analyst",
        "memory-researcher",
        "evidence-verifier",
    ]
    assert all(call["provider"] == "opencode" for call in fake.agent_calls)
    assert payload["rooms"] == {
        "incidents": "k8s-incidents-id",
        "change": "k8s-change-id",
        "memory": "k8s-memory-id",
        "governance": "k8s-governance-id",
    }
    assert payload["incident_kit_package_id"] == "k8s-incident-kit-pkg"
    assert len(fake._documents) == 3
    assert len(fake._package_documents["k8s-incident-kit-pkg"]) == 3
    assert fake.closed is True


def test_console_incident_requires_all_console_agents(tmp_path, monkeypatch, capsys):
    _configure(monkeypatch, tmp_path)
    assert main(["switch", "console", "incident", "--json"]) == 1
    assert "switch console setup" in capsys.readouterr().err


def test_console_incident_creates_a_top_level_kickoff(tmp_path, monkeypatch, capsys):
    _configure(monkeypatch, tmp_path)
    fake = FakeConsoleClient(settings())
    for name in (
        "incident-commander",
        "cluster-investigator",
        "telemetry-analyst",
        "change-analyst",
        "memory-researcher",
        "evidence-verifier",
    ):
        fake._agents.append({"name": name, "agent_id": f"{name}-id"})
    fake._rooms = [
        {"name": "k8s-incidents", "id": "k8s-incidents-id"},
        {"name": "k8s-change", "id": "k8s-change-id"},
        {"name": "k8s-memory", "id": "k8s-memory-id"},
        {"name": "k8s-governance", "id": "k8s-governance-id"},
    ]
    FakeConsoleClient.instances.append(fake)
    monkeypatch.setattr(
        "kube_investigator.cli.ConsoleClient",
        lambda configuration: fake,
    )
    assert (
        main(
            [
                "switch",
                "console",
                "incident",
                "--scenario",
                "selector-mismatch",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["room_id"] == "incident-room-id"
    assert payload["incident_kit_package_id"] == "k8s-incident-kit-pkg"
    assert fake._room_packages["incident-room-id"] == ["k8s-incident-kit-pkg"]
    assert set(fake._links["incident-room-id"]) == {
        "k8s-memory-id",
        "k8s-change-id",
        "k8s-governance-id",
    }
    template = fake.room_calls[-1]
    assert "\nkickoff: |\n" in template
    assert "@incident-commander Start the read-only investigation." in template
    assert "Fixture scenario: selector-mismatch" in template
