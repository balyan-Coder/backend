"""Registration, login, logout, profile and password endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status

from app.config import Settings
from app.deps import (
    SessionContext,
    auth_rate_limit,
    banking_rate_limit,
    banking_write_rate_limit,
    get_auth_service,
    get_session,
    get_settings,
    require_csrf,
)
from app.schemas import (
    LoginIn,
    PasswordChangeIn,
    ProfileUpdateIn,
    RegisterIn,
)
from app.security import clear_session_cookie, set_session_cookie
from app.services.auth import AuthService

router = APIRouter(tags=["authentication"])


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    ip = request.client.host if request.client else None
    return ip, request.headers.get("user-agent")


@router.post("/auth/register", status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterIn,
    request: Request,
    response: Response,
    auth: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
    _: None = Depends(auth_rate_limit),
) -> dict:
    ip_address, user_agent = _client_meta(request)
    result = auth.register(
        username=payload.username,
        email=str(payload.email),
        display_name=payload.display_name,
        password=payload.password,
        confirm_password=payload.confirm_password,
        accept_demo_policy=payload.accept_demo_policy,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    set_session_cookie(response, result.raw_token, settings, result.max_age)
    return {
        "user": result.user,
        "account": result.account,
        "csrf_token": result.csrf_token,
        "expires_at": result.expires_at,
        "message": "Your simulated GAUR BANK account is ready.",
        "simulated": True,
    }


@router.post("/auth/login")
def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    auth: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
    _: None = Depends(auth_rate_limit),
) -> dict:
    ip_address, user_agent = _client_meta(request)
    result = auth.login(
        identifier=payload.identifier,
        password=payload.password,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    set_session_cookie(response, result.raw_token, settings, result.max_age)
    return {
        "user": result.user,
        "account": result.account,
        "csrf_token": result.csrf_token,
        "expires_at": result.expires_at,
        "message": "Signed in to the GAUR BANK simulator.",
        "simulated": True,
    }


@router.post("/auth/logout")
def logout(
    response: Response,
    session: SessionContext = Depends(require_csrf),
    auth: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> dict:
    auth.logout(
        token_hash=session.token_hash,
        user={"id": session.user_id, "username": session.username},
    )
    clear_session_cookie(response, settings)
    return {"message": "Signed out.", "simulated": True}


@router.get("/auth/me")
def me(
    session: SessionContext = Depends(get_session),
    auth: AuthService = Depends(get_auth_service),
) -> dict:
    profile = auth.profile(session.user_id)
    return {
        "user": profile["user"],
        "account": profile["account"],
        "csrf_token": session.csrf_token,
        "expires_at": session.expires_at,
        "simulated": True,
    }


@router.get("/profile")
def get_profile(
    session: SessionContext = Depends(get_session),
    auth: AuthService = Depends(get_auth_service),
) -> dict:
    return auth.profile(session.user_id)


@router.get("/members")
def member_presence(
    request: Request,
    session: SessionContext = Depends(banking_rate_limit),
    auth: AuthService = Depends(get_auth_service),
) -> dict:
    """Owner-only directory showing which members currently have a live socket."""

    online_user_ids = request.app.state.realtime.online_user_ids()
    return auth.members(
        requester_user_id=session.user_id, online_user_ids=online_user_ids
    )


@router.patch("/profile")
def update_profile(
    payload: ProfileUpdateIn,
    session: SessionContext = Depends(require_csrf),
    _: SessionContext = Depends(banking_write_rate_limit),
    auth: AuthService = Depends(get_auth_service),
) -> dict:
    return auth.update_profile(
        user_id=session.user_id,
        display_name=payload.display_name,
        email=str(payload.email),
    )


@router.post("/profile/password")
def change_password(
    payload: PasswordChangeIn,
    session: SessionContext = Depends(require_csrf),
    _: SessionContext = Depends(banking_write_rate_limit),
    auth: AuthService = Depends(get_auth_service),
) -> dict:
    auth.change_password(
        user_id=session.user_id,
        session_id=session.session_id,
        current_password=payload.current_password,
        new_password=payload.new_password,
        confirm_new_password=payload.confirm_new_password,
    )
    return {
        "message": "Password changed. Other sessions were signed out.",
        "simulated": True,
    }
