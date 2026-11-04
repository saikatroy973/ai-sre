import json
from pathlib import Path

from kube_investigator.cli import main
from kube_investigator.switch.runtime import FakeSwitchRuntime


def test_switch_status_reports_off_without_touching_the_network(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("INVESTIGATOR_SWITCH_MODE", "off")
    assert main(["switch", "status", "--json", "--state-dir", str(tmp_path / "switch")]) == 0
    assert json.loads(capsys.readouterr().out) == {"mode": "off"}


def test_switch_bootstrap_fails_closed_without_a_registration_token(tmp_path, capsys):
    assert main(["switch", "bootstrap", "--state-dir", str(tmp_path / "switch")]) == 1
    error = capsys.readouterr().err
    assert "registration token" in error.lower()


def test_switch_demo_uses_the_room_runtime_when_available(tmp_path, monkeypatch, capsys):
    fake = FakeSwitchRuntime()
    monkeypatch.setattr("kube_investigator.coordinator.build_switch_runtime", lambda settings: fake)
    assert (
        main(
            [
                "switch-demo",
                "--json",
                "--data-dir",
                str(tmp_path / "data"),
                "--state-dir",
                str(tmp_path / "switch"),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["switch_base_url"] == "fake://switch"
    assert result["switch_room_id"]
    assert result["report"]["outcome"] == "supported"
    room = fake.room_by_id(result["switch_room_id"])
    assert room.docs["final-report"]["creator"] == "incident-commander"
    assert fake.closed is True


def test_switch_demo_fails_closed_without_a_runtime(tmp_path, capsys):
    assert (
        main(
            [
                "switch-demo",
                "--data-dir",
                str(tmp_path / "data"),
                "--state-dir",
                str(tmp_path / "switch"),
            ]
        )
        == 1
    )
    assert "not all agents are registered" in capsys.readouterr().err


def test_memory_cli_approves_lists_and_searches_a_supported_report(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    assert main(["demo", "--json", "--data-dir", str(data)]) == 0
    first = json.loads(capsys.readouterr().out)
    monkeypatch.setenv("INVESTIGATOR_SWITCH_MODE", "off")

    assert (
        main(
            [
                "switch",
                "memory",
                "approve",
                "--investigation",
                first["id"],
                "--reviewer",
                "alice",
                "--title",
                "Checkout selector mismatch",
                "--summary",
                "The checkout Service selector excluded the ready Deployment.",
                "--data-dir",
                str(data),
                "--json",
            ]
        )
        == 0
    )
    approved = json.loads(capsys.readouterr().out)
    record_id = approved["record"]["id"]
    assert approved["switch_document_id"] is None

    assert (
        main(
            [
                "switch",
                "memory",
                "list",
                "--data-dir",
                str(data),
                "--json",
            ]
        )
        == 0
    )
    listed = json.loads(capsys.readouterr().out)
    assert [record["id"] for record in listed] == [record_id]

    assert (
        main(
            [
                "switch",
                "memory",
                "search",
                "--query",
                "checkout selector",
                "--data-dir",
                str(data),
                "--json",
            ]
        )
        == 0
    )
    leads = json.loads(capsys.readouterr().out)
    assert [lead["record_id"] for lead in leads] == [record_id]
    assert leads[0]["trust"] == "historical_lead_not_evidence"
    assert "Switch" not in json.dumps(leads)


def test_memory_approval_is_not_indexed_when_required_switch_is_unavailable(
    tmp_path, monkeypatch, capsys
):
    data = tmp_path / "data"
    assert main(["demo", "--json", "--data-dir", str(data)]) == 0
    first = json.loads(capsys.readouterr().out)
    monkeypatch.setenv("INVESTIGATOR_SWITCH_MODE", "required")
    assert (
        main(
            [
                "switch",
                "memory",
                "approve",
                "--investigation",
                first["id"],
                "--reviewer",
                "alice",
                "--title",
                "Checkout selector mismatch",
                "--data-dir",
                str(data),
                "--state-dir",
                str(tmp_path / "switch"),
                "--json",
            ]
        )
        == 1
    )
    assert "not all agents are registered" in capsys.readouterr().err

    monkeypatch.setenv("INVESTIGATOR_SWITCH_MODE", "off")
    assert (
        main(
            [
                "switch",
                "memory",
                "list",
                "--data-dir",
                str(data),
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == []


def test_memory_approval_refuses_an_unsupported_report(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    assert main(["demo", "--scenario", "healthy", "--json", "--data-dir", str(data)]) == 0
    first = json.loads(capsys.readouterr().out)
    monkeypatch.setenv("INVESTIGATOR_SWITCH_MODE", "off")
    assert (
        main(
            [
                "switch",
                "memory",
                "approve",
                "--investigation",
                first["id"],
                "--reviewer",
                "alice",
                "--title",
                "Healthy checkout",
                "--data-dir",
                str(data),
            ]
        )
        == 1
    )
    assert "supported report" in capsys.readouterr().err


def test_switch_up_collab_selects_the_mattermost_profile(tmp_path, monkeypatch, capsys):
    calls = []

    class Result:
        returncode = 0

    def fake_run(command, check):
        calls.append(command)
        return Result()

    monkeypatch.setattr("kube_investigator.cli.subprocess.run", fake_run)
    state = tmp_path / "switch"
    assert (
        main(
            [
                "switch",
                "up",
                "--collab",
                "--state-dir",
                str(state),
                "--api-port",
                "18000",
                "--gateway-port",
                "13000",
                "--postgres-port",
                "15432",
            ]
        )
        == 0
    )
    command = calls[0]
    assert command[command.index("--profile") + 1] == "gateway"
    assert command.count("--profile") == 2
    assert command[command.index("--profile", command.index("--profile") + 1) + 1] == "collab"
    env_text = (state / ".env").read_text()
    assert "MATTERMOST_ADMIN_PASSWORD=" in env_text
    assert "Mattermost: http://127.0.0.1:8065" in capsys.readouterr().out


def test_compose_file_exists_for_the_switch_up_command():
    root = Path(__file__).resolve().parents[1]
    compose = root / "deploy" / "switch" / "compose.yaml"
    assert compose.exists()
    text = compose.read_text()
    assert "switch-core" in text or "switch:" in text
    assert "SWITCH_VERSION" in text


def test_generated_switch_environment_satisfies_the_compose_stack(tmp_path):
    import re

    from kube_investigator.cli import _ensure_switch_env

    state = tmp_path / "switch"
    env_file = _ensure_switch_env(
        state, version="0.31.0", api_port=18000, gateway_port=13000, postgres_port=15432
    )
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line)
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert values["SWITCH_VERSION"] == "0.31.0"
    key_id, secret = values["SECRET_KEYS"].split(":", 1)
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,32}", key_id)
    assert len(secret) >= 32
    for name in (
        "DB_USER",
        "DB_PASSWORD",
        "DB_OWNER_USER",
        "DB_OWNER_PASSWORD",
        "DB_NAME",
        "AGENT_REGISTRATION_TOKEN",
        "GATEWAY_ADMIN_EMAIL",
        "GATEWAY_ADMIN_PASSWORD",
        "FRONTEND_BASE_URL",
        "GATEWAY_PUBLIC_URL",
        "MATTERMOST_ADMIN_USER",
        "MATTERMOST_ADMIN_PASSWORD",
        "MATTERMOST_TEAM_NAME",
        "MATTERMOST_USER",
        "MATTERMOST_USER_PASSWORD",
    ):
        assert values[name]
    assert values["API_HOST_PORT"] == "18000"
    assert values["GATEWAY_HOST_PORT"] == "13000"
    assert values["POSTGRES_HOST_PORT"] == "15432"
    original = env_file.read_text()
    assert (
        _ensure_switch_env(state, version="9.9.9", api_port=1, gateway_port=2, postgres_port=3)
        == env_file
    )
    assert env_file.read_text() == original


def test_existing_switch_environment_gets_missing_mattermost_values(tmp_path):
    from kube_investigator.cli import _ensure_switch_env

    state = tmp_path / "switch"
    state.mkdir()
    env_file = state / ".env"
    env_file.write_text("SWITCH_VERSION=0.31.0\nDB_PASSWORD=keep-me\n")
    _ensure_switch_env(
        state, version="0.31.0", api_port=18000, gateway_port=13000, postgres_port=15432
    )
    text = env_file.read_text()
    assert "DB_PASSWORD=keep-me" in text
    assert "MATTERMOST_ADMIN_PASSWORD=" in text
    assert "MATTERMOST_USER_PASSWORD=" in text
    assert env_file.stat().st_mode & 0o777 == 0o600
