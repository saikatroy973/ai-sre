"""Supply-chain pins for CI actions and the isolated container base image."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHA = re.compile(r"^[0-9a-f]{40}$")


def test_workflow_actions_are_pinned_to_commit_shas():
    for path in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped.startswith("- uses:") and not stripped.startswith("uses:"):
                continue
            reference = stripped.split("uses:", 1)[1].strip().split(" #", 1)[0]
            if reference.startswith("./"):
                continue
            assert "@" in reference, f"{path.name}: unpinned action {reference}"
            _action, revision = reference.rsplit("@", 1)
            assert SHA.fullmatch(revision), f"{path.name}: unpinned action {reference}"


def test_container_base_image_is_pinned_by_digest():
    dockerfile = (REPO / "deploy" / "mcp-container" / "Dockerfile").read_text()
    assert re.search(r"^FROM python:3\.14-slim@sha256:[0-9a-f]{64}$", dockerfile, re.MULTILINE)


def test_dependabot_covers_actions_python_and_docker():
    config = (REPO / ".github" / "dependabot.yml").read_text()
    for ecosystem in ('"github-actions"', '"pip"', '"docker"'):
        assert ecosystem in config
