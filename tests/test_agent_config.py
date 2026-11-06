"""Console agent directories carry per-agent signed MCP actor tokens."""

import json
import os
import stat
from pathlib import Path

import pytest

from kube_investigator.config import Settings
from kube_investigator.mcp_auth import (
    authorize_actor,
    authorize_tool,
    generate_actor_keypair,
    sign_actor_token,
)
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
        assert payload["tools"], f"{name} token has no tool claim"


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


def test_wiring_is_repeatable_and_rotates_tokens(wired):
    """A setup re-run is the rotation path: it must not fail on existing dirs."""
    data_dir, auth_dir, _directories = wired
    first = {
        name: load_config_environment(agent_directory(data_dir, name) / "opencode.json")[
            "INVESTIGATOR_MCP_ACTOR_TOKEN"
        ]
        for name in AGENT_NAMES
    }
    directories = write_agent_directories(
        agent_names=AGENT_NAMES,
        data_dir=data_dir,
        auth_dir=auth_dir,
        python_bin=Path("/venv/bin/python"),
        # Ed25519 is deterministic: the same key over the same payload in the
        # same second gives identical bytes. A changed TTL changes the payload
        # and is how an operator forces a fresh token without waiting.
        token_ttl=120,
    )
    assert sorted(directories) == sorted(AGENT_NAMES)
    for name in AGENT_NAMES:
        config_path = agent_directory(data_dir, name) / "opencode.json"
        assert config_mode_strict(config_path)
        rotated = load_config_environment(config_path)["INVESTIGATOR_MCP_ACTOR_TOKEN"]
        assert rotated != first[name], f"{name} token was not rotated"
    # The keypair is reused so previously issued, unexpired tokens stay valid.
    env = load_config_environment(agent_directory(data_dir, AGENT_NAMES[0]) / "opencode.json")
    settings = Settings(tenant="local", data_dir=data_dir)
    os.environ["INVESTIGATOR_MCP_PUBLIC_KEY"] = env["INVESTIGATOR_MCP_PUBLIC_KEY"]
    os.environ["INVESTIGATOR_MCP_ACTOR"] = AGENT_NAMES[0]
    os.environ["INVESTIGATOR_MCP_ACTOR_TOKEN"] = env["INVESTIGATOR_MCP_ACTOR_TOKEN"]
    try:
        actor, error = authorize_actor(settings)
    finally:
        for key in (
            "INVESTIGATOR_MCP_PUBLIC_KEY",
            "INVESTIGATOR_MCP_ACTOR",
            "INVESTIGATOR_MCP_ACTOR_TOKEN",
        ):
            os.environ.pop(key, None)
    assert (actor, error) == (AGENT_NAMES[0], None)


def test_rotation_replaces_the_signing_key_and_invalidates_old_tokens(wired):
    from cryptography.exceptions import InvalidSignature

    from kube_investigator.mcp_auth import verify_actor_token

    data_dir, auth_dir, _directories = wired
    config_path = agent_directory(data_dir, "incident-commander") / "opencode.json"
    before = load_config_environment(config_path)

    write_agent_directories(
        agent_names=AGENT_NAMES,
        data_dir=data_dir,
        auth_dir=auth_dir,
        python_bin=Path("/venv/bin/python"),
        token_ttl=60,
        rotate_key=True,
    )
    after = load_config_environment(config_path)
    assert after["INVESTIGATOR_MCP_PUBLIC_KEY"] != before["INVESTIGATOR_MCP_PUBLIC_KEY"]
    assert after["INVESTIGATOR_MCP_ACTOR_TOKEN"] != before["INVESTIGATOR_MCP_ACTOR_TOKEN"]
    payload = verify_actor_token(
        after["INVESTIGATOR_MCP_ACTOR_TOKEN"],
        after["INVESTIGATOR_MCP_PUBLIC_KEY"],
    )
    assert payload["actor"] == "incident-commander"
    with pytest.raises(InvalidSignature):
        verify_actor_token(
            before["INVESTIGATOR_MCP_ACTOR_TOKEN"],
            after["INVESTIGATOR_MCP_PUBLIC_KEY"],
        )


def test_existing_key_material_permissions_are_tightened(wired):
    data_dir, auth_dir, _directories = wired
    key_path = auth_dir / "mcp-actor-private.pem"
    os.chmod(auth_dir, 0o755)
    os.chmod(key_path, 0o644)

    write_agent_directories(
        agent_names=AGENT_NAMES,
        data_dir=data_dir,
        auth_dir=auth_dir,
        python_bin=Path("/venv/bin/python"),
        token_ttl=60,
    )
    assert stat.S_IMODE(auth_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_tokens_carry_role_specific_tool_claims(wired):
    from kube_investigator.mcp_auth import verify_actor_token

    _data_dir, _auth_dir, directories = wired
    expected = {
        "incident-commander": {"docs", "get", "memory", "run", "source_status", "verify"},
        "cluster-investigator": {"docs", "get", "source_status", "verify"},
        "telemetry-analyst": {"docs", "get", "source_status"},
        "change-analyst": {"docs", "get", "source_status"},
        "memory-researcher": {"docs", "memory"},
        "evidence-verifier": {"docs", "get", "verify"},
    }
    for name, directory in directories.items():
        environment = load_config_environment(directory / "opencode.json")
        payload = verify_actor_token(
            environment["INVESTIGATOR_MCP_ACTOR_TOKEN"],
            environment["INVESTIGATOR_MCP_PUBLIC_KEY"],
        )
        assert set(payload["tools"]) == expected[name]


def test_generated_memory_token_cannot_call_evidence_tools(wired, monkeypatch):
    _data_dir, _auth_dir, directories = wired
    environment = load_config_environment(directories["memory-researcher"] / "opencode.json")
    settings = Settings(data_dir=directories["memory-researcher"], tenant="local")
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    assert authorize_tool(settings, "memory")[0] == "memory-researcher"
    actor, error = authorize_tool(settings, "run")
    assert actor == "memory-researcher"
    assert error == "MCP actor is not authorized for this tool"
