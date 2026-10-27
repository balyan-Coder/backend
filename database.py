"""SQLite access with WAL, foreign keys, migrations and explicit transactions."""

from __future__ import annotations

import logging
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.schema import DEFAULT_SETTINGS, LATEST_SCHEMA_VERSION, MIGRATIONS

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ setup
    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version    INTEGER NOT NULL PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                int(row["version"])
                for row in conn.execute("SELECT version FROM schema_version").fetchall()
            }
            pending = [(v, statements) for v, statements in MIGRATIONS if v not in applied]
            if pending and self.path.exists() and self.path.stat().st_size > 0 and applied:
                self.backup_before_migration()
            for version, statements in pending:
                logger.info("Applying schema migration %s", version)
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    for statement in statements:
                        conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                        (version, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    logger.exception("Migration %s failed", version)
                    raise
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
            conn.execute("PRAGMA user_version = %d" % LATEST_SCHEMA_VERSION)

    def backup_before_migration(self) -> Path:
        backup_dir = self.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = backup_dir / f"pre_migration_{stamp}.db"
        self.backup_to(target)
        logger.info("Pre-migration backup written to %s", target)
        return target

    # -------------------------------------------------------------- connections
    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    logger.exception("Rollback failed")
                raise

    # ------------------------------------------------------------------ backup
    def backup_to(self, target: Path) -> Path:
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(target)
        with sqlite3.connect(self.path, timeout=30) as source:
            with sqlite3.connect(target) as destination:
                source.backup(destination)
        return target

    def validate_file(self, path: Path) -> tuple[bool, str]:
        path = Path(path)
        if not path.is_file() or path.stat().st_size == 0:
            return False, "The backup file is empty or missing."
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                if not row or row[0] != "ok":
                    return False, "SQLite integrity check failed."
                tables = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                required = {"schema_version", "users", "accounts", "transactions"}
                if not required.issubset(tables):
                    return False, "This file is not a GAUR BANK database."
                version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
                if version is None or int(version) > LATEST_SCHEMA_VERSION:
                    return False, "The backup schema is not supported by this version."
                return True, "Backup is valid."
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.warning("Invalid backup rejected: %s", exc)
            return False, "The file could not be read as a SQLite database."

    def restore_from(self, source: Path) -> None:
        valid, message = self.validate_file(source)
        if not valid:
            raise ValueError(message)
        backup_dir = self.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safety = backup_dir / f"pre_restore_{stamp}.db"
        if self.path.exists():
            self.backup_to(safety)
        temporary = self.path.with_suffix(".restore.tmp")
        shutil.copy2(source, temporary)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.path) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        temporary.replace(self.path)
        self.initialize()

    def info(self) -> dict:
        size = self.path.stat().st_size if self.path.exists() else 0
        with self.connection() as conn:
            version = conn.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()[0]
        return {"path": str(self.path), "size_bytes": size, "schema_version": version}
