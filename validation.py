"""Reusable input validation independent of Pydantic transport schemas."""

from __future__ import annotations

import re

from app.errors import ValidationError

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,20}$")
_ACCOUNT_RE = re.compile(r"^\d{12}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
COMMON_PASSWORDS = {
    "password",
    "password1",
    "password123",
    "12345678",
    "123456789",
    "qwerty123",
    "letmein",
    "admin123",
    "gaur1234",
}


def clean_text(value: str | None, *, max_length: int = 200) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    return text[:max_length]


def validate_username(value: str | None) -> str:
    username = str(value or "").strip()
    if not _USERNAME_RE.fullmatch(username):
        raise ValidationError(
            "Username must be 3-20 characters using letters, numbers or underscores.",
            field="username",
        )
    return username


def normalize_email(value: str | None) -> str:
    email = str(value or "").strip().lower()
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        raise ValidationError("Enter a valid email address.", field="email")
    return email


def validate_display_name(value: str | None) -> str:
    name = clean_text(value, max_length=60)
    if len(name) < 2:
        raise ValidationError("Display name must be at least 2 characters.", field="display_name")
    return name


def validate_password(value: str | None) -> str:
    password = str(value or "")
    if len(password) < 10:
        raise ValidationError("Password must be at least 10 characters.", field="password")
    if len(password.encode("utf-8")) > 128:
        raise ValidationError("Password is too long.", field="password")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise ValidationError(
            "Password must include at least one letter and one number.",
            field="password",
        )
    if password.lower() in COMMON_PASSWORDS:
        raise ValidationError("Choose a less common password.", field="password")
    return password


def validate_account_number(value: str | None) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not _ACCOUNT_RE.fullmatch(digits):
        raise ValidationError("Enter a valid 12-digit GAUR BANK account number.", field="account_number")
    return digits


def validate_description(value: str | None, *, max_length: int = 200) -> str | None:
    text = clean_text(value, max_length=max_length)
    return text or None
