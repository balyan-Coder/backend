"""Ledger persistence, filtering and aggregates."""

from __future__ import annotations

import sqlite3
from typing import Any


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


TRANSACTION_SELECT = """
    SELECT t.*,
           cp.account_number AS counterparty_account_number,
           cu.display_name AS counterparty_name,
           tr.reference AS transfer_reference
    FROM transactions t
    LEFT JOIN accounts cp ON cp.id = t.counterparty_account_id
    LEFT JOIN users cu ON cu.id = cp.user_id
    LEFT JOIN transfers tr ON tr.id = t.transfer_id
"""


class TransactionRepository:
    def insert(
        self,
        conn: sqlite3.Connection,
        *,
        reference: str,
        account_id: int,
        counterparty_account_id: int | None,
        type_: str,
        direction: str,
        amount_paise: int,
        balance_before_paise: int,
        balance_after_paise: int,
        status: str,
        description: str | None,
        transfer_id: int | None,
        idempotency_key: str | None,
        created_at: str,
        counts_toward_limit: int = 1,
        counterparty_label: str | None = None,
        counterparty_external_number: str | None = None,
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO transactions(
                reference, account_id, counterparty_account_id, type, direction,
                amount_paise, balance_before_paise, balance_after_paise,
                status, description, transfer_id, idempotency_key,
                counts_toward_limit, created_at,
                counterparty_label, counterparty_external_number
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reference,
                account_id,
                counterparty_account_id,
                type_,
                direction,
                amount_paise,
                balance_before_paise,
                balance_after_paise,
                status,
                description,
                transfer_id,
                idempotency_key,
                counts_toward_limit,
                created_at,
                counterparty_label,
                counterparty_external_number,
            ),
        )
        return int(cursor.lastrowid)

    def get_by_idempotency(
        self, conn: sqlite3.Connection, idempotency_key: str
    ) -> dict[str, Any] | None:
        row = conn.execute(
            TRANSACTION_SELECT + " WHERE t.idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return dict(row) if row else None

    def get_for_account(
        self, conn: sqlite3.Connection, transaction_id: int, account_id: int
    ) -> dict[str, Any] | None:
        return _row(
            conn.execute(
                TRANSACTION_SELECT + " WHERE t.id = ? AND t.account_id = ?",
                (transaction_id, account_id),
            ).fetchone()
        )

    def recent(self, conn: sqlite3.Connection, account_id: int, limit: int = 8) -> list[dict[str, Any]]:
        rows = conn.execute(
            TRANSACTION_SELECT
            + " WHERE t.account_id = ? ORDER BY t.created_at DESC, t.id DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def list(
        self,
        conn: sqlite3.Connection,
        *,
        account_id: int,
        page: int,
        page_size: int,
        search: str = "",
        type_filter: str = "all",
        status: str = "all",
        direction: str = "all",
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "created_at",
        order: str = "desc",
    ) -> tuple[list[dict[str, Any]], int]:
        where = ["t.account_id = ?"]
        params: list[Any] = [account_id]
        if search:
            like = f"%{search.strip()}%"
            where.append(
                "(t.reference LIKE ? OR t.description LIKE ? OR cp.account_number LIKE ?)"
            )
            params.extend([like, like, like])
        if type_filter and type_filter != "all":
            where.append("t.type = ?")
            params.append(type_filter)
        if status and status != "all":
            where.append("t.status = ?")
            params.append(status)
        if direction and direction != "all":
            where.append("t.direction = ?")
            params.append(direction)
        if date_from:
            where.append("t.created_at >= ?")
            params.append(date_from)
        if date_to:
            where.append("t.created_at < ?")
            params.append(date_to)
        clause = " WHERE " + " AND ".join(where)
        total = int(
            conn.execute(
                "SELECT COUNT(*) FROM transactions t "
                "LEFT JOIN accounts cp ON cp.id = t.counterparty_account_id" + clause,
                params,
            ).fetchone()[0]
        )
        sort_column = {
            "created_at": "t.created_at",
            "amount_paise": "t.amount_paise",
            "type": "t.type",
            "status": "t.status",
        }.get(sort, "t.created_at")
        direction_sql = "ASC" if str(order).lower() == "asc" else "DESC"
        offset = (max(1, page) - 1) * page_size
        rows = conn.execute(
            TRANSACTION_SELECT
            + clause
            + f" ORDER BY {sort_column} {direction_sql}, t.id {direction_sql} LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()
        return [dict(row) for row in rows], total

    def totals(self, conn: sqlite3.Connection, account_id: int) -> dict[str, Any]:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN type = 'deposit' AND status = 'success' THEN amount_paise END), 0) AS deposits_paise,
                COALESCE(SUM(CASE WHEN type = 'withdrawal' AND status = 'success' THEN amount_paise END), 0) AS withdrawals_paise,
                COALESCE(SUM(CASE WHEN type = 'transfer_credit' AND status = 'success' THEN amount_paise END), 0) AS transfers_in_paise,
                COALESCE(SUM(CASE WHEN type = 'transfer_debit' AND status = 'success' THEN amount_paise END), 0) AS transfers_out_paise,
                COALESCE(SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END), 0) AS successful_count,
                COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failed_count
            FROM transactions
            WHERE account_id = ?
            """,
            (account_id,),
        ).fetchone()
        return dict(row)

    def monthly_cashflow(
        self, conn: sqlite3.Connection, account_id: int, since: str
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT substr(created_at, 1, 7) AS month,
                   COALESCE(SUM(CASE WHEN type = 'deposit' AND status = 'success' THEN amount_paise END), 0) AS deposits_paise,
                   COALESCE(SUM(CASE WHEN type = 'withdrawal' AND status = 'success' THEN amount_paise END), 0) AS withdrawals_paise
            FROM transactions
            WHERE account_id = ? AND created_at >= ?
            GROUP BY substr(created_at, 1, 7)
            ORDER BY month ASC
            """,
            (account_id, since),
        ).fetchall()
        return [dict(row) for row in rows]

    def daily_activity(
        self, conn: sqlite3.Connection, account_id: int, since: str
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT substr(created_at, 1, 10) AS day,
                   COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN direction = 'credit' AND status = 'success' THEN 1 ELSE 0 END), 0) AS credits,
                   COALESCE(SUM(CASE WHEN direction = 'debit' AND status = 'success' THEN 1 ELSE 0 END), 0) AS debits,
                   COALESCE(SUM(CASE WHEN status = 'success' THEN amount_paise ELSE 0 END), 0) AS volume_paise
            FROM transactions
            WHERE account_id = ? AND created_at >= ?
            GROUP BY substr(created_at, 1, 10)
            ORDER BY day ASC
            """,
            (account_id, since),
        ).fetchall()
        return [dict(row) for row in rows]

    def successful_deposits_since(
        self, conn: sqlite3.Connection, account_id: int, since: str
    ) -> int:
        return int(
            conn.execute(
                """
                SELECT COALESCE(SUM(amount_paise), 0)
                FROM transactions
                WHERE account_id = ? AND type = 'deposit'
                  AND status = 'success' AND counts_toward_limit = 1
                  AND created_at >= ?
                """,
                (account_id, since),
            ).fetchone()[0]
        )
