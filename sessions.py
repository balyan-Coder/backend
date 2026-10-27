"""Opaque session persistence.  Only hashes of tokens are stored."""

from __future__ import annotations

import sqlite3
from typing import Any


class SessionRepository:
    def create(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        token_hash: str,
        csrf_token: str,
        created_at: str,
        expires_at: str,
        ip_address: str | None,
        user_agent: str | None,
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO sessions(
                user_id, token_hash, csrf_token, created_at, last_seen_at,
                expires_at, ip_address, user_agent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                token_hash,
                csrf_token,
                created_at,
                created_at,
                expires_at,
                ip_address,
                (user_agent or "")[:300] or None,
            ),
        )
        return int(cursor.lastrowid)

    def find_active(self, conn: sqlite3.Connection, token_hash: str, now: str) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT s.id AS session_id, s.user_id, s.csrf_token, s.expires_at,
                   s.last_seen_at,
                   u.username, u.email, u.display_name, u.status, u.created_at AS user_created_at
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ?
            """,
            (token_hash, now),
        ).fetchone()
        return dict(row) if row else None

    def touch(self, conn: sqlite3.Connection, session_id: int, now: str) -> None:
        conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (now, session_id))

    def revoke(self, conn: sqlite3.Connection, token_hash: str, now: str) -> None:
        conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
            (now, token_hash),
        )

    def revoke_others(self, conn: sqlite3.Connection, user_id: int, current_session_id: int, now: str) -> None:
        conn.execute(
            """
            UPDATE sessions SET revoked_at = ?
            WHERE user_id = ? AND id <> ? AND revoked_at IS NULL
            """,
            (now, user_id, current_session_id),
        )

    def purge_expired(self, conn: sqlite3.Connection, now: str) -> None:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ? OR revoked_at IS NOT NULL", (now,))
