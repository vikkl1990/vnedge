"""FastAPI routes for the operator settings surface.

The route module knows nothing about trading runtime controls.  It accepts an
authentication callback from the dashboard and exposes only profile and
credential-lifecycle operations.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, SecretStr

from vnedge.dashboard.auth import AuthResult
from vnedge.settings.exchange_connections import (
    ExchangeId,
    KeyPurpose,
    SettingsService,
    TestRateLimitError,
)


class ProfileUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    timezone: str = Field(min_length=1, max_length=80)


class ExchangeUpsert(BaseModel):
    api_key: SecretStr
    api_secret: SecretStr
    purpose: KeyPurpose
    withdrawal_disabled_ack: bool = False


Authorize = Callable[[Request], AuthResult]
CsrfGuard = Callable[[Request], None]
IssueSession = Callable[[AuthResult], JSONResponse]


def mount_settings_routes(
    app: FastAPI,
    *,
    service: SettingsService,
    authorize: Authorize,
    require_csrf: CsrfGuard,
    issue_session: IssueSession,
) -> None:
    """Mount the scoped settings API on the dashboard app."""

    def _actor(request: Request, *, mutate: bool = False) -> AuthResult:
        user = authorize(request)
        if mutate:
            require_csrf(request)
        return user

    @app.get("/api/settings/security")
    async def settings_security(request: Request) -> JSONResponse:
        user = _actor(request)
        return JSONResponse(
            {
                "session": "short_lived_http_only_cookie",
                "secrets_store_ready": service.secrets_ready,
                "live_controls_available": False,
                "operator_id": user.name,
            }
        )

    @app.get("/api/settings/profile")
    async def get_settings_profile(request: Request) -> JSONResponse:
        user = _actor(request)
        return JSONResponse(service.profile(user.name or "operator").public_dict())

    @app.put("/api/settings/profile")
    async def put_settings_profile(request: Request, body: ProfileUpdate) -> JSONResponse:
        user = _actor(request, mutate=True)
        try:
            profile = service.update_profile(
                user.name or "operator", body.display_name, body.timezone
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return JSONResponse(profile.public_dict())

    @app.get("/api/settings/exchanges")
    async def get_settings_exchanges(request: Request) -> JSONResponse:
        _actor(request)
        return JSONResponse([item.public_dict() for item in service.list_connections()])

    @app.put("/api/settings/exchanges/{exchange}")
    async def put_settings_exchange(
        exchange: ExchangeId, request: Request, body: ExchangeUpsert
    ) -> JSONResponse:
        user = _actor(request, mutate=True)
        try:
            item = service.upsert(
                exchange,
                body.purpose,
                body.api_key.get_secret_value(),
                body.api_secret.get_secret_value(),
                user.name or "operator",
                withdrawal_disabled_ack=body.withdrawal_disabled_ack,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse(item.public_dict())

    @app.post("/api/settings/exchanges/{exchange}/test")
    async def test_settings_exchange(exchange: ExchangeId, request: Request) -> JSONResponse:
        user = _actor(request, mutate=True)
        try:
            item, latency_ms = await service.test_connection(exchange, user.name or "operator")
        except TestRateLimitError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({**item.public_dict(), "latency_ms": latency_ms})

    @app.post("/api/settings/exchanges/{exchange}/disable")
    async def disable_settings_exchange(exchange: ExchangeId, request: Request) -> JSONResponse:
        user = _actor(request, mutate=True)
        try:
            item = service.disable(exchange, user.name or "operator")
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return JSONResponse(item.public_dict())

    @app.delete("/api/settings/exchanges/{exchange}")
    async def delete_settings_exchange(exchange: ExchangeId, request: Request) -> Response:
        user = _actor(request, mutate=True)
        if not service.delete(exchange, user.name or "operator"):
            raise HTTPException(status_code=404, detail="connection is not configured")
        return Response(status_code=204)

    @app.post("/api/settings/session/rotate")
    async def rotate_settings_session(request: Request) -> JSONResponse:
        user = _actor(request, mutate=True)
        return issue_session(user)
