import json
import stat

import httpx
import pytest

from kube_investigator.errors import ConfigurationError, SwitchError
from kube_investigator.switch.client import RoomSession, SwitchClient, _RoomSocket
from kube_investigator.switch.identity import AgentCredential, CredentialStore

BASE = "http://switch.test:18000"


def credential(name="incident-commander"):
    return AgentCredential(name=name, agent_id=f"{name}-id", api_key=f"{name}-secret")


def client(handler, credentials=None, **kwargs):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    if credentials is None:
        credentials = {"incident-commander": credential()}
    return SwitchClient(BASE, credentials, http=http, **kwargs)


def test_credential_repr_never_contains_the_key():
    item = credential()
    assert "secret" not in repr(item)
    assert item.api_key == "incident-commander-secret"


def test_credential_store_roundtrip_is_private(tmp_path):
    store = CredentialStore(tmp_path / "switch" / "agents.json", BASE)
    credentials = {
        "incident-commander": credential(),
        "evidence-verifier": credential("evidence-verifier"),
    }
    store.save(credentials)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert store.load() == credentials


def test_credential_store_refuses_credentials_from_another_server(tmp_path):
    store = CredentialStore(tmp_path / "agents.json", BASE)
    store.save({"incident-commander": credential()})
    other = CredentialStore(tmp_path / "agents.json", "http://other.test:18000")
    with pytest.raises(ConfigurationError, match="different base URL"):
        other.load()


def test_credential_store_rejects_malformed_and_unknown_agents(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text("not json")
    with pytest.raises(ConfigurationError, match="unreadable"):
        CredentialStore(path, BASE).load()
    path.write_text(json.dumps({"version": 1, "base_url": BASE, "agents": {"unknown": {}}}))
    with pytest.raises(ConfigurationError, match="malformed"):
        CredentialStore(path, BASE).load()


def test_credential_store_get_names_the_missing_agent(tmp_path):
    with pytest.raises(ConfigurationError, match="incident-commander"):
        CredentialStore(tmp_path / "agents.json", BASE).get("incident-commander")


def test_health_is_public_and_reports_false_on_error_status():
    assert client(lambda request: httpx.Response(200, json={"ok": True})).health() is True
    assert client(lambda request: httpx.Response(503)).health() is False


def test_health_wraps_connection_failures():
    def refuse(request):
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(SwitchError, match="unavailable"):
        client(refuse).health()


def test_register_uses_the_registration_bearer_and_returns_the_one_time_key():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "agent-1", "api_key": "one-time-key"})

    result = client(handler, credentials={}).register("registration-token", {"name": "x"})
    assert result == AgentCredential(name="x", agent_id="agent-1", api_key="one-time-key")
    assert seen == {
        "path": "/agents",
        "auth": "Bearer registration-token",
        "body": {"name": "x"},
    }


def test_register_rejects_a_response_without_a_key():
    with pytest.raises(SwitchError, match="invalid registration"):
        client(
            lambda request: httpx.Response(200, json={"id": "agent-1"}),
            credentials={},
        ).register("token", {"name": "x"})


def test_operation_unwraps_result_and_sends_the_connection_header():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["connection"] = request.headers.get("x-switch-connection-id")
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"result": [{"room_id": "r1"}]})

    result = client(handler).operation(
        "incident-commander", "list_rooms", {"include_archived": False}, connection_id="conn-1"
    )
    assert result == [{"room_id": "r1"}]
    assert seen["path"] == "/agents/incident-commander-id/ops/list_rooms"
    assert seen["connection"] == "conn-1"
    assert seen["body"] == {"include_archived": False}
    assert seen["auth"] == "Bearer incident-commander-secret"


def test_post_message_returns_the_event_id():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "event_id": "$event"})

    assert client(handler).post_message("incident-commander", "room-1", "hello") == "$event"
    assert seen == {
        "path": "/agents/incident-commander-id/message",
        "body": {"room_id": "room-1", "content": "hello"},
    }


