"""Configuration for the GAUR BANK multi-user simulator.

Every value can be overridden through environment variables so that secrets and
deployment-specific settings never live in source control.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if not raw:
        return default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    return path


def _env_origins() -> tuple[str, ...]:
    raw = os.environ.get(
        "GAUR_BANK_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    )
    origins = tuple(origin.strip() for origin in raw.split(",") if origin.strip())
    return origins or ("http://localhost:5173",)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    backups_dir: Path
    exports_dir: Path
    logs_dir: Path
    cors_origins: tuple[str, ...] = field(default_factory=_env_origins)
    session_minutes: int = 480
    cookie_secure: bool = False
    cookie_name: str = "gaur_session"
    demo_starting_balance_paise: int = 0  # ordinary accounts start empty
    owner_username: str = "vansh"
    owner_starting_balance_paise: int = 10_000_000_000_000  # ₹100,000,000,000.00
    transaction_limit_paise: int = 5_000_000  # ₹50,000.00 per operation
    daily_deposit_limit_paise: int = 10_000_000  # ₹1,00,000.00 per day
    allow_demo_deposits: bool = True
    login_max_attempts: int = 5
    login_lock_seconds: int = 120
    auth_rate_limit: int = 20
    auth_rate_window_seconds: int = 300
    banking_rate_limit: int = 30
    banking_rate_window_seconds: int = 60
    argon2_time_cost: int = 3
    argon2_memory_cost: int = 65536
    argon2_parallelism: int = 4
    serve_frontend: bool = False
    app_env: str = "development"
    app_version: str = "2.0.0"

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    def is_owner_username(self, username: str | None) -> bool:
        """Owner status is derived from the configured username, case-insensitively."""

        owner = (self.owner_username or "").strip().casefold()
        candidate = (username or "").strip().casefold()
        return bool(owner) and candidate == owner


def load_settings() -> Settings:
    data_dir = _env_path("GAUR_BANK_DATA_DIR", BACKEND_ROOT / "data")
    database_path = _env_path("GAUR_BANK_DB_PATH", data_dir / "gaur_bank.db")
    return Settings(
        data_dir=data_dir,
        database_path=database_path,
        backups_dir=data_dir / "backups",
        exports_dir=data_dir / "exports",
        logs_dir=data_dir / "logs",
        cors_origins=_env_origins(),
        session_minutes=max(5, _env_int("GAUR_BANK_SESSION_MINUTES", 480)),
        cookie_secure=_env_bool("GAUR_BANK_COOKIE_SECURE", False),
        cookie_name=os.environ.get("GAUR_BANK_COOKIE_NAME", "gaur_session"),
        demo_starting_balance_paise=max(
            0, _env_int("GAUR_BANK_DEMO_STARTING_BALANCE_PAISE", 0)
        ),
        owner_username=(
            os.environ.get("GAUR_BANK_OWNER_USERNAME", "vansh").strip() or "vansh"
        ),
        owner_starting_balance_paise=max(
            0,
            _env_int(
                "GAUR_BANK_OWNER_STARTING_BALANCE_PAISE", 10_000_000_000_000
            ),
        ),
        transaction_limit_paise=max(
            100, _env_int("GAUR_BANK_TRANSACTION_LIMIT_PAISE", 5_000_000)
        ),
        daily_deposit_limit_paise=max(
            100, _env_int("GAUR_BANK_DAILY_DEPOSIT_LIMIT_PAISE", 10_000_000)
        ),
        allow_demo_deposits=_env_bool("GAUR_BANK_ALLOW_DEMO_DEPOSITS", True),
        login_max_attempts=max(1, _env_int("GAUR_BANK_LOGIN_MAX_ATTEMPTS", 5)),
        login_lock_seconds=max(1, _env_int("GAUR_BANK_LOGIN_LOCK_SECONDS", 120)),
        auth_rate_limit=max(1, _env_int("GAUR_BANK_AUTH_RATE_LIMIT", 20)),
        auth_rate_window_seconds=max(
            1, _env_int("GAUR_BANK_AUTH_RATE_WINDOW_SECONDS", 300)
        ),
        banking_rate_limit=max(1, _env_int("GAUR_BANK_BANKING_RATE_LIMIT", 30)),
        banking_rate_window_seconds=max(
            1, _env_int("GAUR_BANK_BANKING_RATE_WINDOW_SECONDS", 60)
        ),
        argon2_time_cost=max(1, _env_int("GAUR_BANK_ARGON2_TIME_COST", 3)),
        argon2_memory_cost=max(8192, _env_int("GAUR_BANK_ARGON2_MEMORY_COST", 65536)),
        argon2_parallelism=max(1, _env_int("GAUR_BANK_ARGON2_PARALLELISM", 4)),
        serve_frontend=_env_bool("GAUR_BANK_SERVE_FRONTEND", False),
        app_env=os.environ.get("GAUR_BANK_ENV", "development"),
        app_version=os.environ.get("GAUR_BANK_VERSION", "2.0.0"),
    )
