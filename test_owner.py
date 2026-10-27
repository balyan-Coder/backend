from __future__ import annotations

import dataclasses

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.auth import OWNER_BOOTSTRAP_KEY

OWNER_PASSWORD = "SecurePass123"


def _member(presence: dict, username: str) -> dict:
    return next(
        member
        for member in presence["members"]
        if member["username"].casefold() == username.casefold()
    )


def test_owner_registration_receives_owner_opening_balance(client, settings):
    response = client.post(
        "/api/auth/register",
        json={
            "username": "vansh",
            "email": "vansh@example.com",
            "display_name": "Vansh Balyan",
            "password": OWNER_PASSWORD,
            "confirm_password": OWNER_PASSWORD,
            "accept_demo_policy": True,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user"]["username"] == "vansh"
    assert body["user"]["is_owner"] is True
    assert body["account"]["balance_paise"] == settings.owner_starting_balance_paise
    assert body["account"]["balance_paise"] == 10_000_000_000_000

    dashboard = client.get("/api/dashboard").json()
    assert dashboard["user"]["is_owner"] is True
    assert dashboard["demo_policy"]["is_owner"] is True
    assert dashboard["demo_policy"]["starting_balance_paise"] == 10_000_000_000_000
    assert dashboard["account"]["balance_paise"] == 10_000_000_000_000


def test_ordinary_registration_starts_at_zero(client):
    response = client.post(
        "/api/auth/register",
        json={
            "username": "aarav",
            "email": "aarav@example.com",
            "display_name": "Aarav Sharma",
            "password": OWNER_PASSWORD,
            "confirm_password": OWNER_PASSWORD,
            "accept_demo_policy": True,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user"]["is_owner"] is False
    assert body["account"]["balance_paise"] == 0

    dashboard = client.get("/api/dashboard").json()
    assert dashboard["user"]["is_owner"] is False
    assert dashboard["demo_policy"]["is_owner"] is False
    assert dashboard["demo_policy"]["starting_balance_paise"] == 0
    assert dashboard["account"]["balance_paise"] == 0
    assert dashboard["recent_transactions"] == []
    assert client.get("/api/transactions").json()["total"] == 0


def test_member_presence_is_owner_only(client, signup, act_as):
    owner = signup("vansh", funded=False)
    member = signup("aarav", funded=False)

    act_as(member)
    forbidden = client.get("/api/members")
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "forbidden"

    act_as(owner)
    response = client.get("/api/members")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["simulated"] is True
    assert body["total"] == 2
    assert body["online_count"] == 0
    assert body["offline_count"] == 2
    assert _member(body, "vansh")["is_owner"] is True
    assert _member(body, "aarav")["is_owner"] is False
    assert _member(body, "aarav")["online"] is False
    assert _member(body, "aarav")["masked_account_number"].endswith(
        member.account_number[-4:]
    )


def test_online_status_tracks_live_websockets(client, signup, act_as):
    owner = signup("vansh", funded=False)
    member = signup("aarav", funded=False)

    act_as(member)
    with client.websocket_connect("/api/ws") as member_ws:
        assert member_ws.receive_json()["type"] == "ready"

        act_as(owner)
        with client.websocket_connect("/api/ws") as owner_ws:
            assert owner_ws.receive_json()["type"] == "ready"
            presence = client.get("/api/members").json()
            assert _member(presence, "aarav")["online"] is True
            assert _member(presence, "vansh")["online"] is True
            assert presence["online_count"] == 2

        presence = client.get("/api/members").json()
        assert _member(presence, "vansh")["online"] is False
        assert _member(presence, "aarav")["online"] is True
        assert presence["online_count"] == 1

    presence = client.get("/api/members").json()
    assert _member(presence, "aarav")["online"] is False
    assert presence["online_count"] == 0


def test_owner_has_no_per_operation_or_daily_caps(client, signup, act_as):
    owner = signup("vansh", funded=False)
    recipient = signup("aarav", funded=False)
    act_as(owner)

    dashboard = client.get("/api/dashboard").json()
    assert dashboard["limits"]["unlimited"] is True
    assert dashboard["limits"]["transaction_limit_paise"] is None
    assert dashboard["limits"]["daily_deposit_limit_paise"] is None

    deposit = client.post(
        "/api/deposits",
        json={"amount": "200000.00", "idempotency_key": "owner-large-deposit-1"},
        headers=owner.headers,
    )
    assert deposit.status_code == 200, deposit.text
    assert deposit.json()["receipt"]["amount_paise"] == 20_000_000

    transfer = client.post(
        "/api/transfers",
        json={
            "recipient_account_number": recipient.account_number,
            "amount": "100000.00",
            "idempotency_key": "owner-large-transfer-1",
        },
        headers=owner.headers,
    )
    assert transfer.status_code == 200, transfer.text
    assert transfer.json()["receipt"]["amount_paise"] == 10_000_000

    act_as(recipient)
    assert client.get("/api/dashboard").json()["account"]["balance_paise"] == 10_000_000


def test_ordinary_members_keep_their_limits(client, signup, act_as, fund):
    sender = signup("diya", funded=False)
    recipient = signup("aarav", funded=False)
    fund(sender, 20_000_000)
    act_as(sender)

    dashboard = client.get("/api/dashboard").json()
    assert dashboard["limits"]["unlimited"] is False
    assert dashboard["limits"]["transaction_limit_paise"] == 5_000_000

    transfer = client.post(
        "/api/transfers",
        json={
            "recipient_account_number": recipient.account_number,
            "amount": "100000.00",
            "idempotency_key": "member-large-transfer-1",
        },
        headers=sender.headers,
    )
    assert transfer.status_code == 400
    assert transfer.json()["code"] == "limit_exceeded"

    deposit = client.post(
        "/api/deposits",
        json={"amount": "200000.00", "idempotency_key": "member-large-deposit-1"},
        headers=sender.headers,
    )
    assert deposit.status_code == 400
    assert deposit.json()["code"] == "limit_exceeded"


def test_owner_can_send_to_an_external_account(client, signup, act_as):
    owner = signup("vansh", funded=False)
    act_as(owner)
    payload = {
        "recipient_account_number": "999888777666",
        "recipient_name": "Rohan Mehta",
        "external": True,
        "amount": "150000.00",
        "description": "External rent",
        "idempotency_key": "external-transfer-key-1",
    }

    response = client.post("/api/transfers", json=payload, headers=owner.headers)
    assert response.status_code == 200, response.text
    receipt = response.json()["receipt"]
    assert receipt["external"] is True
    assert receipt["amount_paise"] == 15_000_000
    assert receipt["counterparty_name"] == "Rohan Mehta"
    assert receipt["counterparty_masked_account"].endswith("7666")
    assert receipt["balance_paise"] == 10_000_000_000_000 - 15_000_000

    listing = client.get("/api/transactions").json()
    item = listing["items"][0]
    assert item["type"] == "transfer_debit"
    assert item["direction"] == "debit"
    assert item["amount_paise"] == 15_000_000
    assert item["description"] == "External rent"
    assert item["counterparty_name"] == "Rohan Mehta"
    assert item["counterparty_account_number"].endswith("7666")

    # Retrying with the same key must not send the money twice.
    retry = client.post("/api/transfers", json=payload, headers=owner.headers)
    assert retry.status_code == 200, retry.text
    assert retry.json()["receipt"]["duplicate"] is True
    assert (
        client.get("/api/dashboard").json()["account"]["balance_paise"]
        == 10_000_000_000_000 - 15_000_000
    )


def test_external_transfer_requires_a_name_and_rejects_members(
    client, signup, act_as
):
    owner = signup("vansh", funded=False)
    member = signup("aarav", funded=False)
    act_as(owner)

    missing_name = client.post(
        "/api/transfers",
        json={
            "recipient_account_number": "999888777666",
            "external": True,
            "amount": "10.00",
            "idempotency_key": "external-key-no-name",
        },
        headers=owner.headers,
    )
    assert missing_name.status_code == 422, missing_name.text
    assert missing_name.json()["fields"]["recipient_name"]

    member_number = client.post(
        "/api/transfers",
        json={
            "recipient_account_number": member.account_number,
            "recipient_name": "Aarav",
            "external": True,
            "amount": "10.00",
            "idempotency_key": "external-key-member",
        },
        headers=owner.headers,
    )
    assert member_number.status_code == 400
    assert member_number.json()["code"] == "recipient_is_member"


def test_ordinary_member_external_transfer_keeps_limits(
    client, signup, act_as, fund
):
    sender = signup("diya", funded=False)
    fund(sender, 20_000_000)
    act_as(sender)

    response = client.post(
        "/api/transfers",
        json={
            "recipient_account_number": "999888777666",
            "recipient_name": "Outside Party",
            "external": True,
            "amount": "100000.00",
            "idempotency_key": "member-external-large-1",
        },
        headers=sender.headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "limit_exceeded"


def test_existing_owner_balance_is_reconciled_once(settings):
    legacy_settings = dataclasses.replace(
        settings, owner_starting_balance_paise=1_000_000
    )
    first_app = create_app(legacy_settings)
    with TestClient(first_app) as client:
        registration = client.post(
            "/api/auth/register",
            json={
                "username": "vansh",
                "email": "vansh@example.com",
                "display_name": "Vansh Balyan",
                "password": OWNER_PASSWORD,
                "confirm_password": OWNER_PASSWORD,
                "accept_demo_policy": True,
            },
        )
        assert registration.status_code == 201, registration.text
        assert registration.json()["account"]["balance_paise"] == 1_000_000

    # Simulate a database created before the owner policy existed.
    with first_app.state.db.transaction() as conn:
        conn.execute(
            "DELETE FROM app_settings WHERE key = ?", (OWNER_BOOTSTRAP_KEY,)
        )

    upgraded_settings = dataclasses.replace(
        settings, owner_starting_balance_paise=10_000_000_000_000
    )
    second_app = create_app(upgraded_settings)
    with TestClient(second_app) as client:
        login = client.post(
            "/api/auth/login",
            json={"identifier": "vansh", "password": OWNER_PASSWORD},
        )
        assert login.status_code == 200, login.text
        assert login.json()["user"]["is_owner"] is True
        dashboard = client.get("/api/dashboard").json()
        assert dashboard["account"]["balance_paise"] == 10_000_000_000_000

    with second_app.state.db.connection() as conn:
        adjustments = conn.execute(
            """
            SELECT amount_paise, balance_before_paise, balance_after_paise,
                   counts_toward_limit, description
            FROM transactions
            WHERE description LIKE 'Owner opening balance adjustment%'
            """
        ).fetchall()
    assert len(adjustments) == 1
    adjustment = adjustments[0]
    assert adjustment["amount_paise"] == 9_999_999_000_000
    assert adjustment["balance_before_paise"] == 1_000_000
    assert adjustment["balance_after_paise"] == 10_000_000_000_000
    assert adjustment["counts_toward_limit"] == 0

    # A second restart must not top the owner up again.
    third_app = create_app(upgraded_settings)
    with TestClient(third_app) as client:
        login = client.post(
            "/api/auth/login",
            json={"identifier": "vansh", "password": OWNER_PASSWORD},
        )
        assert login.status_code == 200
        assert client.get("/api/dashboard").json()["account"]["balance_paise"] == (
            10_000_000_000_000
        )
    with third_app.state.db.connection() as conn:
        count = conn.execute(
            """
            SELECT COUNT(*) FROM transactions
            WHERE description LIKE 'Owner opening balance adjustment%'
            """
        ).fetchone()[0]
    assert count == 1
