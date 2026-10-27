"""FastAPI application factory for the GAUR BANK simulator."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import replace

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import auth as auth_routes
from app.api import banking as banking_routes
from app.api import system as system_routes
from app.api import ws as ws_routes
from app.config import BACKEND_ROOT, PROJECT_ROOT, Settings, load_settings
from app.database import Database
from app.errors import AppError, RateLimitError
from app.ratelimit import RateLimiter
from app.realtime import ConnectionManager
from app.services.auth import AuthService
from app.services.banking import BankingService

logger = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}

API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
SPA_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def _field_errors(exc: RequestValidationError) -> dict[str, str]:
    fields: dict[str, str] = {}
    friendly = {
        "missing": "This field is required.",
        "greater_than": "Must be greater than zero.",
        "less_than": "Value is too large.",
        "string_too_short": "This value is too short.",
        "string_too_long": "This value is too long.",
        "bool_parsing": "Enter a valid value.",
        "decimal_parsing": "Enter a valid amount.",
        "decimal_max_places": "Use at most two decimal places.",
        "json_invalid": "The request body is not valid JSON.",
        "email": "Enter a valid email address.",
    }
    for error in exc.errors():
        location = [part for part in error.get("loc", ()) if isinstance(part, str)]
        name = next(
            (
                part
                for part in reversed(location)
                if part not in {"body", "query", "path", "header", "cookie"}
            ),
            "request",
        )
        message = friendly.get(str(error.get("type")))
        if message is None:
            message = str(error.get("msg") or "Enter a valid value.")
            message = message.removeprefix("Value error, ").strip()
        fields.setdefault(name, message)
    return fields


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        load_dotenv(BACKEND_ROOT / ".env", override=False)
        settings = load_settings()

    if settings.is_production and not settings.cookie_secure:
        logger.warning(
            "Production mode requires GAUR_BANK_COOKIE_SECURE=true for session cookies."
        )
    if "*" in settings.cors_origins:
        logger.warning(
            "Wildcard CORS origins are ignored because credentialed requests are enabled."
        )
        settings = replace(
            settings,
            cors_origins=tuple(o for o in settings.cors_origins if o != "*"),
        )

    db = Database(settings.database_path)
    services = {
        "auth_service": AuthService(db, settings),
        "banking_service": BankingService(db, settings),
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for directory in (
            settings.data_dir,
            settings.backups_dir,
            settings.exports_dir,
            settings.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        db.initialize()
        owner_update = services["auth_service"].bootstrap_owner_balance()
        if owner_update:
            logger.info(
                "Owner opening balance reconciled: account=%s %s -> %s paise",
                owner_update["account_id"],
                owner_update["balance_before_paise"],
                owner_update["balance_after_paise"],
            )
        app.state.realtime = ConnectionManager()
        logger.info(
            "GAUR BANK API ready (env=%s, schema=%s, demo_balance_paise=%s, owner=%s)",
            settings.app_env,
            "initialized",
            settings.demo_starting_balance_paise,
            settings.owner_username,
        )
        try:
            yield
        finally:
            await app.state.realtime.close_all()

    docs_enabled = not settings.is_production
    app = FastAPI(
        title="GAUR BANK API",
        version=settings.app_version,
        description=(
            "Educational multi-user banking simulator. Every balance and "
            "transaction is simulated; no real money or payment network is used."
        ),
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.db = db
    app.state.rate_limiter = RateLimiter()
    for name, service in services.items():
        setattr(app.state, name, service)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            *list(settings.cors_origins),
            "https://gaurbank.netlify.app",
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
        expose_headers=["X-Simulated"],
        max_age=600,
    )

    @app.middleware("http")
    async def hardening(request: Request, call_next):
        raw_length = request.headers.get("content-length")
        if raw_length:
            try:
                if int(raw_length) > 2_000_000:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "detail": "Request body is too large.",
                            "code": "payload_too_large",
                            "fields": {},
                        },
                    )
            except ValueError:
                pass
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        is_api = request.url.path == "/api" or request.url.path.startswith("/api/")
        response.headers.setdefault(
            "Content-Security-Policy", API_CSP if is_api else SPA_CSP
        )
        if is_api:
            response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Simulated", "true")
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        headers: dict[str, str] = {}
        if isinstance(exc, RateLimitError):
            headers["Retry-After"] = str(exc.retry_after)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": exc.message,
                "code": exc.code,
                "fields": exc.fields,
            },
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields = _field_errors(exc)
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Please check the highlighted fields.",
                "code": "validation_error",
                "fields": fields,
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        codes = {401: "authentication_required", 403: "forbidden", 404: "not_found"}
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": detail,
                "code": codes.get(exc.status_code, "http_error"),
                "fields": {},
            },
            headers=dict(exc.headers or {}),
        )

    @app.exception_handler(sqlite3.Error)
    async def database_error_handler(
        request: Request, exc: sqlite3.Error
    ) -> JSONResponse:
        logger.exception("Database error on %s", request.url.path)
        return JSONResponse(
            status_code=503,
            content={
                "detail": "The banking database is temporarily busy. Please retry.",
                "code": "database_busy",
                "fields": {},
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Something went wrong. No financial operation was silently applied.",
                "code": "internal_error",
                "fields": {},
            },
        )

    app.include_router(system_routes.router, prefix="/api")
    app.include_router(auth_routes.router, prefix="/api")
    app.include_router(banking_routes.router, prefix="/api")
    app.include_router(ws_routes.router, prefix="/api")

    frontend_dist = PROJECT_ROOT / "frontend" / "dist"
    if settings.serve_frontend and (frontend_dist / "index.html").is_file():
        assets = frontend_dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            if full_path == "api" or full_path.startswith("api/"):
                raise StarletteHTTPException(status_code=404, detail="Not Found")
            candidate = (frontend_dist / full_path).resolve()
            try:
                candidate.relative_to(frontend_dist.resolve())
            except ValueError:
                raise StarletteHTTPException(status_code=404, detail="Not Found")
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(frontend_dist / "index.html")
    else:
        if settings.serve_frontend:
            logger.warning(
                "GAUR_BANK_SERVE_FRONTEND is enabled but %s does not exist.",
                frontend_dist / "index.html",
            )

        @app.get("/", include_in_schema=False)
        def root() -> dict:
            return {
                "name": "GAUR BANK API",
                "simulated": True,
                "docs": "/docs" if docs_enabled else None,
                "health": "/api/health",
                "config": "/api/config",
            }

    return app


app = create_app()


__all__ = ["app", "create_app"]
