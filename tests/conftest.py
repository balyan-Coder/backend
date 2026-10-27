from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, load_settings
from app.main import create_app
from app.utils.money import new_reference
from app.utils.time import iso


@dataclass
class Actor:
    username: str
    email: str
    password: str
    user: dict[str, Any]
    account: dict[str, Any]
    token: str
    csrf: str

    @property
    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf}

    @property
    def account_number(self) -> str:
        return str(self.account["account_number"])


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return dataclasses.replace(
        load_settings(),
        data_dir=tmp_path,
        database_path=tmp_path / "gaur_bank_test.db",
        backups_dir=tmp_path / "backups",
        exports_dir=tmp_path / "exports",
        logs_dir=tmp_path / "logs",
        argon2_time_cost=1,
        argon2_memory_cost=8192,
        argon2_parallelism=1,
        auth_rate_limit=1000,
        auth_rate_window_seconds=300,
        banking_rate_limit=1000,
        banking_rate_window_seconds=60,
        login_max_attempts=5,
        login_lock_seconds=120,
        demo_starting_balance_paise=0,
        owner_username="vansh",
        owner_starting_balance_paise=10_000_000_000_000,
        transaction_limit_paise=5_000_000,
        daily_deposit_limit_paise=10_000_000,
        allow_demo_deposits=True,
        app_env="test",
    )


@pytest.fixture
def make_app(settings: Settings) -> Callable[..., Any]:
    def factory(**overrides: Any):
        return create_app(dataclasses.replace(settings, **overrides))

    return factory


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def fund(app):
    """Give a test actor an opening simulated credit without touching the API.

    Production registration now starts ordinary accounts at ₹0.  Most existing
    banking tests need spendable funds, so this helper adds an append-only
    opening-credit row (excluded from daily limits) exactly like the old policy.
    """

    def factory(actor: Actor, paise: int = 1_000_000) -> Actor:
        if paise <= 0:
            return actor
        now = iso()
        account_id = int(actor.account["id"])
        with app.state.db.transaction() as conn:
            row = conn.execute(
                "SELECT balance_paise FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            assert row is not None
            before = int(row["balance_paise"])
            conn.execute(
                "UPDATE accounts SET balance_paise = ?, updated_at = ? WHERE id = ?",
                (before + paise, now, account_id),
            )
            app.state.banking_service.transactions.insert(
                conn,
                reference=new_reference("TXN"),
                account_id=account_id,
                counterparty_account_id=None,
                type_="deposit",
                direction="credit",
                amount_paise=paise,
                balance_before_paise=before,
                balance_after_paise=before + paise,
                status="success",
                description="Test opening demo credit",
                transfer_id=None,
                idempotency_key=None,
                created_at=now,
                counts_toward_limit=0,
            )
        actor.account = {**actor.account, "balance_paise": before + paise}
        return actor

    return factory


@pytest.fixture
def signup(client: TestClient, settings: Settings, fund) -> Callable[..., Actor]:
    def factory(
        username: str = "aarav",
        *,
        email: str | None = None,
        display_name: str | None = None,
        password: str = "SecurePass123",
        accept_demo_policy: bool = True,
        funded: bool = True,
    ) -> Actor:
        payload = {
            "username": username,
            "email": email or f"{username.lower()}@example.com",
            "display_name": display_name or username.title(),
            "password": password,
            "confirm_password": password,
            "accept_demo_policy": accept_demo_policy,
        }
        response = client.post("/api/auth/register", json=payload)
        assert response.status_code == 201, response.text
        body = response.json()
        token = client.cookies.get(settings.cookie_name)
        assert token
        actor = Actor(
            username=username,
            email=payload["email"],
            password=password,
            user=body["user"],
            account=body["account"],
            token=token,
            csrf=body["csrf_token"],
        )
        if funded:
            fund(actor)
        return actor

    return factory


@pytest.fixture
def act_as(client: TestClient, settings: Settings) -> Callable[[Actor], None]:
    def factory(actor: Actor) -> None:
        client.cookies.clear()
        client.cookies.set(settings.cookie_name, actor.token)

    return factory


@pytest.fixture
def login(client: TestClient, settings: Settings) -> Callable[..., Actor]:
    def factory(
        identifier: str,
        password: str = "SecurePass123",
    ) -> Actor:
        response = client.post(
            "/api/auth/login",
            json={"identifier": identifier, "password": password},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        token = client.cookies.get(settings.cookie_name)
        assert token
        return Actor(
            username=body["user"]["username"],
            email=body["user"]["email"],
            password=password,
            user=body["user"],
            account=body["account"],
            token=token,
            csrf=body["csrf_token"],
        )

    return factory
