from __future__ import annotations

import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect

from app.realtime import ConnectionManager


class FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.messages: list[dict] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)


def test_websocket_requires_an_authenticated_session(client):
    client.cookies.clear()
    with client.websocket_connect("/api/ws") as websocket:
        with pytest.raises(WebSocketDisconnect) as error:
            websocket.receive_json()
    assert error.value.code == 4401


def test_websocket_rejects_invalid_session_cookie(client, settings):
    client.cookies.clear()
    client.cookies.set(settings.cookie_name, "this-is-not-a-valid-session")
    with client.websocket_connect("/api/ws") as websocket:
        with pytest.raises(WebSocketDisconnect) as error:
            websocket.receive_json()
    assert error.value.code == 4401


def test_websocket_ready_ping_and_subscription_refusal(client, signup):
    signup("aarav")
    with client.websocket_connect("/api/ws") as websocket:
        ready = websocket.receive_json()
        assert ready["type"] == "ready"
        assert ready["message"].startswith("Connected")
        assert ready["simulated"] is True

        websocket.send_text('{"type":"ping"}')
        assert websocket.receive_json()["type"] == "pong"

        websocket.send_text(
            '{"type":"subscribe","channel":"account:999999999999"}'
        )
        refusal = websocket.receive_json()
        assert refusal["type"] == "error"
        assert refusal["code"] == "subscriptions_not_supported"


def test_recipient_receives_realtime_credit_after_commit(client, signup, act_as):
    sender = signup("aarav", display_name="Aarav Sharma")
    recipient = signup("diya", display_name="Diya Kapoor")
    act_as(recipient)
    with client.websocket_connect("/api/ws") as websocket:
        assert websocket.receive_json()["type"] == "ready"

        act_as(sender)
        response = client.post(
            "/api/transfers",
            json={
                "recipient_account_number": recipient.account_number,
                "amount": "250.00",
                "description": "Realtime lunch",
                "idempotency_key": "realtime-key-0001",
            },
            headers=sender.headers,
        )
        assert response.status_code == 200, response.text
        reference = response.json()["receipt"]["reference"]

        event = websocket.receive_json()
        assert event["type"] == "transaction"
        assert event["direction"] == "credit"
        assert event["transaction_type"] == "transfer_credit"
        assert event["amount_paise"] == 25_000
        assert event["balance_paise"] == 1_025_000
        assert event["reference"] == reference
        assert event["counterparty_name"] == "Aarav Sharma"
        assert "account_number" not in event

    act_as(recipient)
    assert client.get("/api/dashboard").json()["account"]["balance_paise"] == 1_025_000


def test_sender_and_recipient_both_receive_scoped_events(client, signup, act_as):
    sender = signup("aarav", display_name="Aarav Sharma")
    recipient = signup("diya", display_name="Diya Kapoor")

    act_as(sender)
    with client.websocket_connect("/api/ws") as sender_ws:
        assert sender_ws.receive_json()["type"] == "ready"
        act_as(recipient)
        with client.websocket_connect("/api/ws") as recipient_ws:
            assert recipient_ws.receive_json()["type"] == "ready"

            act_as(sender)
            response = client.post(
                "/api/transfers",
                json={
                    "recipient_account_number": recipient.account_number,
                    "amount": "100.00",
                    "idempotency_key": "realtime-key-0002",
                },
                headers=sender.headers,
            )
            assert response.status_code == 200, response.text

            sender_event = sender_ws.receive_json()
            recipient_event = recipient_ws.receive_json()
            assert sender_event["direction"] == "debit"
            assert sender_event["transaction_type"] == "transfer_debit"
            assert sender_event["balance_paise"] == 990_000
            assert sender_event["counterparty_name"] == "Diya Kapoor"
            assert recipient_event["direction"] == "credit"
            assert recipient_event["balance_paise"] == 1_010_000


def test_reconnect_reconciles_missed_event_from_database(client, signup, act_as):
    sender = signup("aarav")
    recipient = signup("diya")
    act_as(recipient)
    with client.websocket_connect("/api/ws") as websocket:
        assert websocket.receive_json()["type"] == "ready"

    # Recipient is offline while the transfer commits.
    act_as(sender)
    response = client.post(
        "/api/transfers",
        json={
            "recipient_account_number": recipient.account_number,
            "amount": "49.99",
            "idempotency_key": "reconnect-key-0001",
        },
        headers=sender.headers,
    )
    assert response.status_code == 200, response.text

    act_as(recipient)
    with client.websocket_connect("/api/ws") as websocket:
        ready = websocket.receive_json()
        assert ready["type"] == "ready"
        websocket.send_text('{"type":"ping"}')
        assert websocket.receive_json()["type"] == "pong"

    dashboard = client.get("/api/dashboard").json()
    assert dashboard["account"]["balance_paise"] == 1_004_999
    latest = dashboard["recent_transactions"][0]
    assert latest["type"] == "transfer_credit"
    assert latest["amount_paise"] == 4_999


def test_connection_manager_routes_only_to_the_target_user():
    async def scenario() -> None:
        manager = ConnectionManager()
        first = FakeWebSocket()
        second = FakeWebSocket()
        await manager.connect(1, first)  # type: ignore[arg-type]
        await manager.connect(2, second)  # type: ignore[arg-type]
        assert first.accepted and second.accepted

        sent = await manager.send_to_user(1, {"type": "transaction", "secret": "one"})
        assert sent == 1
        assert first.messages == [{"type": "transaction", "secret": "one"}]
        assert second.messages == []
        assert manager.connection_count(1) == 1
        assert manager.active_users() == 2

        await manager.disconnect(1, first)  # type: ignore[arg-type]
        assert manager.connection_count(1) == 0
        assert manager.active_users() == 1

    asyncio.run(scenario())


def test_unrelated_connection_does_not_receive_another_users_event():
    async def scenario() -> None:
        manager = ConnectionManager()
        outsider = FakeWebSocket()
        recipient = FakeWebSocket()
        await manager.connect(5, outsider)  # type: ignore[arg-type]
        await manager.connect(6, recipient)  # type: ignore[arg-type]

        await manager.send_to_user(6, {"type": "transaction", "amount_paise": 100})
        assert outsider.messages == []
        assert len(recipient.messages) == 1

    asyncio.run(scenario())
