"""Transfer persistence and idempotency lookups."""

from __future__ import annotations

import sqlite3
from typing import Any


class TransferRepository:
    def create(
        self,
        conn: sqlite3.Connection,
        *,
        reference: str,
        from_account_id: int,
        to_account_id: int,
        initiated_by_user_id: int,
        amount_paise: int,
        description: str | None,
        idempotency_key: str,
        created_at: str,
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO transfers(
                reference, from_account_id, to_account_id, initiated_by_user_id,
                amount_paise, description, idempotency_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reference,
                from_account_id,
                to_account_id,
                initiated_by_user_id,
                amount_paise,
                description,
                idempotency_key,
                created_at,
            ),
        )
        return int(cursor.lastrowid)

    def get_by_idempotency(
        self, conn: sqlite3.Connection, idempotency_key: str
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM transfers WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return dict(row) if row else None

    def get_by_reference(self, conn: sqlite3.Connection, reference: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM transfers WHERE reference = ?", (reference,)).fetchone()
        return dict(row) if row else None

    def ledger_rows(self, conn: sqlite3.Connection, transfer_id: int) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT t.*, a.account_number
            FROM transactions t
            JOIN accounts a ON a.id = t.account_id
            WHERE t.transfer_id = ?
            ORDER BY t.id ASC
            """,
            (transfer_id,),
        ).fetchall()
        return [dict(row) for row in rows]
