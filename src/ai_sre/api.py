import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ai_sre.auth import Principal
from ai_sre.config import Settings
from ai_sre.coordinator import Coordinator, build_coordinator
from ai_sre.errors import (
    AuthorizationError,
    BusyError,
    ConfigurationError,
    InvestigatorError,
    NotFoundError,
)
from ai_sre.models import AgentMessage, Evidence, Investigation, InvestigationRequest


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = 8192):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                await JSONResponse({"detail": "Request too large"}, status_code=413)(
                    scope, receive, send
                )
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(settings: Settings | None = None, coordinator: Coordinator | None = None) -> FastAPI:
    settings = settings if settings is not None else Settings.from_env()
    if not settings.api_token or len(settings.api_token.encode()) < 32:
        raise ConfigurationError("The API requires an AI_SRE_API_TOKEN of at least 32 bytes")
    service = coordinator if coordinator is not None else build_coordinator(settings)
    principal = Principal.operator(settings, subject="api-operator")
    bearer = HTTPBearer(auto_error=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            service.close()

    app = FastAPI(
        title="AI SRE",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(BodyLimitMiddleware)
    app.state.coordinator = service

    def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Principal:
        from fastapi import HTTPException

        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode(), (settings.api_token or "").encode()
        ):
            raise HTTPException(
                status_code=401,
                detail="Valid bearer authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return principal

    @app.exception_handler(InvestigatorError)
    async def investigator_error(request: Request, exc: InvestigatorError) -> JSONResponse:
        if isinstance(exc, AuthorizationError):
            return JSONResponse(
                {"detail": "Target is outside the authorized scope"}, status_code=403
            )
        if isinstance(exc, NotFoundError):
            return JSONResponse({"detail": "Investigation not found"}, status_code=404)
        if isinstance(exc, BusyError):
            return JSONResponse(
                {"detail": "Investigation capacity reached"},
                status_code=429,
                headers={"Retry-After": "1"},
            )
        return JSONResponse({"detail": "Investigation could not complete"}, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Validation errors must not echo questions or unexpected credential fields.
        return JSONResponse({"detail": "Invalid investigation request"}, status_code=422)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/investigations", status_code=201)
    def investigate(
        payload: InvestigationRequest,
        authenticated: Annotated[Principal, Depends(authenticate)],
    ) -> Investigation:
        return service.run(payload, authenticated)

    @app.get("/v1/investigations/{investigation_id}")
    def get_investigation(
        investigation_id: UUID,
        authenticated: Annotated[Principal, Depends(authenticate)],
    ) -> Investigation:
        return service.get(investigation_id, authenticated)

    @app.get("/v1/investigations/{investigation_id}/messages")
    def get_messages(
        investigation_id: UUID,
        authenticated: Annotated[Principal, Depends(authenticate)],
    ) -> list[AgentMessage]:
        service.get(investigation_id, authenticated)
        return service.store.messages(investigation_id)

    @app.get("/v1/investigations/{investigation_id}/evidence")
    def get_evidence(
        investigation_id: UUID,
        authenticated: Annotated[Principal, Depends(authenticate)],
    ) -> list[Evidence]:
        service.get(investigation_id, authenticated)
        return service.store.evidence(investigation_id)

    return app
