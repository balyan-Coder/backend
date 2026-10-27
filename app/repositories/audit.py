"""Append-only audit log persistence."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


class AuditRepository:
    def log(
        self,
        conn: sqlite3.Connection,
        *,
        created_at: str,
        actor_user_id: int | None,
        actor_username: str,
        action: str,
        outcome: str,
        entity_type: str | None = None,
        entity_id: str | int | None = None,
        details: dict[str, Any] | None = None,
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO audit_logs(
                created_at, actor_user_id, actor_username, action,
                entity_type, entity_id, outcome, details
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                actor_user_id,
                actor_username or "anonymous",
                action,
                entity_type,
                str(entity_id) if entity_id is not None else None,
                outcome,
                json.dumps(details, separators=(",", ":")) if details else None,
            ),
        )
        return int(cursor.lastrowid)

    def list(
        self,
        conn: sqlite3.Connection,
        *,
        page: int,
        page_size: int,
        action: str = "",
        outcome: str = "all",
        search: str = "",
    ) -> tuple[list[dict[str, Any]], int]:
        where = ["1 = 1"]
        params: list[Any] = []
        if action:
            where.append("action = ?")
            params.append(action)
        if outcome and outcome != "all":
            where.append("outcome = ?")
            params.append(outcome)
        if search:
            like = f"%{search.strip()}%"
            where.append("(actor_username LIKE ? OR action LIKE ? OR details LIKE ?)")
            params.extend([like, like, like])
        clause = " WHERE " + " AND ".join(where)
        total = int(
            conn.execute("SELECT COUNT(*) FROM audit_logs" + clause, params).fetchone()[0]
        )
        offset = (max(1, page) - 1) * page_size
        rows = conn.execute(
            "SELECT * FROM audit_logs"
            + clause
            + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()
        return [dict(row) for row in rows], total
