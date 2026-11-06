"""Kubernetes documentation index and the read-only MCP docs tool."""

import base64
import json
import time

from cryptography.hazmat.primitives import serialization

from kube_investigator.knowledge import build_index, parse_document, search_docs
from kube_investigator.mcp_auth import generate_actor_keypair
from kube_investigator.mcp_server import kube_investigator_docs

SERVICE_DOC = """---
title: Service
content_type: concept
---

A Service exposes a set of Pods as a network service. If a Service has no
endpoints, its selector matches no ready Pods. Check the selector and the
EndpointSlices before changing anything.
"""

PROBE_DOC = """---
title: Probes
---

## Readiness probe

A readiness probe that fails keeps the container running but removes the Pod
from Service endpoints.
"""


def make_docs(tmp_path):
    source = tmp_path / "docs"
    (source / "concepts" / "services").mkdir(parents=True)
    (source / "concepts" / "workloads").mkdir(parents=True)
    (source / "concepts" / "services" / "service.md").write_text(SERVICE_DOC)
    (source / "concepts" / "workloads" / "_index.md").write_text(PROBE_DOC)
    knowledge = tmp_path / "knowledge"
    (knowledge / "cluster-investigator").mkdir(parents=True)
    (knowledge / "telemetry-analyst").mkdir(parents=True)
    (knowledge / "cluster-investigator" / "playbook.md").write_text(
        "# Cluster playbook\n\nCheck the Service selector and EndpointSlices.\n"
    )
    (knowledge / "telemetry-analyst" / "playbook.md").write_text(
        "# Telemetry playbook\n\nRead logs and metrics with bounded ranges.\n"
    )
    index = tmp_path / "k8s-docs" / "docs.sqlite3"
    stats = build_index(source, index, extra=((knowledge, "knowledge"),))
    return index, stats


def test_parse_document_strips_front_matter_and_shortcodes():
    title, sections = parse_document(
        "---\ntitle: Example\n---\n\n## Heading\n\nText with {{< note >}}shortcode{{< /note >}}.\n"
    )
    assert title == "Example"
    assert sections[0][0] == "Heading"
    assert "shortcode" in sections[0][1]


def test_build_and_search_docs(tmp_path):
    index, stats = make_docs(tmp_path)
    assert stats["documents"] == 4
    results = search_docs(tmp_path, "service has no endpoints")
    assert results
    assert results[0]["url"].startswith("https://kubernetes.io/docs/")
    assert "endpoints" in results[0]["passage"]


def test_search_without_an_index_returns_nothing(tmp_path):
    assert search_docs(tmp_path, "readiness probe") == []


def test_role_knowledge_is_scoped_and_cited(tmp_path):
    make_docs(tmp_path)
    cluster = search_docs(tmp_path, "endpointslices", 5, agent="cluster-investigator")
    telemetry = search_docs(tmp_path, "logs metrics", 5, agent="telemetry-analyst")
    assert any(r["path"].startswith("knowledge/cluster-investigator/") for r in cluster)
    assert not any(r["path"].startswith("knowledge/telemetry-analyst/") for r in cluster)
    assert any(r["path"].startswith("knowledge/telemetry-analyst/") for r in telemetry)
    assert not any(r["path"].startswith("knowledge/cluster-investigator/") for r in telemetry)
    knowledge_hits = [r for r in cluster if r["path"].startswith("knowledge/")]
    assert knowledge_hits[0]["url"].startswith("knowledge/cluster-investigator/")
    global_hits = search_docs(tmp_path, "readiness probe", 5)
    assert all(not r["path"].startswith("knowledge/") for r in global_hits)


def configure(monkeypatch, tmp_path, index):
    monkeypatch.setenv("INVESTIGATOR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("INVESTIGATOR_DOCS_INDEX", str(index))
    monkeypatch.setenv("INVESTIGATOR_SWITCH_MODE", "off")


def token(private_path, tools):
    private_key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    payload = {
        "actor": "cluster-investigator",
        "tenant": "local",
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
        "tools": tools,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    return f"{encode(raw)}.{encode(private_key.sign(raw))}"


def test_docs_tool_returns_cited_results_in_dev_mode(monkeypatch, tmp_path):
    index, _stats = make_docs(tmp_path)
    configure(monkeypatch, tmp_path, index)
    result = kube_investigator_docs("service endpoints", 3)
    assert result["ok"] is True
    assert result["count"] >= 1
    assert result["results"][0]["url"].startswith("https://kubernetes.io/docs/")


def test_docs_tool_requires_the_tool_claim(monkeypatch, tmp_path):
    index, _stats = make_docs(tmp_path)
    configure(monkeypatch, tmp_path, index)
    private_key, public_key = generate_actor_keypair(tmp_path / "auth")
    monkeypatch.setenv("INVESTIGATOR_MCP_PUBLIC_KEY", public_key)
    monkeypatch.setenv("INVESTIGATOR_MCP_ACTOR", "cluster-investigator")
    monkeypatch.setenv(
        "INVESTIGATOR_MCP_ACTOR_TOKEN",
        token(private_key, ["get"]),
    )
    result = kube_investigator_docs("service endpoints", 3)
    assert result["ok"] is False
    assert result["error"] == "MCP actor is not authorized for this tool"


def test_docs_tool_reports_a_missing_index(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path, tmp_path / "missing.sqlite3")
    result = kube_investigator_docs("service", 3)
    assert result["ok"] is False
    assert "not built" in result["error"]
