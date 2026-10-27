"""Password hashing, opaque tokens and cookie helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Response

from app.config import Settings


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_session_token() -> str:
    return secrets.token_urlsafe(48)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


@lru_cache(maxsize=8)
def _hasher(time_cost: int, memory_cost: int, parallelism: int) -> PasswordHasher:
    return PasswordHasher(
        time_cost=time_cost,
        memory_cost=memory_cost,
        parallelism=parallelism,
        hash_len=32,
        salt_len=16,
    )


def password_hasher(settings: Settings) -> PasswordHasher:
    return _hasher(
        settings.argon2_time_cost,
        settings.argon2_memory_cost,
        settings.argon2_parallelism,
    )


def hash_password(password: str, settings: Settings) -> str:
    return password_hasher(settings).hash(password)


def verify_password(password: str, password_hash: str, settings: Settings) -> bool:
    try:
        return password_hasher(settings).verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def dummy_verify(password: str, settings: Settings) -> None:
    """Spend comparable time for unknown accounts to reduce user enumeration."""
    try:
        password_hasher(settings).verify(_dummy_hash(settings), password)
    except Exception:  # noqa: BLE001
        return


@lru_cache(maxsize=8)
def _dummy_hash(settings: Settings) -> str:
    return password_hasher(settings).hash("gaur-bank-timing-equaliser-not-a-credential")


def secure_compare(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def set_session_cookie(response: Response, raw_token: str, settings: Settings, max_age: int) -> None:
    response.set_cookie(
        key=settings.cookie_name,
        value=raw_token,
        max_age=max_age,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.cookie_name,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
    )
