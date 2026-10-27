"""Key/value application settings."""

from __future__ import annotations

import sqlite3


class SettingsRepository:
    def get(self, conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, conn: sqlite3.Connection, key: str, value: str, now: str) -> None:
        conn.execute(
            """
            INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now),
        )

    def all(self, conn: sqlite3.Connection) -> dict[str, str]:
        return {row[0]: row[1] for row in conn.execute("SELECT key, value FROM app_settings")}

    def next_account_number(self, conn: sqlite3.Connection, now: str) -> str:
        current = self.get(conn, "next_account_number", "200300400001")
        try:
            number = int(current or "200300400001")
        except ValueError:
            number = 200300400001
        self.set(conn, "next_account_number", str(number + 1), now)
        return f"{number:012d}"
