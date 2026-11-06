"""Per-agent Console runtime directories backed by signed actor tokens.

Console-managed agents were wired by pointing every agent at one shared
directory and one shared ``opencode.json``.  That also meant one shared MCP
identity, which makes the memory audit log useless for attribution and lets
one compromised agent read approved memory wedges as any actor.  This module
gives each agent its own directory containing an ``opencode.json`` that:

- injects that agent's Ed25519-signed ``INVESTIGATOR_MCP_ACTOR_TOKEN`` plus
  the operator public key into its MCP server environment, so
  ``authorize_actor`` can verify the caller,
- pins an absolute ``INVESTIGATOR_DATA_DIR`` (agents run from their own
  directory; relative paths would otherwise fork the report store), and
- keeps the same tool permissions as the repository config.

Config files carry tokens, so they are written 0600 inside ``.data`` and are
never committed.  The private key stays operator-side under ``mcp-auth``.

Same-uid limit: this separation is credential hygiene, not an OS boundary.
All Console agents currently run as the same operating-system user, so any one
of them can read another agent's config and the shared signing key.  Tokens
also travel in the MCP child environment, so a same-user process can read them
with ``ps -ax -Eww``.  A compromised agent can mint or replay another actor's
token and forge the audit attribution; that account can also open the evidence
DB or audit files directly.  Use a signing service with independent
approval/identity checks, or a separate uid/container per agent, before
treating these names as a security boundary.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from kube_investigator.mcp_auth import (
    PRIVATE_KEY_NAME,
    generate_actor_keypair,
    sign_actor_token,
    tools_for_actor,
)

AGENT_CONFIG_DIR = ".data"
AGENT_CONFIG_SUBDIR = "console-agents"
AUTH_SUBDIR = "mcp-auth"
DEFAULT_TOKEN_TTL_SECONDS = 14 * 24 * 3600


def agent_tools(agent_name: str) -> list[str]:
    """Return the MCP tools granted to one Console agent role."""
    return tools_for_actor(agent_name)


def agent_directories_root(data_dir: Path) -> Path:
    return data_dir / AGENT_CONFIG_SUBDIR


def agent_directory(data_dir: Path, agent_name: str) -> Path:
    return agent_directories_root(data_dir) / agent_name


def mcp_auth_dir(data_dir: Path) -> Path:
    return data_dir / AUTH_SUBDIR


def _permissions() -> dict[str, str]:
    """Allow every kube-investigator tool for Console-managed agents.

    The keys follow the repository ``opencode.json`` name scheme
    (``<server>_<tool>``) so the Console runtime treats a denied tool exactly
    like a remote one: hidden from the tool list.
    """
    return {
        "kube-investigator_": "allow",
        "kube-investigator_kube_investigator_health": "allow",
        "kube-investigator_kube_investigator_run": "allow",
        "kube-investigator_kube_investigator_get": "allow",
        "kube-investigator_kube_investigator_verify": "allow",
        "kube-investigator_kube_investigator_memory": "allow",
        "kube-investigator_kube_investigator_source_status": "allow",
        "kube-investigator_kube_investigator_docs": "allow",
    }


def agent_environment(
    *,
    agent_name: str,
    public_key: str,
    token: str,
    data_dir: Path,
    tenant: str = "local",
) -> dict[str, str]:
    """The MCP environment for one Console agent, per the auth contract."""
    environment = {
        "INVESTIGATOR_MCP_ACTOR": agent_name,
        "INVESTIGATOR_MCP_ACTOR_TOKEN": token,
        "INVESTIGATOR_MCP_PUBLIC_KEY": public_key,
        "INVESTIGATOR_DATA_DIR": str(data_dir.resolve()),
        "INVESTIGATOR_TENANT": tenant,
    }
    for key in (
        "INVESTIGATOR_BACKEND",
        "INVESTIGATOR_KUBE_CONTEXT",
        "INVESTIGATOR_CLUSTER",
        "INVESTIGATOR_NAMESPACES",
    ):
        value = (os.getenv(key) or "").strip()
        if value:
            environment[key] = value
    return environment


def write_agent_config(
    *,
    agent_name: str,
    data_dir: Path,
    auth_dir: Path,
    python_bin: Path,
    token_ttl: int = DEFAULT_TOKEN_TTL_SECONDS,
    tenant: str = "local",
    tools: list[str] | None = None,
) -> Path:
    """Write ``<agent>/opencode.json`` and return the directory.

    Idempotent: the keypair is generated once and reused; tokens are signed
    fresh so ``exp`` stays valid across setup re-runs.
    """
    _private_path, public_key = generate_actor_keypair(auth_dir)
    token = sign_actor_token(
        _private_path,
        agent_name,
        tenant,
        token_ttl,
        tools=agent_tools(agent_name) if tools is None else tools,
    )
    directory = agent_directory(data_dir, agent_name)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    config = {
        "$schema": "https://opencode.ai/config.json",
        "permission": _permissions(),
        "mcp": {
            "kube-investigator": {
                "type": "local",
                "command": [str(python_bin), "-m", "kube_investigator.mcp_server"],
                "enabled": True,
                "environment": agent_environment(
                    agent_name=agent_name,
                    public_key=public_key,
                    token=token,
                    data_dir=data_dir,
                    tenant=tenant,
                ),
            }
        },
    }
    config_path = directory / "opencode.json"
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    os.chmod(config_path, 0o600)
    os.chmod(directory, 0o700)
    return directory


def write_agent_directories(
    *,
    agent_names: list[str],
    data_dir: Path,
    auth_dir: Path,
    python_bin: Path,
    token_ttl: int = DEFAULT_TOKEN_TTL_SECONDS,
    tenant: str = "local",
    rotate_key: bool = False,
    tools: list[str] | None = None,
) -> dict[str, Path]:
    """One directory (and one identity) per Console agent.

    ``rotate_key`` destroys the existing signing keypair before the run, so
    every token is signed with a new key.  Runtimes started before rotation
    keep the old public key and remain valid until they restart; the operator
    must restart them (a Console kickoff does this) to complete revocation.
    """
    if len(set(agent_names)) != len(agent_names):
        raise ValueError("agent names must be unique")
    if rotate_key:
        (auth_dir / PRIVATE_KEY_NAME).unlink(missing_ok=True)
    root = agent_directories_root(data_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    return {
        name: write_agent_config(
            agent_name=name,
            data_dir=data_dir,
            auth_dir=auth_dir,
            python_bin=python_bin,
            token_ttl=token_ttl,
            tenant=tenant,
            tools=tools,
        )
        for name in agent_names
    }


def load_config_environment(config_path: Path) -> dict[str, str]:
    """Read the MCP environment block back out of a written agent config.

    Raises ``ValueError`` for a structurally malformed config instead of
    letting a ``TypeError`` escape from an index into a non-mapping.
    """
    payload = json.loads(config_path.read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"{config_path} is not a JSON object")
    server = payload.get("mcp")
    if not isinstance(server, dict):
        raise ValueError(f"{config_path} has no mcp config block")
    server = server.get("kube-investigator")
    if not isinstance(server, dict):
        raise ValueError(f"{config_path} has no kube-investigator MCP server")
    environment = server.get("environment") or {}
    if not isinstance(environment, dict):
        raise ValueError(f"{config_path} has a malformed MCP environment block")
    return {str(key): str(value) for key, value in environment.items()}


def config_mode_strict(config_path: Path) -> bool:
    mode = stat.S_IMODE(config_path.stat().st_mode)
    return mode == 0o600
