"""Console agent directories carry per-agent signed MCP actor tokens."""

import json
import os
import stat
from pathlib import Path

import pytest

from kube_investigator.config import Settings
from kube_investigator.mcp_auth import authorize_actor, generate_actor_keypair, sign_actor_token
from kube_investigator.switch.agent_config import (
    DEFAULT_TOKEN_TTL_SECONDS,
    agent_directory,
    config_mode_strict,
    load_config_environment,
    mcp_auth_dir,
    write_agent_config,
    write_agent_directories,
)
from kube_investigator.switch.specs import AGENTS

AGENT_NAMES = [spec.name for spec in AGENTS]


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    monkeypatch.delenv("INVESTIGATOR_MCP_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("INVESTIGATOR_MCP_ACTOR_TOKEN", raising=False)
    monkeypatch.delenv("INVESTIGATOR_MCP_ACTOR", raising=False)
    data_dir = tmp_path / "data"
    auth_dir = mcp_auth_dir(data_dir)
    directories = write_agent_directories(
        agent_names=AGENT_NAMES,
        data_dir=data_dir,
        auth_dir=auth_dir,
        python_bin=Path("/venv/bin/python"),
        token_ttl=60,
    )
    return data_dir, auth_dir, directories


def test_every_console_agent_gets_its_own_directory_and_identity(wired):
    data_dir, _auth_dir, directories = wired
    assert sorted(directories) == sorted(AGENT_NAMES)
    assert len({str(path) for path in directories.values()}) == len(AGENT_NAMES)
    for name, directory in directories.items():
        assert directory == agent_directory(data_dir, name)
        config_path = directory / "opencode.json"
        assert config_path.exists()
        assert config_mode_strict(config_path)
        environment = load_config_environment(config_path)
        assert environment["INVESTIGATOR_MCP_ACTOR"] == name
        # The data dir must be absolute: agents run from their own directory.
        assert os.path.isabs(environment["INVESTIGATOR_DATA_DIR"])
        assert Path(environment["INVESTIGATOR_DATA_DIR"]) == data_dir.resolve()
        payload = json.loads(config_path.read_text())
        server = payload["mcp"]["kube-investigator"]
        assert server["type"] == "local"
        assert server["command"][0] == "/venv/bin/python"
        assert server["command"][1:] == ["-m", "kube_investigator.mcp_server"]


def test_tokens_are_signed_by_one_operator_key_and_verifiable(wired):
    _data_dir, auth_dir, directories = wired
    _private_path, public_key = generate_actor_keypair(auth_dir)
    for name, directory in directories.items():
        environment = load_config_environment(directory / "opencode.json")
        assert environment["INVESTIGATOR_MCP_PUBLIC_KEY"] == public_key
        from kube_investigator.mcp_auth import verify_actor_token

        payload = verify_actor_token(
            environment["INVESTIGATOR_MCP_ACTOR_TOKEN"],
            environment["INVESTIGATOR_MCP_PUBLIC_KEY"],
        )
        assert payload["actor"] == name
        assert payload["tenant"] == environment["INVESTIGATOR_TENANT"]


def test_tokens_expire_and_setup_rewrites_them(wired, tmp_path, monkeypatch):
    _data_dir, auth_dir, directories = wired
    name = AGENT_NAMES[0]
    import time

    _private_path, public_key = generate_actor_keypair(auth_dir)
    short_lived = sign_actor_token(auth_dir / "mcp-actor-private.pem", name, "local", ttl=1)
    from kube_investigator.mcp_auth import verify_actor_token

    # Push the clock past the token's exp to prove expiry, not just wrongness.
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 30)
    with pytest.raises(ValueError, match="expired"):
        verify_actor_token(short_lived, public_key)

    # A setup re-run signs fresh tokens for the same key, not a new key.
    fresh = write_agent_config(
        agent_name=name,
        data_dir=tmp_path / "data",
        auth_dir=auth_dir,
        python_bin=Path("/venv/bin/python"),
    )
    assert config_mode_strict(fresh / "opencode.json")


def test_agent_environment_authorizes_with_the_written_config(monkeypatch, tmp_path):
    name = AGENT_NAMES[0]
    data_dir = tmp_path / "data"
    auth_dir = mcp_auth_dir(data_dir)
    directory = write_agent_config(
        agent_name=name,
        data_dir=data_dir,
        auth_dir=auth_dir,
        python_bin=Path("/venv/bin/python"),
    )
    environment = load_config_environment(directory / "opencode.json")
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    settings = Settings(data_dir=tmp_path / "mcp-data", tenant="local")
    actor, error = authorize_actor(settings)
    assert error is None
    assert actor == name


def test_missing_foreign_and_expired_tokens_fail_closed(wired, monkeypatch):
    _data_dir, _auth_dir, directories = wired
    first, second = AGENT_NAMES[0], AGENT_NAMES[1]

    missing = load_config_environment(directories[first] / "opencode.json")
    missing.pop("INVESTIGATOR_MCP_ACTOR_TOKEN")
    settings = Settings(data_dir=directories[first], tenant="local")
    for key, value in missing.items():
        monkeypatch.setenv(key, value)
    actor, error = authorize_actor(settings)
    assert actor is None
    assert error == "MCP actor token is required"

    # Foreign use: another agent's token presented under the first actor.
    foreign = load_config_environment(directories[first] / "opencode.json")
    second_token = load_config_environment(directories[second] / "opencode.json")[
        "INVESTIGATOR_MCP_ACTOR_TOKEN"
    ]
    foreign["INVESTIGATOR_MCP_ACTOR_TOKEN"] = second_token
    for key, value in foreign.items():
        monkeypatch.setenv(key, value)
    actor, error = authorize_actor(settings)
    assert actor is None
    assert error == "MCP actor does not match the signed token"


def test_keypair_is_reused_across_runs(tmp_path):
    first = generate_actor_keypair(tmp_path / "auth")
    second = generate_actor_keypair(tmp_path / "auth")
    assert first[1] == second[1]
    assert first[0].read_bytes() == second[0].read_bytes()
    assert stat.S_IMODE(first[0].stat().st_mode) == 0o600


def test_default_ttl_is_long_enough_for_a_demo_replaying_later():
    assert DEFAULT_TOKEN_TTL_SECONDS == 14 * 24 * 3600
