"""Minimal Switch agent-protocol client.

Only the operations this product needs are exposed.  The client maps every
failure onto a sanitized :class:`SwitchError`; response bodies are never
propagated, because a hostile or misconfigured server could echo credentials.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx

from ai_sre.errors import SwitchError
from ai_sre.switch.identity import AgentCredential


class SwitchClient:
    def __init__(
        self,
        base_url: str,
        credentials: dict[str, AgentCredential],
        *,
        http: httpx.Client | None = None,
        timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.credentials = credentials
        self.timeout = timeout
        self._http = http or httpx.Client(timeout=timeout, follow_redirects=False)

    def close(self) -> None:
        self._http.close()

    def health(self) -> bool:
        try:
            response = self._http.get(f"{self.base_url}/health", timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise SwitchError("Switch is unavailable") from exc
        return response.status_code == 200

    def register(self, registration_token: str, body: dict[str, object]) -> AgentCredential:
        payload = self._json(
            self._http.post(
                f"{self.base_url}/agents",
                json=body,
                headers={"Authorization": f"Bearer {registration_token}"},
                timeout=self.timeout,
            ),
            "registration",
        )
        agent_id = payload.get("id")
        api_key = payload.get("api_key")
        name = body.get("name")
        if (
            not isinstance(agent_id, str)
            or not agent_id
            or not isinstance(api_key, str)
            or not api_key
            or not isinstance(name, str)
        ):
            raise SwitchError("Switch returned an invalid registration response")
        return AgentCredential(name=name, agent_id=agent_id, api_key=api_key)

    def operation(
        self,
        agent_name: str,
        operation: str,
        arguments: dict[str, Any] | None = None,
        *,
        connection_id: str | None = None,
    ) -> Any:
        credential = self.credential(agent_name)
        headers = {"Authorization": f"Bearer {credential.api_key}"}
        if connection_id:
            headers["X-Switch-Connection-Id"] = connection_id
        payload = self._json(
            self._http.post(
                f"{self.base_url}/agents/{quote(credential.agent_id, safe='')}/ops/"
                f"{quote(operation, safe='')}",
                json=arguments or {},
                headers=headers,
                timeout=self.timeout,
            ),
            operation,
        )
        return payload.get("result")

    def post_message(self, agent_name: str, room_id: str, content: str) -> str:
        credential = self.credential(agent_name)
        payload = self._json(
            self._http.post(
                f"{self.base_url}/agents/{quote(credential.agent_id, safe='')}/message",
                json={"room_id": room_id, "content": content},
                headers={"Authorization": f"Bearer {credential.api_key}"},
                timeout=self.timeout,
            ),
            "message",
        )
        event_id = payload.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise SwitchError("Switch did not return a message id")
        return event_id

    def history(self, agent_name: str, room_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        credential = self.credential(agent_name)
        response = self._http.get(
            f"{self.base_url}/agents/{quote(credential.agent_id, safe='')}/rooms/"
            f"{quote(room_id, safe='')}/history",
            params={"limit": limit},
            headers={"Authorization": f"Bearer {credential.api_key}"},
            timeout=self.timeout,
        )
        payload = self._json(response, "history")
        events = payload.get("events", [])
        if not isinstance(events, list):
            raise SwitchError("Switch returned an invalid history response")
        return [event for event in events if isinstance(event, dict)]

    def credential(self, agent_name: str) -> AgentCredential:
        try:
            return self.credentials[agent_name]
        except KeyError as exc:
            raise SwitchError(
                f"Switch agent {agent_name} is not registered; run switch bootstrap"
            ) from exc

    @contextmanager
    def open_room_session(self, agent_name: str, room_id: str) -> Iterator[RoomSession]:
        credential = self.credential(agent_name)
        socket = _RoomSocket(
            base_url=self.base_url,
            credential=credential,
            room_id=room_id,
            timeout=self.timeout,
        )
        session = RoomSession(self, agent_name=agent_name, room_id=room_id, socket=socket)
        try:
            yield session
        finally:
            session.close()

    def _json(self, response: httpx.Response, operation: str) -> dict[str, Any]:
        if response.status_code >= 400:
            if response.status_code in {401, 403}:
                raise SwitchError("Switch rejected the agent credentials")
            if response.status_code == 404:
                raise SwitchError(f"Switch could not find the resource for {operation}")
            if response.status_code == 409:
                raise SwitchError(f"Switch connection conflict during {operation}")
            if response.status_code == 429:
                raise SwitchError("Switch rate limit reached")
            raise SwitchError(
                f"Switch request failed during {operation} (status={response.status_code})"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SwitchError("Switch returned an invalid JSON response") from exc
        if not isinstance(payload, dict):
            raise SwitchError("Switch returned an invalid response shape")
        return payload


class RoomSession:
    """A live, single-room Switch connection plus the room-scoped operations."""

    def __init__(
        self,
        client: SwitchClient,
        *,
        agent_name: str,
        room_id: str,
        socket: _RoomSocket,
    ):
        self.client = client
        self.agent_name = agent_name
        self.room_id = room_id
        self.connection_id = socket.connection_id
        self._socket = socket

    def post(
        self,
        body: str,
        *,
        targets: list[str] | None = None,
        thread_id: str | None = None,
    ) -> str:
        if targets:
            arguments: dict[str, Any] = {"body": body, "target_names": targets}
            if thread_id:
                arguments["thread_id"] = thread_id
            result = self.client.operation(
                self.agent_name,
                "send_targeted_message",
                arguments,
                connection_id=self.connection_id,
            )
            event_id = (result or {}).get("event_id")
        else:
            arguments = {"body": body}
            if thread_id:
                arguments["thread_id"] = thread_id
            result = self.client.operation(
                self.agent_name,
                "post_message",
                arguments,
                connection_id=self.connection_id,
            )
            event_id = (result or {}).get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise SwitchError("Switch did not return a room message id")
        return event_id

    def create_document(
        self,
        *,
        name: str,
        description: str,
        instructions: str,
        content: str,
    ) -> str:
        result = self.client.operation(
            self.agent_name,
            "create_room_document",
            {
                "name": name,
                "description": description,
                "instructions": instructions,
                "content": content,
            },
            connection_id=self.connection_id,
        )
        document_id = (result or {}).get("document_id")
        if not isinstance(document_id, str) or not document_id:
            raise SwitchError("Switch did not return a room document id")
        return document_id

    def update_document(
        self,
        document_id: str,
        *,
        content: str | None = None,
        description: str | None = None,
    ) -> None:
        arguments: dict[str, Any] = {"document_id": document_id}
        if content is not None:
            arguments["content"] = content
        if description is not None:
            arguments["description"] = description
        self.client.operation(
            self.agent_name,
            "update_room_document",
            arguments,
            connection_id=self.connection_id,
        )

    def assume_role(self, role: str) -> dict[str, Any]:
        result = self.client.operation(
            self.agent_name,
            "assume_role",
            {"role": role},
            connection_id=self.connection_id,
        )
        if not isinstance(result, dict):
            raise SwitchError("Switch returned an invalid role response")
        return result

    def release_role(self) -> None:
        self.client.operation(
            self.agent_name,
            "release_role",
            {},
            connection_id=self.connection_id,
        )

    def list_references(self) -> dict[str, Any]:
        result = self.client.operation(
            self.agent_name,
            "list_references",
            {},
            connection_id=self.connection_id,
        )
        if not isinstance(result, dict):
            raise SwitchError("Switch returned an invalid room resource list")
        return result

    def list_roles(self) -> list[dict[str, Any]]:
        result = self.client.operation(
            self.agent_name,
            "list_roles",
            {},
            connection_id=self.connection_id,
        )
        if not isinstance(result, list):
            raise SwitchError("Switch returned an invalid room role list")
        return [role for role in result if isinstance(role, dict)]

    def load_documents(self, document_ids: list[str]) -> list[dict[str, Any]]:
        result = self.client.operation(
            self.agent_name,
            "load_internal_documents",
            {"ids": document_ids},
            connection_id=self.connection_id,
        )
        if not isinstance(result, list):
            raise SwitchError("Switch returned an invalid room document response")
        return [document for document in result if isinstance(document, dict)]

    def read_context(self, *, limit: int = 50) -> dict[str, Any]:
        result = self.client.operation(
            self.agent_name,
            "read_context",
            {"limit": limit},
            connection_id=self.connection_id,
        )
        if not isinstance(result, dict):
            raise SwitchError("Switch returned an invalid room context")
        return result

    def close(self) -> None:
        try:
            self.release_role()
        except SwitchError:
            # The socket still has to close, and Switch expires the lease with
            # the connection even when the explicit release could not be sent.
            pass
        finally:
            self._socket.close()


class _RoomSocket:
    """The WebSocket that owns a connection id and keeps the room claimed.

    Room-scoped operations need a live connection.  Switch sends an application
    ``ping`` frame; answering ``pong`` is the heartbeat, so the reader thread
    stays off the caller's thread and handles it.
    """

    def __init__(
        self,
        *,
        base_url: str,
        credential: AgentCredential,
        room_id: str,
        timeout: float,
    ):
        try:
            import websocket
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise SwitchError("websocket-client is required for Switch rooms") from exc

        self.connection_id = uuid4().hex
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._error: SwitchError | None = None
        parts = urlsplit(base_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        query = urlencode(
            {
                "connection_id": self.connection_id,
                "scope": "single",
                "filter": "all",
                "rooms": room_id,
                "client": "ai-sre",
            }
        )
        path = f"/agents/{quote(credential.agent_id, safe='')}/connection/ws"
        url = urlunsplit((scheme, parts.netloc, path, query, ""))
        try:
            self._socket = websocket.create_connection(
                url,
                header=[f"Authorization: Bearer {credential.api_key}"],
                timeout=timeout,
                enable_multithread=True,
            )
        except Exception as exc:
            raise SwitchError("Switch room connection failed") from exc

        try:
            self._socket.settimeout(timeout)
            state = self._read_until_state()
            if state is None:
                raise SwitchError("Switch room connection did not become ready")
        except SwitchError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise SwitchError("Switch room connection failed") from exc
        self._socket.settimeout(1.0)
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_until_state(self) -> dict[str, Any] | None:
        while not self._stop.is_set():
            frame = json.loads(self._socket.recv())
            if not isinstance(frame, dict):
                continue
            event = frame.get("event")
            if event == "ping":
                self._pong()
            elif event == "refused":
                raise SwitchError("Switch refused the room connection")
            elif event == "connection_state":
                return frame
        return None

    def _read_loop(self) -> None:
        import websocket

        while not self._stop.is_set():
            try:
                frame = json.loads(self._socket.recv())
            except websocket.WebSocketTimeoutException:
                # A quiet room is not a dead room: the socket has a short read
                # timeout so the loop can answer the server's ping frames.
                continue
            except Exception:
                if not self._stop.is_set():
                    self._error = SwitchError("Switch room connection was lost")
                return
            if not isinstance(frame, dict):
                continue
            event = frame.get("event")
            if event == "ping":
                try:
                    self._pong()
                except Exception:
                    self._error = SwitchError("Switch room heartbeat failed")
                    return
            elif event in {"refused", "evicted"}:
                self._error = SwitchError("Switch closed the room connection")
                return

    def _pong(self) -> None:
        with self._lock:
            self._socket.send(json.dumps({"type": "pong"}))

    def close(self) -> None:
        self._stop.set()
        try:
            self._socket.close()
        except Exception:
            pass
        thread = getattr(self, "_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
