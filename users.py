"""User account persistence."""

from __future__ import annotations

import sqlite3
from typing import Any


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


class UserRepository:
    def create(
        self,
        conn: sqlite3.Connection,
        *,
        username: str,
        email: str,
        display_name: str,
        password_hash: str,
        now: str,
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO users(
                username, email, display_name, password_hash,
                status, failed_attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'active', 0, ?, ?)
            """,
            (username, email, display_name, password_hash, now, now),
        )
        return int(cursor.lastrowid)

    def get_by_id(self, conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
        return _dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())

    def find_by_login(self, conn: sqlite3.Connection, identifier: str) -> dict[str, Any] | None:
        return _dict(
            conn.execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE OR email = ? COLLATE NOCASE",
                (identifier, identifier),
            ).fetchone()
        )

    def username_exists(self, conn: sqlite3.Connection, username: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)
            ).fetchone()
            is not None
        )

    def email_exists(self, conn: sqlite3.Connection, email: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM users WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone()
            is not None
        )

    def register_failure(
        self, conn: sqlite3.Connection, user_id: int, *, attempts: int, locked_until: str | None
    ) -> None:
        conn.execute(
            "UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
            (attempts, locked_until, user_id),
        )

    def register_success(self, conn: sqlite3.Connection, user_id: int, first_seen: str, now: str) -> None:
        conn.execute(
            """
            UPDATE users
            SET failed_attempts = 0, locked_until = NULL,
                last_login_at = COALESCE(?, ?), updated_at = ?
            WHERE id = ?
            """,
            (now, first_seen, now, user_id),
        )

    def update_profile(
        self, conn: sqlite3.Connection, user_id: int, *, display_name: str, email: str, now: str
    ) -> None:
        conn.execute(
            "UPDATE users SET display_name = ?, email = ?, updated_at = ? WHERE id = ?",
            (display_name, email, now, user_id),
        )

    def update_password(self, conn: sqlite3.Connection, user_id: int, password_hash: str, now: str) -> None:
        conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (password_hash, now, user_id),
        )

    def list_members(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        """Every user with their linked account, for owner-only presence views."""

        rows = conn.execute(
            """
            SELECT u.id, u.username, u.display_name, u.status AS user_status,
                   u.created_at, u.last_login_at,
                   a.account_number, a.status AS account_status, a.balance_paise
            FROM users u
            LEFT JOIN accounts a ON a.user_id = u.id
            ORDER BY u.id
            """
        ).fetchall()
        return [dict(row) for row in rows]
