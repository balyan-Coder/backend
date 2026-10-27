"""User-scoped WebSocket hub.

Financial events are published only after the database transaction commits.
Connections are bound to one authenticated user at handshake time and there is
no subscription protocol, so a client can never listen to another user's keys.

The registry uses a thread lock and schedules sends on each connection's own
event loop.  In production every connection shares one Uvicorn loop; the
loop-aware handling also keeps the hub correct in threaded test clients.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


@dataclass
class _Connection:
    websocket: WebSocket
    loop: asyncio.AbstractEventLoop
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[int, list[_Connection]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ registry
    async def connect(self, user_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        connection = _Connection(websocket=websocket, loop=asyncio.get_running_loop())
        with self._lock:
            self._connections.setdefault(user_id, []).append(connection)
        logger.info("WebSocket connected for user %s", user_id)

    def _remove(self, user_id: int, connection: _Connection) -> None:
        with self._lock:
            bucket = self._connections.get(user_id)
            if bucket is not None:
                try:
                    bucket.remove(connection)
                except ValueError:
                    pass
                if not bucket:
                    self._connections.pop(user_id, None)

    async def disconnect(self, user_id: int, websocket: WebSocket) -> None:
        with self._lock:
            connection = next(
                (c for c in self._connections.get(user_id, ()) if c.websocket is websocket),
                None,
            )
        if connection is not None:
            self._remove(user_id, connection)
        logger.info("WebSocket disconnected for user %s", user_id)

    def connection_count(self, user_id: int) -> int:
        with self._lock:
            return len(self._connections.get(user_id, ()))

    def active_users(self) -> int:
        with self._lock:
            return len(self._connections)

    def online_user_ids(self) -> set[int]:
        """Return the users with at least one authenticated live socket."""

        with self._lock:
            return {
                user_id
                for user_id, bucket in self._connections.items()
                if bucket
            }

    def is_online(self, user_id: int) -> bool:
        with self._lock:
            return bool(self._connections.get(user_id))

    # -------------------------------------------------------------------- sending
    async def _send(self, connection: _Connection, payload: dict[str, Any]) -> None:
        async with connection.send_lock:
            await connection.websocket.send_json(payload)

    async def _run_on_connection(self, connection: _Connection, coroutine: Any) -> None:
        if connection.loop is asyncio.get_running_loop():
            await coroutine
            return
        future = asyncio.run_coroutine_threadsafe(coroutine, connection.loop)
        await asyncio.wrap_future(future)

    async def send_to_socket(self, websocket: WebSocket, payload: dict[str, Any]) -> bool:
        with self._lock:
            connection = next(
                (
                    c
                    for bucket in self._connections.values()
                    for c in bucket
                    if c.websocket is websocket
                ),
                None,
            )
        if connection is None:
            return False
        try:
            await self._run_on_connection(connection, self._send(connection, payload))
            return True
        except Exception:  # noqa: BLE001
            return False

    async def send_to_user(self, user_id: int, payload: dict[str, Any]) -> int:
        with self._lock:
            connections = list(self._connections.get(user_id, ()))
        sent = 0
        for connection in connections:
            try:
                await self._run_on_connection(
                    connection, self._send(connection, payload)
                )
                sent += 1
            except Exception:  # noqa: BLE001 - stale sockets are discarded
                self._remove(user_id, connection)
        return sent

    async def publish(self, notifications: list[tuple[int, dict[str, Any]]]) -> None:
        for user_id, payload in notifications:
            await self.send_to_user(user_id, payload)

    async def close_all(self) -> None:
        with self._lock:
            connections = [
                c for bucket in self._connections.values() for c in bucket
            ]
            self._connections.clear()
        for connection in connections:
            try:
                await self._run_on_connection(connection, connection.websocket.close(code=1001))
            except Exception:  # noqa: BLE001
                continue
