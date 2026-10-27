from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from fastapi.testclient import TestClient

from app.errors import BankingError
from app.main import create_app


def dashboard(client: TestClient) -> dict:
    response = client.get("/api/dashboard")
    assert response.status_code == 200, response.text
    return response.json()


def set_account_status(app, account_id: int, status: str) -> None:
    with app.state.db.transaction() as conn:
        conn.execute(
            "UPDATE accounts SET status = ? WHERE id = ?", (status, account_id)
        )


def test_deposit_is_atomic_and_updates_dashboard(client, signup):
    actor = signup("aarav")
    response = client.post(
        "/api/deposits",
        json={
            "amount": "1500.25",
            "description": "Test top-up",
            "idempotency_key": "deposit-key-0001",
        },
        headers=actor.headers,
    )
    assert response.status_code == 200, response.text
    receipt = response.json()["receipt"]
    assert receipt["amount_paise"] == 150_025
    assert receipt["balance_paise"] == 1_150_025
    assert receipt["direction"] == "credit"
    assert receipt["duplicate"] is False

    data = dashboard(client)
    assert data["account"]["balance_paise"] == 1_150_025
    assert data["totals"]["deposits_paise"] == 1_150_025
    assert data["totals"]["withdrawals_paise"] == 0
    assert data["recent_transactions"][0]["reference"] == receipt["reference"]
    assert data["recent_transactions"][0]["balance_after_paise"] == 1_150_025

    listing = client.get("/api/transactions").json()
    assert listing["total"] == 2
    latest = listing["items"][0]
    assert latest["type"] == "deposit"
    assert latest["status"] == "success"
    assert latest["description"] == "Test top-up"


def test_invalid_amounts_are_rejected(client, signup):
    actor = signup("aarav")
    for amount in ("0", "-10.00", "10.999", "not-a-number"):
        response = client.post(
            "/api/deposits",
            json={"amount": amount, "idempotency_key": f"bad-{amount}"},
            headers=actor.headers,
        )
        assert response.status_code == 422, amount
    assert dashboard(client)["account"]["balance_paise"] == 1_000_000


def test_per_operation_and_daily_deposit_limits(client, signup):
    actor = signup("aarav")
    too_large = client.post(
        "/api/deposits",
        json={"amount": "50000.01", "idempotency_key": "limit-key-0001"},
        headers=actor.headers,
    )
    assert too_large.status_code == 400
    assert too_large.json()["code"] == "limit_exceeded"
    assert dashboard(client)["account"]["balance_paise"] == 1_000_000


def test_daily_deposit_limit_excludes_demo_credit(make_app):
    app = make_app(daily_deposit_limit_paise=200_000)
    with TestClient(app) as client:
        registration = client.post(
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
        csrf = registration.json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}
        first = client.post(
            "/api/deposits",
            json={"amount": "1500.00", "idempotency_key": "daily-key-0001"},
            headers=headers,
        )
        assert first.status_code == 200, first.text
        second = client.post(
            "/api/deposits",
            json={"amount": "500.01", "idempotency_key": "daily-key-0002"},
            headers=headers,
        )
        assert second.status_code == 400
        assert second.json()["code"] == "daily_limit_exceeded"
        assert dashboard(client)["account"]["balance_paise"] == 150_000


