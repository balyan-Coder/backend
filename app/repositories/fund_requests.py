"""Fund request persistence for ticketing system."""
from __future__ import annotations

import sqlite3
from typing import Any


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


class FundRequestRepository:
    def create(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        account_id: int,
        amount_paise: int,
        reason: str | None,
        requested_by: int,
        now: str,
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO fund_requests(
                user_id, account_id, amount_paise, reason,
                status, requested_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (user_id, account_id, amount_paise, reason, requested_by, now, now),
        )
        return int(cursor.lastrowid)

    def get_by_id(self, conn: sqlite3.Connection, request_id: int) -> dict[str, Any] | None:
        return _dict(conn.execute("SELECT * FROM fund_requests WHERE id = ?", (request_id,)).fetchone())

    def get_by_id_with_details(self, conn: sqlite3.Connection, request_id: int) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT fr.*, 
                   u.username, u.display_name, u.email,
                   a.account_number,
                   reviewer.username AS reviewed_by_username,
                   fulfiller.username AS fulfilled_by_username
            FROM fund_requests fr
            JOIN users u ON u.id = fr.user_id
            JOIN accounts a ON a.id = fr.account_id
            LEFT JOIN users reviewer ON reviewer.id = fr.reviewed_by
            LEFT JOIN users fulfiller ON fulfiller.id = fr.fulfilled_by
            WHERE fr.id = ?
            """,
            (request_id,),
        ).fetchone()
        return _dict(row)

    def list_by_user(self, conn: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT fr.*, 
                   a.account_number,
                   reviewer.username AS reviewed_by_username,
                   fulfiller.username AS fulfilled_by_username
            FROM fund_requests fr
            JOIN accounts a ON a.id = fr.account_id
            LEFT JOIN users reviewer ON reviewer.id = fr.reviewed_by
            LEFT JOIN users fulfiller ON fulfiller.id = fr.fulfilled_by
            WHERE fr.user_id = ?
            ORDER BY fr.created_at DESC
            """,
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_all(self, conn: sqlite3.Connection, status: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT fr.*, 
                   u.username, u.display_name,
                   a.account_number,
                   reviewer.username AS reviewed_by_username,
                   fulfiller.username AS fulfilled_by_username
            FROM fund_requests fr
            JOIN users u ON u.id = fr.user_id
            JOIN accounts a ON a.id = fr.account_id
            LEFT JOIN users reviewer ON reviewer.id = fr.reviewed_by
            LEFT JOIN users fulfiller ON fulfiller.id = fr.fulfilled_by
        """
        params: list[Any] = []
        if status:
            query += " WHERE fr.status = ?"
            params.append(status)
        query += " ORDER BY fr.created_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def update_status(
        self,
        conn: sqlite3.Connection,
        request_id: int,
        status: str,
        reviewed_by: int | None = None,
        fulfilled_by: int | None = None,
        transaction_id: int | None = None,
        now: str | None = None,
    ) -> None:
        now = now or ""
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, now]
        
        if reviewed_by is not None:
            updates.append("reviewed_by = ?")
            updates.append("reviewed_at = ?")
            params.extend([reviewed_by, now])
        if fulfilled_by is not None:
            updates.append("fulfilled_by = ?")
            updates.append("fulfilled_at = ?")
            params.extend([fulfilled_by, now])
        if transaction_id is not None:
            updates.append("transaction_id = ?")
            params.append(transaction_id)
        
        params.append(request_id)
        conn.execute(
            f"UPDATE fund_requests SET {', '.join(updates)} WHERE id = ?",
            params,
        )
