"""Public configuration and health endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.config import Settings
from app.database import Database
from app.deps import get_db, get_settings
from app.utils.time import iso

router = APIRouter(tags=["system"])

DISCLAIMER = (
    "GAUR BANK is an educational banking simulator. All balances, deposits, "
    "withdrawals and transfers are simulated. No real money, payment network, "
    "UPI, card processor or external bank is connected."
)


@router.get("/health")
def health(request: Request, db: Database = Depends(get_db)) -> dict:
    with db.connection() as conn:
        conn.execute("SELECT 1").fetchone()
    return {
        "status": "ok",
        "service": "gaur-bank-api",
        "version": request.app.state.settings.app_version,
        "time": iso(),
    }


@router.get("/config")
def public_config(settings: Settings = Depends(get_settings)) -> dict:
    return {
        "bank_name": "GAUR BANK",
        "currency": "INR",
        "demo_starting_balance_paise": settings.demo_starting_balance_paise,
        "transaction_limit_paise": settings.transaction_limit_paise,
        "daily_deposit_limit_paise": settings.daily_deposit_limit_paise,
        "allow_demo_deposits": settings.allow_demo_deposits,
        "session_minutes": settings.session_minutes,
        "version": settings.app_version,
        "disclaimer": DISCLAIMER,
        "environment": settings.app_env,
    }
