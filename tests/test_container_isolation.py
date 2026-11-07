"""The isolated MCP container refuses root, writes, and the network."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CONTAINER_DIR = REPO / "deploy" / "mcp-container"
IMAGE = "ai-sre-mcp:isolated"


def _docker_available() -> bool:
    # CI sets this in the matrix jobs: the dedicated `isolation` job already
    # runs the full gate once, and rebuilding the image per Python version is
    # pure duplication. Local runs leave it unset and get the gate inline.
    if os.getenv("SKIP_CONTAINER_GATE") == "1":
        return False
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return probe.returncode == 0


def test_verify_script_pins_the_hardening_flags():
    script = (CONTAINER_DIR / "verify.sh").read_text()
    for flag in ("--read-only", "--cap-drop ALL", "--network none", "--pids-limit"):
        assert flag in script, f"missing hardening flag: {flag}"


def test_image_runtime_is_not_root_and_lacks_a_shell_login():
    dockerfile = (CONTAINER_DIR / "Dockerfile").read_text()
    assert "USER 10001:10001" in dockerfile
    assert "/usr/sbin/nologin" in dockerfile
    assert "no-new-privileges" in (CONTAINER_DIR / "verify.sh").read_text()


@pytest.mark.skipif(not _docker_available(), reason="docker server not reachable")
def test_isolation_probes_pass_inside_the_hardened_container(tmp_path):
    result = subprocess.run(
        ["bash", str(CONTAINER_DIR / "verify.sh"), IMAGE],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(REPO),
    )
    if result.returncode != 0:
        pytest.fail(
            "isolation gate failed:\n"
            f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-2000:]}"
        )
    for probe in ("uid-isolation", "filesystem-isolation", "network-isolation"):
        assert f"PASS {probe}" in result.stdout
    assert "ISOLATION-OK" in result.stdout
