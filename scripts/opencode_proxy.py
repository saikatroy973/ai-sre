"""Local OpenAI-compatible proxy for the Switch Trust scanner.

The pinned scanner image uses litellm with an OpenAI-compatible provider.  The
stock CodeScan action cannot pass a custom base URL or the OpenCode Go session
header, so this proxy adds both while keeping the scanner image and Trust
upload path unchanged.

Run with:
    OPENCODE_GO_API_KEY=... python scripts/opencode_proxy.py
"""

from __future__ import annotations

import json
import os

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

UPSTREAM = os.getenv("OPENCODE_UPSTREAM", "https://opencode.ai/zen/go")
SESSION = os.getenv("OPENCODE_SESSION", "77777777-7777-4777-8777-777777777777")
DROP_HEADERS = {
    "host",
    "authorization",
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "connection",
}

app = FastAPI(title="OpenCode Go proxy for the Switch Trust scanner")


def strip_strict(value: object) -> None:
    """Remove JSON-schema ``strict`` flags that some providers reject."""
    if isinstance(value, dict):
        value.pop("strict", None)
        for item in value.values():
            strip_strict(item)
    elif isinstance(value, list):
        for item in value:
            strip_strict(item)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def proxy(path: str, request: Request) -> Response:
    api_key = os.getenv("OPENCODE_GO_API_KEY")
    if not api_key:
        return Response(
            content=json.dumps({"error": {"message": "OPENCODE_GO_API_KEY is not set"}}),
            status_code=503,
            media_type="application/json",
        )
    body = await request.body()
    if body and "json" in request.headers.get("content-type", ""):
        try:
            payload = json.loads(body)
            strip_strict(payload)
            body = json.dumps(payload).encode()
        except ValueError:
            pass
    headers = {
        key: value for key, value in request.headers.items() if key.lower() not in DROP_HEADERS
    }
    headers["authorization"] = f"Bearer {api_key}"
    headers["x-opencode-session"] = SESSION
    async with httpx.AsyncClient(timeout=300) as client:
        upstream = await client.request(
            request.method,
            f"{UPSTREAM}/{path}",
            content=body,
            headers=headers,
            params=request.query_params,
        )
    if upstream.status_code >= 400:
        print(
            "UPSTREAM_ERROR",
            request.method,
            path,
            upstream.status_code,
            upstream.text[:3000],
            flush=True,
        )
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers={
            key: value for key, value in upstream.headers.items() if key.lower() not in DROP_HEADERS
        },
    )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8110")))
