"""Switch Console integration for managed agents and Console-room setup.

Switch Console is the mandatory agent runtime.  This module talks only to the
local Console Gateway's management API: it creates managed agents on the
Console's enrolled machine and creates rooms from the same YAML templates the
rest of the repository uses.  It never starts an agent process itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from kube_investigator.errors import ConfigurationError, SwitchError

_LOCALHOST = "localhost"
_MACOS_CONSOLE_ENV = (
    Path.home()
    / "Library"
    / "Application Support"
    / "switchdash"
    / "local-switch-server"
    / "native"
    / "run"
    / "switch-core.env"
)


def validate_console_url(value: str) -> str:
    """Accept HTTPS, or cleartext only for a loopback Console Gateway."""
    try:
        parsed = urlsplit(value)
        _port = parsed.port  # validate a malformed port before the origin is used
    except ValueError as exc:
        raise ConfigurationError("INVESTIGATOR_CONSOLE_SERVER_URL must be a URL") from exc
    host = parsed.hostname
    if (
        not host
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError("INVESTIGATOR_CONSOLE_SERVER_URL must be an origin")
    if parsed.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}:
        return urlunsplit(("http", parsed.netloc, "", "", ""))
    if parsed.scheme != "https":
        raise ConfigurationError("INVESTIGATOR_CONSOLE_SERVER_URL must use HTTPS")
    return urlunsplit(("https", parsed.netloc, "", "", ""))


@dataclass(frozen=True)
class ConsoleSettings:
    server_url: str
    email: str
    password: str = field(repr=False)
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "server_url", validate_console_url(self.server_url))
        if not self.email.strip():
            raise ConfigurationError("Switch Console admin email is required")
        if not self.password.strip():
            raise ConfigurationError("Switch Console admin password is required")
        if self.timeout_seconds <= 0:
            raise ConfigurationError("Switch Console timeout must be positive")

    @classmethod
    def from_env(cls, *, required: bool = False) -> ConsoleSettings | None:
        server_url = os.getenv("INVESTIGATOR_CONSOLE_SERVER_URL")
        email = os.getenv("INVESTIGATOR_CONSOLE_EMAIL")
        password = os.getenv("INVESTIGATOR_CONSOLE_PASSWORD")
        env_file = os.getenv("INVESTIGATOR_CONSOLE_ENV_FILE")
        if not (server_url and email and password):
            path = Path(env_file) if env_file else _MACOS_CONSOLE_ENV
            if path.exists():
                values = _read_env_file(path)
                email = email or values.get("GATEWAY_ADMIN_EMAIL")
                password = password or values.get("GATEWAY_ADMIN_PASSWORD")
                server_url = (
                    server_url
                    or values.get("GATEWAY_PUBLIC_URL")
                    or values.get("FRONTEND_BASE_URL")
                )
                env_file = str(path)
        if not (server_url and email and password):
            if required:
                raise ConfigurationError(
                    "Switch Console is not configured; set "
                    "INVESTIGATOR_CONSOLE_SERVER_URL, INVESTIGATOR_CONSOLE_EMAIL, and "
                    "INVESTIGATOR_CONSOLE_PASSWORD, or point "
                    "INVESTIGATOR_CONSOLE_ENV_FILE at the Console server env file"
                )
            return None
        raw_timeout = os.getenv("INVESTIGATOR_CONSOLE_TIMEOUT_SECONDS", "20")
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise ConfigurationError(
                "INVESTIGATOR_CONSOLE_TIMEOUT_SECONDS must be a number"
            ) from exc
        return cls(
            server_url=server_url,
            email=email,
            password=password,
            timeout_seconds=timeout,
        )


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise ConfigurationError("Switch Console env file is unreadable") from exc
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


class ConsoleClient:
    """A thin, read/write client for the Console Gateway management API.

    The credential is an admin session cookie.  It is never logged, and every
    response body is sanitized before it can reach an operator.
    """

    def __init__(self, settings: ConsoleSettings, *, http: httpx.Client | None = None):
        self.settings = settings
        self._http = http or httpx.Client(
            base_url=settings.server_url,
            timeout=settings.timeout_seconds,
            follow_redirects=False,
        )
        self._logged_in = False

    def close(self) -> None:
        self._http.close()

    def login(self) -> None:
        response = self._request(
            "POST",
            "/gateway/auth/login",
            json={"email": self.settings.email, "password": self.settings.password},
            authenticated=False,
        )
        if response.status_code in {401, 403}:
            raise SwitchError("Switch Console rejected the admin credentials")
        self._check(response, "Console login")
        self._logged_in = True

    def controllers(self) -> list[dict[str, Any]]:
        return self._list("/gateway/management/controllers", "Console controllers")

    def agents(self) -> list[dict[str, Any]]:
        return self._list("/gateway/management/agents", "Console agents")

    def rooms(self) -> list[dict[str, Any]]:
        return self._list("/gateway/rooms", "Console rooms")

    def documents(self) -> list[dict[str, Any]]:
        return self._list("/gateway/documents", "Console documents")

    def create_document(
        self,
        *,
        name: str,
        description: str,
        instructions: str,
        content: str,
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/gateway/documents",
            json={
                "read_visibility": "public",
                "write_visibility": "private",
                "name": name,
                "description": description,
                "instructions": instructions,
                "content": content,
            },
        )
        self._check(response, "Console document creation")
        return self._json(response, "Console document creation")

    def packages(self) -> list[dict[str, Any]]:
        return self._list("/gateway/packages", "Console packages")

    def create_package(
        self,
        *,
        name: str,
        description: str,
        instructions: str,
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/gateway/packages",
            json={
                "read_visibility": "public",
                "write_visibility": "private",
                "name": name,
                "description": description,
                "instructions": instructions,
            },
        )
        self._check(response, "Console package creation")
        return self._json(response, "Console package creation")

    def package_documents(self, package_id: str) -> list[dict[str, Any]]:
        return self._list(
            f"/gateway/packages/{package_id}/documents",
            "Console package documents",
        )

    def add_document_to_package(self, package_id: str, document_id: str) -> None:
        response = self._request(
            "POST",
            f"/gateway/packages/{package_id}/documents/{document_id}",
        )
        self._check(response, "Console package document attachment")

    def attach_document_to_room(self, room_id: str, document_id: str) -> None:
        response = self._request(
            "POST",
            f"/gateway/rooms/{room_id}/documents/{document_id}",
        )
        self._check(response, "Console room document attachment")

    def room_packages(self, room_id: str) -> list[dict[str, Any]]:
        return self._list(
            f"/gateway/rooms/{room_id}/packages",
            "Console room packages",
        )

    def attach_package_to_room(self, room_id: str, package_id: str) -> None:
        response = self._request(
            "POST",
            f"/gateway/rooms/{room_id}/packages/{package_id}",
        )
        self._check(response, "Console package attachment")

    def linked_rooms(self, room_id: str) -> list[dict[str, Any]]:
        return self._list(
            f"/gateway/rooms/{room_id}/linked-rooms",
            "Console room links",
        )

    def link_room(self, room_id: str, target_room_id: str, label: str) -> None:
        response = self._request(
            "POST",
            f"/gateway/rooms/{room_id}/linked-rooms",
            json={"target_room_id": target_room_id, "label": label},
        )
        self._check(response, "Console room link")

    def create_agent(
        self,
        *,
        name: str,
        description: str,
        display_name: str,
        controller_id: str,
        provider: str,
        instructions: str,
        directory: str,
        model: str | None = None,
        desired_state: str = "running",
        auto_mode: bool = True,
    ) -> dict[str, Any]:
        payload = {
            "name": name,
            "description": description,
            "display_name": display_name,
            "controller_id": controller_id,
            "desired_state": desired_state,
            "definition": {
                "provider": provider,
                "model": model,
                "advanced_config": {},
                "instructions": instructions,
                "auto_mode": auto_mode,
                "auto_approve": False,
                "directory": directory,
                "isolation": "shared",
            },
        }
        response = self._request("POST", "/gateway/management/agents", json=payload)
        if response.status_code == 409:
            raise SwitchError(f"Switch Console already has an agent named {name}")
        self._check(response, "Console agent creation")
        return self._json(response, "Console agent creation")

    def update_agent(
        self,
        agent_id: str,
        *,
        instructions: str,
        directory: str,
        desired_state: str = "running",
        auto_mode: bool = True,
        provider: str = "opencode",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "desired_state": desired_state,
            "definition": {
                "provider": provider,
                "advanced_config": {},
                "instructions": instructions,
                "auto_mode": auto_mode,
                "auto_approve": False,
                "directory": directory,
                "isolation": "shared",
            },
        }
        response = self._request(
            "PATCH",
            f"/gateway/management/agents/{agent_id}",
            json=payload,
        )
        self._check(response, "Console agent update")
        return self._json(response, "Console agent update")

    def create_rooms_from_yaml(self, template: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/gateway/rooms/from-yaml",
            json={"yaml": template},
        )
        self._check(response, "Console room creation")
        return self._json(response, "Console room creation")

    def _list(self, path: str, operation: str) -> list[dict[str, Any]]:
        response = self._request("GET", path)
        self._check(response, operation)
        payload = self._json(response, operation)
        if not isinstance(payload, list):
            raise SwitchError(f"{operation} returned an invalid response")
        return [item for item in payload if isinstance(item, dict)]

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        authenticated: bool = True,
    ) -> httpx.Response:
        if authenticated:
            self._ensure_login()
        try:
            return self._http.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise SwitchError("Switch Console is unavailable") from exc

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self.login()

    def _check(self, response: httpx.Response, operation: str) -> None:
        if response.status_code < 400:
            return
        if response.status_code in {401, 403}:
            raise SwitchError(f"Switch Console rejected the {operation} request")
        if response.status_code == 404:
            raise SwitchError(f"Switch Console could not find the {operation} resource")
        if response.status_code == 409:
            raise SwitchError(f"Switch Console conflict during {operation}")
        if response.status_code == 422:
            raise SwitchError(f"Switch Console rejected the {operation} values")
        if response.status_code == 429:
            raise SwitchError("Switch Console rate limit reached")
        raise SwitchError(f"Switch Console request failed during {operation}")

    def _json(self, response: httpx.Response, operation: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise SwitchError(f"Switch Console returned invalid JSON during {operation}") from exc