def test_history_filters_non_objects():
    def handler(request):
        assert request.url.params["limit"] == "7"
        return httpx.Response(200, json={"events": [{"id": 1}, "junk"]})

    assert client(handler).history("incident-commander", "room-1", limit=7) == [{"id": 1}]


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "rejected the agent credentials"),
        (403, "rejected the agent credentials"),
        (404, "could not find the resource"),
        (409, "connection conflict"),
        (429, "rate limit"),
        (500, "request failed"),
    ],
)
def test_errors_are_sanitized_and_named(status, message):
    def handler(request):
        return httpx.Response(status, text="secret-key and server internals")

    with pytest.raises(SwitchError) as caught:
        client(handler).operation("incident-commander", "list_rooms")
    assert message in str(caught.value)
    assert "secret-key" not in str(caught.value)
    assert "internals" not in str(caught.value)


def test_invalid_json_is_rejected_without_echoing_the_body():
    with pytest.raises(SwitchError, match="invalid JSON"):
        client(lambda request: httpx.Response(200, text="<html>secret</html>")).operation(
            "incident-commander", "list_rooms"
        )


def test_missing_credentials_fail_closed():
    with pytest.raises(SwitchError, match="not registered"):
        client(lambda request: httpx.Response(200, json={}), credentials={}).operation(
            "incident-commander", "list_rooms"
        )


class FakeSocket:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def recv(self):
        if not self.frames:
            raise OSError("closed")
        frame = self.frames.pop(0)
        return frame if isinstance(frame, str) else json.dumps(frame)

    def send(self, value):
        self.sent.append(json.loads(value))

    def close(self):
        self.closed = True


class StubClient:
    def __init__(self, *, fail_release=False):
        self.calls = []
        self.fail_release = fail_release

    def operation(self, agent_name, operation, arguments=None, *, connection_id=None):
        self.calls.append((agent_name, operation, arguments, connection_id))
        if self.fail_release and operation == "release_role":
            raise SwitchError("release failed")
        return {} if operation != "assume_role" else {"role": "incident-commander"}


class StubSocket:
    connection_id = "conn-1"
    closed = False

    def close(self):
        self.closed = True


def test_room_session_releases_its_role_before_closing_the_socket():
    client = StubClient()
    socket = StubSocket()
    session = RoomSession(client, agent_name="incident-commander", room_id="room-1", socket=socket)
    session.assume_role("incident-commander")
    session.close()
    assert client.calls == [
        (
            "incident-commander",
            "assume_role",
            {"role": "incident-commander"},
            "conn-1",
        ),
        ("incident-commander", "release_role", {}, "conn-1"),
    ]
    assert socket.closed is True


def test_room_session_closes_the_socket_when_release_fails():
    client = StubClient(fail_release=True)
    socket = StubSocket()
    RoomSession(client, agent_name="incident-commander", room_id="room-1", socket=socket).close()
    assert socket.closed is True


def test_room_socket_opens_one_room_with_a_client_generated_connection_id(monkeypatch):
    import websocket

    fake = FakeSocket([{"event": "connection_state", "data": {}}])
    seen = {}

    def create_connection(url, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        return fake

    monkeypatch.setattr(websocket, "create_connection", create_connection)
    socket = _RoomSocket(base_url=BASE, credential=credential(), room_id="room-1", timeout=5)
    try:
        assert len(socket.connection_id) == 32
        assert "connection/ws" in seen["url"]
        assert f"connection_id={socket.connection_id}" in seen["url"]
        assert "scope=single" in seen["url"]
        assert "filter=all" in seen["url"]
        assert "rooms=room-1" in seen["url"]
        assert seen["kwargs"]["header"] == ["Authorization: Bearer incident-commander-secret"]
    finally:
        socket.close()
    assert fake.closed is True


def test_room_socket_maps_a_refusal_to_a_safe_error(monkeypatch):
    import websocket

    fake = FakeSocket([{"event": "refused", "data": {"detail": "secret-key"}}])
    monkeypatch.setattr(websocket, "create_connection", lambda url, **kwargs: fake)
    with pytest.raises(SwitchError, match="refused the room connection") as caught:
        _RoomSocket(base_url=BASE, credential=credential(), room_id="room-1", timeout=5)
    assert "secret-key" not in str(caught.value)


def test_room_socket_maps_a_transport_failure(monkeypatch):
    import websocket

    def fail(url, **kwargs):
        raise OSError("connection refused with secret-key")

    monkeypatch.setattr(websocket, "create_connection", fail)
    with pytest.raises(SwitchError, match="connection failed") as caught:
        _RoomSocket(base_url=BASE, credential=credential(), room_id="room-1", timeout=5)
    assert "secret-key" not in str(caught.value)
