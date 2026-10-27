from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient


def test_registration_creates_demo_account_and_session(client, signup):
    response = client.post(
        "/api/auth/register",
        json={
            "username": "aarav",
            "email": "aarav@example.com",
            "display_name": "Aarav Sharma",
            "password": "SecurePass123",
            "confirm_password": "SecurePass123",
            "accept_demo_policy": True,
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["simulated"] is True
    assert body["user"]["username"] == "aarav"
    assert body["user"]["email"] == "aarav@example.com"
    assert body["account"]["balance_paise"] == 0
    assert body["account"]["status"] == "active"
    assert body["user"]["is_owner"] is False
    assert len(body["account"]["account_number"]) == 12
    assert body["account"]["masked_account_number"].endswith(
        body["account"]["account_number"][-4:]
    )
    assert body["account"]["masked_account_number"].count("•") == 8
    assert body["csrf_token"]
    cookie = response.headers.get("set-cookie", "").lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "aarav"
    assert me.json()["account"]["balance_paise"] == 0


def test_duplicate_username_and_email_are_rejected_case_insensitively(client, signup):
    signup("aarav", email="aarav@example.com")
    duplicate_username = client.post(
        "/api/auth/register",
        json={
            "username": "AARAV",
            "email": "other@example.com",
            "display_name": "Other Person",
            "password": "SecurePass123",
            "confirm_password": "SecurePass123",
            "accept_demo_policy": True,
        },
    )
    assert duplicate_username.status_code == 409
    assert duplicate_username.json()["fields"]["username"]

    duplicate_email = client.post(
        "/api/auth/register",
        json={
            "username": "diya",
            "email": "AARAV@example.com",
            "display_name": "Diya",
            "password": "SecurePass123",
            "confirm_password": "SecurePass123",
            "accept_demo_policy": True,
        },
    )
    assert duplicate_email.status_code == 409
    assert duplicate_email.json()["fields"]["email"]


def test_registration_validation(client, signup):
    base = {
        "username": "aarav",
        "email": "aarav@example.com",
        "display_name": "Aarav Sharma",
        "password": "SecurePass123",
        "confirm_password": "SecurePass123",
        "accept_demo_policy": True,
    }

    mismatch = client.post(
        "/api/auth/register", json={**base, "confirm_password": "Different123"}
    )
    assert mismatch.status_code == 422
    assert "confirm_password" in mismatch.json()["fields"]

    weak = client.post(
        "/api/auth/register",
        json={**base, "password": "short", "confirm_password": "short"},
    )
    assert weak.status_code == 422
    assert "password" in weak.json()["fields"]

    no_number = client.post(
        "/api/auth/register",
        json={
            **base,
            "password": "OnlyLettersHere",
            "confirm_password": "OnlyLettersHere",
        },
    )
    assert no_number.status_code == 422
    assert "password" in no_number.json()["fields"]

    no_policy = client.post(
        "/api/auth/register", json={**base, "accept_demo_policy": False}
    )
    assert no_policy.status_code == 422
    assert "accept_demo_policy" in no_policy.json()["fields"]

    bad_email = client.post(
        "/api/auth/register", json={**base, "email": "not-an-email"}
    )
    assert bad_email.status_code == 422
    assert "email" in bad_email.json()["fields"]

    bad_username = client.post(
        "/api/auth/register", json={**base, "username": "no spaces"}
    )
    assert bad_username.status_code == 422
    assert "username" in bad_username.json()["fields"]


def test_login_logout_and_session_revocation(client, signup, login):
    actor = signup("aarav")
    assert client.post(
        "/api/auth/logout", headers=actor.headers
    ).status_code == 200
    assert client.get("/api/auth/me").status_code == 401

    by_username = login("aarav")
    assert by_username.user["id"] == actor.user["id"]
    assert by_username.csrf

    client.cookies.clear()
    by_email = login("AARAV@example.com")
    assert by_email.user["id"] == actor.user["id"]

    saved_token = by_email.token
    client.cookies.clear()
    client.cookies.set("gaur_session", saved_token)
    assert client.get("/api/auth/me").status_code == 200
    client.cookies.clear()
    client.cookies.set("gaur_session", actor.token)
    assert client.get("/api/auth/me").status_code == 401


def test_wrong_password_and_rate_limiting(make_app):
    app = make_app(auth_rate_limit=1000, login_max_attempts=10)
    with TestClient(app) as client:
        client.post(
            "/api/auth/register",
            json={
                "username": "aarav",
                "email": "aarav@example.com",
                "display_name": "Aarav",
                "password": "SecurePass123",
                "confirm_password": "SecurePass123",
                "accept_demo_policy": True,
            },
        )
        wrong = client.post(
            "/api/auth/login",
            json={"identifier": "aarav", "password": "WrongPass123"},
        )
        assert wrong.status_code == 401
        assert "Incorrect" in wrong.json()["detail"]
        unknown = client.post(
            "/api/auth/login",
            json={"identifier": "nobody", "password": "WrongPass123"},
        )
        assert unknown.status_code == 401


def test_account_lockout_after_repeated_failures(make_app):
    app = make_app(
        login_max_attempts=2,
        login_lock_seconds=60,
        auth_rate_limit=1000,
    )
    with TestClient(app) as client:
        client.post(
            "/api/auth/register",
            json={
                "username": "aarav",
                "email": "aarav@example.com",
                "display_name": "Aarav",
                "password": "SecurePass123",
                "confirm_password": "SecurePass123",
                "accept_demo_policy": True,
            },
        )
        first = client.post(
            "/api/auth/login",
            json={"identifier": "aarav", "password": "WrongPass123"},
        )
        assert first.status_code == 401
        second = client.post(
            "/api/auth/login",
            json={"identifier": "aarav", "password": "WrongPass123"},
        )
        assert second.status_code == 429
        assert second.headers.get("retry-after")
        locked = client.post(
            "/api/auth/login",
            json={"identifier": "aarav", "password": "SecurePass123"},
        )
        assert locked.status_code == 429


def test_authentication_rate_limit(make_app):
    app = make_app(
        auth_rate_limit=3,
        auth_rate_window_seconds=300,
        login_max_attempts=100,
    )
    with TestClient(app) as client:
        client.post(
            "/api/auth/register",
            json={
                "username": "aarav",
                "email": "aarav@example.com",
                "display_name": "Aarav",
                "password": "SecurePass123",
                "confirm_password": "SecurePass123",
                "accept_demo_policy": True,
            },
        )
        assert (
            client.post(
                "/api/auth/login",
                json={"identifier": "aarav", "password": "WrongPass123"},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/auth/login",
                json={"identifier": "aarav", "password": "WrongPass123"},
            ).status_code
            == 401
        )
        limited = client.post(
            "/api/auth/login",
            json={"identifier": "aarav", "password": "SecurePass123"},
        )
        assert limited.status_code == 429
        assert limited.json()["code"] == "rate_limited"


def test_protected_endpoints_and_csrf(client, signup):
    client.cookies.clear()
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/profile").status_code == 401
    assert client.get("/api/dashboard").status_code == 401
    assert client.get("/api/transactions").status_code == 401

    actor = signup("aarav")
    missing = client.post("/api/deposits", json={"amount": "10.00"})
    assert missing.status_code == 403
    assert missing.json()["code"] == "csrf_failed"
    wrong = client.post(
        "/api/deposits",
        json={"amount": "10.00"},
        headers={"X-CSRF-Token": "not-the-token"},
    )
    assert wrong.status_code == 403

    bad_origin_header = client.get("/api/auth/me")
    assert bad_origin_header.headers["cache-control"] == "no-store"
    assert bad_origin_header.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in bad_origin_header.headers[
        "content-security-policy"
    ]


def test_session_expiration_is_enforced(client, signup, app):
    actor = signup("aarav")
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(
        timespec="seconds"
    )
    with app.state.db.transaction() as conn:
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE user_id = ?",
            (past, actor.user["id"]),
        )
    assert client.get("/api/auth/me").status_code == 401


def test_profile_update_and_duplicate_email(client, signup, act_as):
    first = signup("aarav")
    second = signup("diya")

    act_as(first)
    updated = client.patch(
        "/api/profile",
        json={"display_name": "Aarav S.", "email": "aarav.new@example.com"},
        headers=first.headers,
    )
    assert updated.status_code == 200
    assert updated.json()["user"]["display_name"] == "Aarav S."
    assert updated.json()["user"]["email"] == "aarav.new@example.com"

    duplicate = client.patch(
        "/api/profile",
        json={"display_name": "Aarav S.", "email": second.email},
        headers=first.headers,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["fields"]["email"]

    invalid = client.patch(
        "/api/profile",
        json={"display_name": "Aarav S.", "email": "broken"},
        headers=first.headers,
    )
    assert invalid.status_code == 422


def test_change_password_requires_current_and_revokes_other_sessions(
    client, signup, login, act_as
):
    actor = signup("aarav")
    other = login("aarav")

    act_as(actor)
    wrong = client.post(
        "/api/profile/password",
        json={
            "current_password": "WrongPass123",
            "new_password": "NewSecure456",
            "confirm_new_password": "NewSecure456",
        },
        headers=actor.headers,
    )
    assert wrong.status_code == 401

    mismatch = client.post(
        "/api/profile/password",
        json={
            "current_password": "SecurePass123",
            "new_password": "NewSecure456",
            "confirm_new_password": "NewSecure457",
        },
        headers=actor.headers,
    )
    assert mismatch.status_code == 422

    changed = client.post(
        "/api/profile/password",
        json={
            "current_password": "SecurePass123",
            "new_password": "NewSecure456",
            "confirm_new_password": "NewSecure456",
        },
        headers=actor.headers,
    )
    assert changed.status_code == 200

    act_as(other)
    assert client.get("/api/auth/me").status_code == 401

    act_as(actor)
    assert client.get("/api/auth/me").status_code == 200

    client.cookies.clear()
    old_login = client.post(
        "/api/auth/login",
        json={"identifier": "aarav", "password": "SecurePass123"},
    )
    assert old_login.status_code == 401
    new_login = client.post(
        "/api/auth/login",
        json={"identifier": "aarav", "password": "NewSecure456"},
    )
    assert new_login.status_code == 200


def test_public_config_labels_simulation(client):
    config = client.get("/api/config")
    assert config.status_code == 200
    body = config.json()
    assert "simulated" in body["disclaimer"].lower()
    assert body["currency"] == "INR"
    assert body["demo_starting_balance_paise"] == 0
    assert body["allow_demo_deposits"] is True
