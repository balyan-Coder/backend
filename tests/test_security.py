from __future__ import annotations

import hashlib
import sqlite3

import pytest


def test_password_and_session_tokens_are_stored_hashed(app, signup):
    actor = signup("aarav", password="SecretPass123")
    with app.state.db.connection() as conn:
        user = conn.execute(
            "SELECT password_hash FROM users WHERE username = 'aarav'"
        ).fetchone()
        session = conn.execute(
            "SELECT token_hash, csrf_token FROM sessions WHERE user_id = ?",
            (actor.user["id"],),
        ).fetchone()
    assert user["password_hash"].startswith("$argon2id$")
    assert "SecretPass123" not in user["password_hash"]
    assert session["token_hash"] == hashlib.sha256(
        actor.token.encode("utf-8")
    ).hexdigest()
    assert actor.token not in session["token_hash"]
    assert session["csrf_token"] == actor.csrf


def test_ledger_and_audit_log_are_append_only(app, signup):
    signup("aarav")
    with pytest.raises(sqlite3.IntegrityError) as update_error:
        with app.state.db.transaction() as conn:
            conn.execute("UPDATE transactions SET amount_paise = 1")
    assert "append-only" in str(update_error.value)

    with pytest.raises(sqlite3.IntegrityError) as delete_error:
        with app.state.db.transaction() as conn:
            conn.execute("DELETE FROM transactions")
    assert "append-only" in str(delete_error.value)

    with pytest.raises(sqlite3.IntegrityError):
        with app.state.db.transaction() as conn:
            conn.execute("UPDATE audit_logs SET outcome = 'failure'")

    with pytest.raises(sqlite3.IntegrityError):
        with app.state.db.transaction() as conn:
            conn.execute("DELETE FROM audit_logs")


def test_balances_cannot_be_negative_at_the_database_layer(app, signup):
    actor = signup("aarav")
    with pytest.raises(sqlite3.IntegrityError):
        with app.state.db.transaction() as conn:
            conn.execute(
                "UPDATE accounts SET balance_paise = -1 WHERE id = ?",
                (actor.account["id"],),
            )


def test_client_supplied_identity_fields_are_ignored(client, signup, act_as):
    first = signup("aarav")
    second = signup("diya")
    third = signup("kabir")
    act_as(first)
    response = client.post(
        "/api/transfers",
        json={
            "recipient_account_number": third.account_number,
            "amount": "10.00",
            "idempotency_key": "spoof-key-0001",
            "from_user_id": second.user["id"],
            "from_account_id": second.account["id"],
            "balance_paise": 999_999_999,
        },
        headers=first.headers,
    )
    assert response.status_code == 200, response.text

    act_as(second)
    second_dashboard = client.get("/api/dashboard").json()
    assert second_dashboard["account"]["balance_paise"] == 1_000_000
    act_as(first)
    assert client.get("/api/dashboard").json()["account"]["balance_paise"] == 999_000
    act_as(third)
    assert client.get("/api/dashboard").json()["account"]["balance_paise"] == 1_001_000


def test_cors_preflight_only_allows_configured_origin(client):
    allowed = client.options(
        "/api/auth/login",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-csrf-token",
        },
    )
    assert allowed.status_code in {200, 204}
    assert (
        allowed.headers.get("access-control-allow-origin")
        == "http://localhost:5173"
    )
    assert allowed.headers.get("access-control-allow-credentials") == "true"

    denied = client.options(
        "/api/auth/login",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert denied.headers.get("access-control-allow-origin") is None


def test_simulated_label_present_on_receipts(client, signup):
    actor = signup("aarav")
    response = client.post(
        "/api/deposits",
        json={"amount": "1.00", "idempotency_key": "label-key-0001"},
        headers=actor.headers,
    )
    assert response.status_code == 200
    assert response.json()["simulated"] is True
    assert response.json()["receipt"]["amount_paise"] == 100
