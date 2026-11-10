#!/usr/bin/env python3
"""Check the CodeScan LLM provider before spending CI time on a scan.

The CodeScan action can complete its static inventory while its MCP LLM scanner
silently does not run. This preflight turns a missing key or unavailable model
into a failed job; an exhausted provider quota becomes a skipped scan (not a
pass), because no model can run until the account is funded.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_QUOTA_CODES = {
    "credit_balance_exhausted",
    "insufficient_quota",
    "billing_hard_limit_reached",
    "quota_exceeded",
}


class ProviderQuotaExhausted(Exception):
    """The provider is configured but cannot serve requests until it is funded."""


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
        if exc.code == 429 or detail in _QUOTA_CODES:
            raise ProviderQuotaExhausted(detail or f"HTTP {exc.code}") from None
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
    try:
        _run_provider(provider, model, api_key)
    except ProviderQuotaExhausted as exc:
        return _skip_scan(str(exc))
    print(f"LLM provider preflight passed: {provider}:{model}")
    return 0


def _run_provider(provider: str, model: str, api_key: str) -> None:
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
        raise SystemExit(1)


def _skip_scan(reason: str) -> int:
    message = f"LLM provider cannot serve this scan ({reason}); CodeScan is skipped, not passed."
    print(f"::warning::{message}")
    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("skip=true\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
