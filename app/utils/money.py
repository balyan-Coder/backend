"""Money and reference helpers.  Money is always integer paise."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from app.errors import ValidationError


def decimal_to_paise(value: Decimal | str | int) -> int:
    try:
        amount = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ValidationError("Enter a valid amount.", field="amount") from exc
    if not amount.is_finite():
        raise ValidationError("Enter a valid amount.", field="amount")
    if amount <= 0:
        raise ValidationError("Amount must be greater than zero.", field="amount")
    if amount.as_tuple().exponent < -2:
        raise ValidationError("Use at most two decimal places.", field="amount")
    paise = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if paise <= 0:
        raise ValidationError("Amount must be greater than zero.", field="amount")
    return paise


def paise_to_string(paise: int) -> str:
    return f"{paise // 100}.{paise % 100:02d}"


def mask_account(number: str) -> str:
    digits = str(number)
    if len(digits) <= 4:
        return "•" * len(digits)
    return "•" * (len(digits) - 4) + digits[-4:]


def new_reference(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{prefix}-{stamp}-{secrets.token_hex(3).upper()}"


def new_idempotency_key() -> str:
    return secrets.token_urlsafe(24)
