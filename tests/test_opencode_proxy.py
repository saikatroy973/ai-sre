import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient


def load_proxy():
    path = Path(__file__).resolve().parents[1] / "scripts" / "opencode_proxy.py"
    spec = importlib.util.spec_from_file_location("opencode_proxy", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_proxy_strips_strict_flags_recursively():
    proxy = load_proxy()
    payload = {
        "tools": [
            {"type": "function", "function": {"name": "x", "strict": True}},
        ],
        "response_format": {"json_schema": {"strict": True, "schema": {"type": "object"}}},
    }
    proxy.strip_strict(payload)
    assert "strict" not in payload["tools"][0]["function"]
    assert "strict" not in payload["response_format"]["json_schema"]


def test_proxy_health_is_local():
    proxy = load_proxy()
    response = TestClient(proxy.app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
