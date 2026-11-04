#!/usr/bin/env python3
"""Check the CodeScan LLM provider before spending CI time on a scan.

The CodeScan action can complete its static inventory while its MCP LLM scanner
silently does not run. This preflight turns a missing key, exhausted quota, or
unavailable model into a failed job instead of a green run with incomplete
inventory.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _post(url: str, headers: dict[str, str], body: dict) -> None:
    request = Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            if response.status < 200 or response.status >= 300:
                raise SystemExit(f"LLM provider returned HTTP {response.status}")
    except HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read())
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                detail = str(error.get("code") or error.get("type") or "")
        except (ValueError, OSError):
            pass
        suffix = f" ({detail})" if detail else ""
        raise SystemExit(f"LLM provider returned HTTP {exc.code}{suffix}") from None
    except (URLError, TimeoutError) as exc:
        raise SystemExit(f"LLM provider request failed: {type(exc).__name__}") from None


def main() -> int:
    model_string = os.getenv("SWITCH_TRUST_LLM_MODEL", "")
    api_key = os.getenv("SWITCH_TRUST_LLM_API_KEY", "")
    if ":" not in model_string:
        print("SWITCH_TRUST_LLM_MODEL must be provider:model", file=sys.stderr)
        return 1
    provider, model = model_string.split(":", 1)
    provider = provider.lower()
    model = model.strip()
    if not model:
        print("SWITCH_TRUST_LLM_MODEL must name a model", file=sys.stderr)
        return 1
    if not api_key:
        print("SWITCH_TRUST_LLM_API_KEY is required", file=sys.stderr)
        return 1
    if provider == "openai":
        _post(
            "https://api.openai.com/v1/chat/completions",
            {"Authorization": f"Bearer {api_key}"},
            {
                "model": model,
                "messages": [{"role": "user", "content": "Reply OK"}],
                "max_tokens": 1,
            },
        )
    elif provider == "anthropic":
        _post(
            "https://api.anthropic.com/v1/messages",
            {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            {
                "model": model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "Reply OK"}],
            },
        )
    elif provider in {"gemini", "google"}:
        _post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            {"x-goog-api-key": api_key},
            {"contents": [{"parts": [{"text": "Reply OK"}]}]},
        )
    else:
        print(
            "Unsupported provider in SWITCH_TRUST_LLM_MODEL; expected "
            "openai, anthropic, gemini, or google",
            file=sys.stderr,
        )
        return 1
    print(f"LLM provider preflight passed: {provider}:{model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