def test_withdrawal_and_insufficient_funds(client, signup):
    actor = signup("aarav")
    withdrawn = client.post(
        "/api/withdrawals",
        json={"amount": "2500.00", "idempotency_key": "withdraw-key-0001"},
        headers=actor.headers,
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["receipt"]["balance_paise"] == 750_000

    insufficient = client.post(
        "/api/withdrawals",
        json={"amount": "10000.01", "idempotency_key": "withdraw-key-0002"},
        headers=actor.headers,
    )
    assert insufficient.status_code == 400
    assert insufficient.json()["code"] == "insufficient_funds"

    data = dashboard(client)
    assert data["account"]["balance_paise"] == 750_000
    assert data["totals"]["withdrawals_paise"] == 250_000
    assert data["totals"]["failed_count"] == 1

    failed = client.get("/api/transactions?status=failed").json()
    assert failed["total"] == 1
    row = failed["items"][0]
    assert row["type"] == "withdrawal"
    assert row["status"] == "failed"
    assert row["balance_before_paise"] == row["balance_after_paise"] == 750_000


def test_frozen_account_blocks_operations(client, signup, app, act_as):
    first = signup("aarav")
    second = signup("diya")
    act_as(first)
    set_account_status(app, first.account["id"], "frozen")

    deposit = client.post(
        "/api/deposits",
        json={"amount": "10.00", "idempotency_key": "frozen-deposit-1"},
        headers=first.headers,
    )
    assert deposit.status_code == 400
    assert deposit.json()["code"] == "account_inactive"

    withdrawal = client.post(
        "/api/withdrawals",
        json={"amount": "10.00", "idempotency_key": "frozen-withdraw-1"},
        headers=first.headers,
    )
    assert withdrawal.status_code == 400
    assert withdrawal.json()["code"] == "account_inactive"

    transfer = client.post(
        "/api/transfers",
        json={
            "recipient_account_number": second.account_number,
            "amount": "10.00",
            "idempotency_key": "frozen-transfer-1",
        },
        headers=first.headers,
    )
    assert transfer.status_code == 400
    assert transfer.json()["code"] == "account_inactive"


def test_frozen_recipient_blocks_transfer(client, signup, app, act_as):
    first = signup("aarav")
    second = signup("diya")
    act_as(first)
    set_account_status(app, second.account["id"], "frozen")
    response = client.post(
        "/api/transfers",
        json={
            "recipient_account_number": second.account_number,
            "amount": "10.00",
            "idempotency_key": "frozen-recipient-1",
        },
        headers=first.headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "recipient_inactive"
    assert dashboard(client)["account"]["balance_paise"] == 1_000_000


def test_transfer_resolution_and_completed_transfer(client, signup, act_as, app):
    first = signup("aarav", display_name="Aarav Sharma")
    second = signup("diya", display_name="Diya Kapoor")
    act_as(first)

    resolved = client.post(
        "/api/transfers/resolve",
        json={"recipient_account_number": second.account_number},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["display_name"] == "Diya Kapoor"
    assert resolved.json()["masked_account_number"].endswith(
        second.account_number[-4:]
    )

    response = client.post(
        "/api/transfers",
        json={
            "recipient_account_number": second.account_number,
            "amount": "2500.50",
            "description": "Dinner split",
            "idempotency_key": "transfer-key-0001",
        },
        headers=first.headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    receipt = body["receipt"]
    assert receipt["amount_paise"] == 250_050
    assert receipt["balance_paise"] == 749_950
    assert receipt["counterparty_name"] == "Diya Kapoor"
    assert receipt["reference"].startswith("TRF-")
    assert receipt["duplicate"] is False

    sender_dashboard = dashboard(client)
    assert sender_dashboard["account"]["balance_paise"] == 749_950
    assert sender_dashboard["totals"]["transfers_out_paise"] == 250_050
    sender_row = client.get("/api/transactions?type=transfer_debit").json()["items"][0]
    assert sender_row["transfer_reference"] == receipt["reference"]
    assert sender_row["counterparty_name"] == "Diya Kapoor"
    assert sender_row["direction"] == "debit"

    act_as(second)
    recipient_dashboard = dashboard(client)
    assert recipient_dashboard["account"]["balance_paise"] == 1_250_050
    assert recipient_dashboard["totals"]["transfers_in_paise"] == 250_050
    recipient_row = client.get("/api/transactions?type=transfer_credit").json()[
        "items"
    ][0]
    assert recipient_row["transfer_reference"] == receipt["reference"]
    assert recipient_row["counterparty_name"] == "Aarav Sharma"
    assert recipient_row["direction"] == "credit"

    with app.state.db.connection() as conn:
        ledger = conn.execute(
            """
            SELECT t.* FROM transactions t
            JOIN transfers tr ON tr.id = t.transfer_id
            WHERE tr.reference = ?
            ORDER BY t.id
            """,
            (receipt["reference"],),
        ).fetchall()
    assert len(ledger) == 2
    assert {row["type"] for row in ledger} == {"transfer_debit", "transfer_credit"}
    assert sum(row["amount_paise"] for row in ledger) == 500_100


def test_transfer_rejects_self_unknown_and_insufficient(client, signup, act_as):
    first = signup("aarav")
    second = signup("diya")
    act_as(first)

    self_transfer = client.post(
        "/api/transfers",
        json={
            "recipient_account_number": first.account_number,
            "amount": "10.00",
            "idempotency_key": "self-transfer-01",
        },
        headers=first.headers,
    )
    assert self_transfer.status_code in {400, 422}
    assert self_transfer.json()["code"] in {"self_transfer", "validation_error"}

    unknown = client.post(
        "/api/transfers",
        json={
            "recipient_account_number": "999999999999",
            "amount": "10.00",
            "idempotency_key": "unknown-transfer-1",
        },
        headers=first.headers,
    )
    assert unknown.status_code == 400
    assert unknown.json()["code"] == "recipient_not_found"

    insufficient = client.post(
        "/api/transfers",
        json={
            "recipient_account_number": second.account_number,
            "amount": "10000.01",
            "idempotency_key": "insufficient-trf-1",
        },
        headers=first.headers,
    )
    assert insufficient.status_code == 400
    assert insufficient.json()["code"] == "insufficient_funds"
    assert dashboard(client)["account"]["balance_paise"] == 1_000_000


def test_duplicate_transfer_request_is_idempotent(client, signup, act_as, app):
    first = signup("aarav")
    second = signup("diya")
    act_as(first)
    payload = {
        "recipient_account_number": second.account_number,
        "amount": "125.00",
        "description": "Idempotent payment",
        "idempotency_key": "duplicate-key-0001",
    }
    first_response = client.post(
        "/api/transfers", json=payload, headers=first.headers
    )
    assert first_response.status_code == 200, first_response.text
    second_response = client.post(
        "/api/transfers", json=payload, headers=first.headers
    )
    assert second_response.status_code == 200
    assert second_response.json()["receipt"]["duplicate"] is True
    assert (
        second_response.json()["receipt"]["reference"]
        == first_response.json()["receipt"]["reference"]
    )

    assert dashboard(client)["account"]["balance_paise"] == 987_500
    act_as(second)
    assert dashboard(client)["account"]["balance_paise"] == 1_012_500

    with app.state.db.connection() as conn:
        transfer_count = conn.execute(
            "SELECT COUNT(*) FROM transfers WHERE idempotency_key = ?",
            ("duplicate-key-0001",),
        ).fetchone()[0]
        debit_count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE type = 'transfer_debit'"
        ).fetchone()[0]
    assert transfer_count == 1
    assert debit_count == 1


def test_transfer_rollback_leaves_no_partial_state(
    client, signup, act_as, app, monkeypatch
):
    first = signup("aarav")
    second = signup("diya")
    act_as(first)
    service = app.state.banking_service
    original_credit = service.accounts.credit

    def failing_credit(conn, account_id, amount_paise, now):
        if account_id == int(second.account["id"]):
            return False
        return original_credit(conn, account_id, amount_paise, now)

    monkeypatch.setattr(service.accounts, "credit", failing_credit)
    response = client.post(
        "/api/transfers",
        json={
            "recipient_account_number": second.account_number,
            "amount": "500.00",
            "idempotency_key": "rollback-key-0001",
        },
        headers=first.headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "recipient_inactive"

    with app.state.db.connection() as conn:
        sender_balance = conn.execute(
            "SELECT balance_paise FROM accounts WHERE id = ?",
            (first.account["id"],),
        ).fetchone()[0]
        recipient_balance = conn.execute(
            "SELECT balance_paise FROM accounts WHERE id = ?",
            (second.account["id"],),
        ).fetchone()[0]
        transfer_count = conn.execute(
            "SELECT COUNT(*) FROM transfers WHERE idempotency_key = ?",
            ("rollback-key-0001",),
        ).fetchone()[0]
        linked_rows = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE transfer_id IS NOT NULL"
        ).fetchone()[0]
    assert sender_balance == 1_000_000
    assert recipient_balance == 1_000_000
    assert transfer_count == 0
    assert linked_rows == 0


def test_concurrent_transfers_cannot_overdraw(app, signup):
    first = signup("aarav")
    second = signup("diya")
    third = signup("kabir")
    service = app.state.banking_service
    barrier = threading.Barrier(2)

    def attempt(recipient: str, key: str):
        barrier.wait(timeout=10)
        try:
            outcome = service.transfer(
                user_id=first.user["id"],
                recipient_account_number=recipient,
                amount=Decimal("6000.00"),
                description="Concurrent race",
                idempotency_key=key,
            )
            return "ok", outcome.receipt["reference"]
        except BankingError as exc:
            return "error", exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda args: attempt(*args),
                [
                    (second.account_number, "concurrent-key-01"),
                    (third.account_number, "concurrent-key-02"),
                ],
            )
        )
    outcomes = sorted(result[0] for result in results)
    assert outcomes == ["error", "ok"]
    assert [result[1] for result in results if result[0] == "error"] == [
        "insufficient_funds"
    ]

    with app.state.db.connection() as conn:
        balances = {
            row["id"]: row["balance_paise"]
            for row in conn.execute("SELECT id, balance_paise FROM accounts")
        }
        transfer_count = conn.execute(
            "SELECT COUNT(*) FROM transfers WHERE idempotency_key LIKE 'concurrent-key-%'"
        ).fetchone()[0]
    assert balances[first.account["id"]] == 400_000
    assert sorted(
        [
            balances[second.account["id"]],
            balances[third.account["id"]],
        ]
    ) == [1_000_000, 1_600_000]
    assert transfer_count == 1
    assert sum(balances.values()) == 3_000_000


def test_persistence_across_application_restart(settings):
    first_app = create_app(settings)
    with TestClient(first_app) as client:
        registration = client.post(
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
        csrf = registration.json()["csrf_token"]
        deposit = client.post(
            "/api/deposits",
            json={"amount": "777.77", "idempotency_key": "persist-key-0001"},
            headers={"X-CSRF-Token": csrf},
        )
        assert deposit.status_code == 200
        reference = deposit.json()["receipt"]["reference"]

    second_app = create_app(settings)
    with TestClient(second_app) as client:
        login = client.post(
            "/api/auth/login",
            json={"identifier": "aarav", "password": "SecurePass123"},
        )
        assert login.status_code == 200, login.text
        data = dashboard(client)
        assert data["account"]["balance_paise"] == 77_777
        listing = client.get("/api/transactions").json()
        assert listing["total"] == 1
        assert any(row["reference"] == reference for row in listing["items"])


def test_csv_export_contains_only_own_transactions(
    client, signup, act_as
):
    first = signup("aarav")
    second = signup("diya")
    act_as(first)
    assert (
        client.post(
            "/api/deposits",
            json={
                "amount": "11.00",
                "description": "A-ONLY-MARKER",
                "idempotency_key": "csv-a-key-0001",
            },
            headers=first.headers,
        ).status_code
        == 200
    )
    act_as(second)
    assert (
        client.post(
            "/api/deposits",
            json={
                "amount": "22.00",
                "description": "B-ONLY-MARKER",
                "idempotency_key": "csv-b-key-0001",
            },
            headers=second.headers,
        ).status_code
        == 200
    )

    act_as(first)
    export = client.get("/api/transactions/export")
    assert export.status_code == 200
    assert "text/csv" in export.headers["content-type"]
    assert "attachment" in export.headers["content-disposition"]
    text = export.content.decode("utf-8-sig")
    assert "A-ONLY-MARKER" in text
    assert "B-ONLY-MARKER" not in text
    assert "11.00" in text


def test_transaction_isolation_between_users(client, signup, act_as):
    first = signup("aarav")
    second = signup("diya")
    act_as(first)
    deposit = client.post(
        "/api/deposits",
        json={
            "amount": "33.00",
            "description": "PRIVATE-A",
            "idempotency_key": "isolation-key-01",
        },
        headers=first.headers,
    )
    first_txn_id = deposit.json()["receipt"]["reference"]

    listing = client.get("/api/transactions").json()
    first_id = listing["items"][0]["id"]

    act_as(second)
    assert client.get(f"/api/transactions/{first_id}").status_code == 404
    search = client.get("/api/transactions?search=PRIVATE-A").json()
    assert search["total"] == 0
    assert client.get("/api/transactions/export").content.decode(
        "utf-8-sig"
    ).count("PRIVATE-A") == 0
    assert first_txn_id not in client.get("/api/transactions/export").text


def test_transaction_filters_and_pagination(client, signup):
    actor = signup("aarav")
    for index in range(6):
        response = client.post(
            "/api/deposits",
            json={
                "amount": f"{index + 1}.00",
                "description": f"Filter row {index}",
                "idempotency_key": f"filter-key-{index:04d}",
            },
            headers=actor.headers,
        )
        assert response.status_code == 200

    page = client.get("/api/transactions?page=1&page_size=5").json()
    assert page["total"] == 7
    assert page["pages"] == 2
    assert len(page["items"]) == 5

    filtered = client.get("/api/transactions?type=deposit&status=success").json()
    assert filtered["total"] == 7
    assert all(row["type"] == "deposit" for row in filtered["items"])

    searched = client.get("/api/transactions?search=Filter row 3").json()
    assert searched["total"] == 1

    ascending = client.get("/api/transactions?sort=amount_paise&order=asc").json()
    amounts = [row["amount_paise"] for row in ascending["items"]]
    assert amounts == sorted(amounts)
