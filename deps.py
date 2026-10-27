"""FastAPI dependencies: services, authenticated sessions and CSRF checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Header, Request

from app.config import Settings
from app.database import Database
from app.errors import AuthenticationError, PermissionError, RateLimitError
from app.ratelimit import RateLimiter
from app.security import hash_token, secure_compare
from app.services.auth import AuthService
from app.services.banking import BankingService
from app.utils.time import iso, parse_iso, utcnow


@dataclass(frozen=True)
class SessionContext:
    """Everything an authenticated request needs, resolved from the database."""

    user_id: int
    session_id: int
    token_hash: str
    csrf_token: str
    username: str
    email: str
    display_name: str
    status: str
    expires_at: str
    last_seen_at: str


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_limiter(request: Request) -> RateLimiter:
    return request.app.state.rate_limiter


def get_auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service


def get_banking_service(request: Request) -> BankingService:
    return request.app.state.banking_service


def client_ip(request: Request) -> str:
    """Return the direct peer address.

    ``X-Forwarded-For`` is deliberately ignored: without an explicit trusted
    proxy configuration a client could spoof it and bypass rate limits.
    """

    return request.client.host if request.client else "unknown"


def session_from_request(request: Request) -> SessionContext:
    settings: Settings = request.app.state.settings
    auth: AuthService = request.app.state.auth_service
    db: Database = request.app.state.db
    raw_token = request.cookies.get(settings.cookie_name)
    if not raw_token:
        raise AuthenticationError("Please sign in to continue.")
    token_hash = hash_token(raw_token)
    now = iso()
    with db.connection() as conn:
        row = auth.sessions.find_active(conn, token_hash, now)
    if row is None:
        raise AuthenticationError("Your session has expired. Please sign in again.")
    if str(row["status"]) != "active":
        raise PermissionError("This account is suspended.")
    context = SessionContext(
        user_id=int(row["user_id"]),
        session_id=int(row["session_id"]),
        token_hash=token_hash,
        csrf_token=str(row["csrf_token"]),
        username=str(row["username"]),
        email=str(row["email"]),
        display_name=str(row["display_name"]),
        status=str(row["status"]),
        expires_at=str(row["expires_at"]),
        last_seen_at=str(row["last_seen_at"]),
    )
    _touch_session(db, auth, context, now)
    return context


def _touch_session(db: Database, auth: AuthService, context: SessionContext, now: str) -> None:
    """Refresh last_seen at most once a minute to avoid a write per request."""

    try:
        last_seen = parse_iso(context.last_seen_at)
        if (utcnow() - last_seen).total_seconds() < 60:
            return
    except ValueError:
        pass
    try:
        with db.transaction() as conn:
            auth.sessions.touch(conn, context.session_id, now)
    except Exception:  # noqa: BLE001 - presence tracking is best effort
        return


def get_session(request: Request) -> SessionContext:
    return session_from_request(request)


def require_csrf(
    session: SessionContext = Depends(get_session),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> SessionContext:
    if not x_csrf_token or not secure_compare(x_csrf_token, session.csrf_token):
        raise PermissionError(
            "The security token is missing or invalid. Refresh the page and try again.",
            code="csrf_failed",
        )
    return session


def enforce_rate_limit(
    request: Request,
    *,
    bucket: str,
    identity: str | None = None,
    limit: int | None = None,
    window_seconds: int | None = None,
) -> None:
    settings: Settings = request.app.state.settings
    limiter: RateLimiter = request.app.state.rate_limiter
    limiter.check(
        f"{bucket}:{identity or client_ip(request)}",
        limit=limit or settings.banking_rate_limit,
        window_seconds=window_seconds or settings.banking_rate_window_seconds,
    )


def auth_rate_limit(request: Request) -> None:
    settings: Settings = request.app.state.settings
    enforce_rate_limit(
        request,
        bucket="auth",
        limit=settings.auth_rate_limit,
        window_seconds=settings.auth_rate_window_seconds,
    )


def banking_rate_limit(
    request: Request,
    session: SessionContext = Depends(get_session),
) -> SessionContext:
    settings: Settings = request.app.state.settings
    enforce_rate_limit(
        request,
        bucket="banking",
        identity=str(session.user_id),
        limit=settings.banking_rate_limit,
        window_seconds=settings.banking_rate_window_seconds,
    )
    return session


def banking_write_rate_limit(
    request: Request,
    session: SessionContext = Depends(get_session),
) -> SessionContext:
    settings: Settings = request.app.state.settings
    enforce_rate_limit(
        request,
        bucket="banking-write",
        identity=str(session.user_id),
        limit=max(5, settings.banking_rate_limit // 2),
        window_seconds=settings.banking_rate_window_seconds,
    )
    return session


def websocket_session(websocket: Any, *, db: Database, auth: AuthService, settings: Settings) -> SessionContext | None:
    """Authenticate a WebSocket handshake from the session cookie."""

    raw_token = websocket.cookies.get(settings.cookie_name)
    if not raw_token:
        return None
    now = iso()
    with db.connection() as conn:
        row = auth.sessions.find_active(conn, hash_token(raw_token), now)
    if row is None or str(row["status"]) != "active":
        return None
    return SessionContext(
        user_id=int(row["user_id"]),
        session_id=int(row["session_id"]),
        token_hash=hash_token(raw_token),
        csrf_token=str(row["csrf_token"]),
        username=str(row["username"]),
        email=str(row["email"]),
        display_name=str(row["display_name"]),
        status=str(row["status"]),
        expires_at=str(row["expires_at"]),
        last_seen_at=str(row["last_seen_at"]),
    )


__all__ = [
    "SessionContext",
    "auth_rate_limit",
    "banking_rate_limit",
    "banking_write_rate_limit",
    "client_ip",
    "enforce_rate_limit",
    "get_auth_service",
    "get_banking_service",
    "get_db",
    "get_limiter",
    "get_session",
    "get_settings",
    "require_csrf",
    "session_from_request",
    "websocket_session",
]
