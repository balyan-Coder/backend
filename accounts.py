"""Account persistence and conditional balance updates."""

from __future__ import annotations

import sqlite3
from typing import Any


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


class AccountRepository:
    def create(
        self,
        conn: sqlite3.Connection,
        *,
        account_number: str,
        user_id: int,
        account_type: str,
        balance_paise: int,
        now: str,
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO accounts(
                account_number, user_id, account_type, balance_paise,
                status, currency, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'active', 'INR', ?, ?)
            """,
            (account_number, user_id, account_type, balance_paise, now, now),
        )
        return int(cursor.lastrowid)

    def get_by_user_id(self, conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
        return _dict(
            conn.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,)).fetchone()
        )

    def get_by_id(self, conn: sqlite3.Connection, account_id: int) -> dict[str, Any] | None:
        return _dict(
            conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        )

    def get_by_number(self, conn: sqlite3.Connection, account_number: str) -> dict[str, Any] | None:
        return _dict(
            conn.execute(
                "SELECT * FROM accounts WHERE account_number = ?", (account_number,)
            ).fetchone()
        )

    def credit(self, conn: sqlite3.Connection, account_id: int, amount_paise: int, now: str) -> bool:
        cursor = conn.execute(
            """
            UPDATE accounts
            SET balance_paise = balance_paise + ?, updated_at = ?
            WHERE id = ? AND status = 'active'
            """,
            (amount_paise, now, account_id),
        )
        return cursor.rowcount == 1

    def debit(self, conn: sqlite3.Connection, account_id: int, amount_paise: int, now: str) -> bool:
        cursor = conn.execute(
            """
            UPDATE accounts
            SET balance_paise = balance_paise - ?, updated_at = ?
            WHERE id = ? AND status = 'active' AND balance_paise >= ?
            """,
            (amount_paise, now, account_id, amount_paise),
        )
        return cursor.rowcount == 1

    def set_status(self, conn: sqlite3.Connection, account_id: int, status: str, now: str) -> None:
        conn.execute(
            "UPDATE accounts SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, account_id),
        )

    def summary(self, conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT a.*, u.display_name, u.username, u.email
            FROM accounts a
            JOIN users u ON u.id = a.user_id
            WHERE a.user_id = ?
            """,
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
