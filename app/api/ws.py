"""Authenticated, user-scoped WebSocket endpoint."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.deps import websocket_session
from app.realtime import ConnectionManager
from app.utils.time import iso

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    state = websocket.app.state
    context = websocket_session(
        websocket,
        db=state.db,
        auth=state.auth_service,
        settings=state.settings,
    )
    if context is None:
        await websocket.accept()
        await websocket.close(code=4401, reason="Authentication required")
        return

    manager: ConnectionManager = state.realtime
    await manager.connect(context.user_id, websocket)
    await manager.send_to_socket(
        websocket,
        {
            "type": "ready",
            "user_id": context.user_id,
            "message": "Connected. This channel is scoped to your account.",
            "at": iso(),
            "simulated": True,
        },
    )
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(message, dict):
                continue
            kind = message.get("type")
            if kind == "ping":
                await manager.send_to_socket(
                    websocket, {"type": "pong", "at": iso()}
                )
            elif kind == "subscribe":
                # There is deliberately no cross-account subscription API.
                await manager.send_to_socket(
                    websocket,
                    {
                        "type": "error",
                        "code": "subscriptions_not_supported",
                        "detail": (
                            "This connection already receives only your own "
                            "account events. Subscribing to other accounts is not allowed."
                        ),
                    },
                )
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - never leak socket details
        logger.info("WebSocket closed unexpectedly for user %s", context.user_id)
    finally:
        await manager.disconnect(context.user_id, websocket)
