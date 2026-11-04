"""Agent identities and their one-time Switch API keys.

Keys are returned once by registration and are stored locally in a mode-0600
file.  They are never printed, persisted in investigation records, or sent to a
model.  The file is an operational credential store, not a database: losing it
means re-registering or rotating the agent keys in Switch.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kube_investigator.errors import ConfigurationError
from kube_investigator.switch.specs import AGENTS_BY_NAME


@dataclass(frozen=True)
class AgentCredential:
    name: str
    agent_id: str
    api_key: str = field(repr=False)


class CredentialStore:
    def __init__(self, path: Path, base_url: str):
        self.path = path
        self.base_url = base_url.rstrip("/")

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> dict[str, AgentCredential]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError) as exc:
            raise ConfigurationError("Switch credential file is unreadable") from exc
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise ConfigurationError("Switch credential file has an unsupported format")
        if str(raw.get("base_url", "")).rstrip("/") != self.base_url:
            raise ConfigurationError(
                "Switch credentials belong to a different base URL; run switch bootstrap again"
            )
        agents = raw.get("agents")
        if not isinstance(agents, dict):
            raise ConfigurationError("Switch credential file has no agents")
        result: dict[str, AgentCredential] = {}
        for name, entry in agents.items():
            if not isinstance(entry, dict):
                raise ConfigurationError("Switch credential file is malformed")
            agent_id = entry.get("agent_id")
            api_key = entry.get("api_key")
            if (
                name not in AGENTS_BY_NAME
                or not isinstance(agent_id, str)
                or not agent_id
                or not isinstance(api_key, str)
                or not api_key
            ):
                raise ConfigurationError("Switch credential file is malformed")
            result[name] = AgentCredential(name=name, agent_id=agent_id, api_key=api_key)
        return result

    def save(self, credentials: dict[str, AgentCredential]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload: dict[str, Any] = {
            "version": 1,
            "base_url": self.base_url,
            "agents": {
                name: {"agent_id": credential.agent_id, "api_key": credential.api_key}
                for name, credential in sorted(credentials.items())
            },
        }
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)

    def get(self, name: str) -> AgentCredential:
        try:
            return self.load()[name]
        except KeyError as exc:
            raise ConfigurationError(
                f"Switch agent {name} is not registered; run switch bootstrap"
            ) from exc
