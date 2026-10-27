"""Versioned SQLite schema for the multi-user GAUR BANK simulator.

Money is stored as integer paise.  Completed ledger entries and audit records
are protected by append-only triggers; account balances are only changed by
the banking service inside explicit transactions.
"""

from __future__ import annotations

LATEST_SCHEMA_VERSION = 2

MIGRATIONS: list[tuple[int, tuple[str, ...]]] = [
    (
        1,
        (
            """
            CREATE TABLE users (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                username        TEXT NOT NULL COLLATE NOCASE UNIQUE,
                email           TEXT NOT NULL COLLATE NOCASE UNIQUE,
                display_name    TEXT NOT NULL,
                password_hash   TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'suspended')),
                failed_attempts INTEGER NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
                locked_until    TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                last_login_at   TEXT
            )
            """,
            """
            CREATE TABLE accounts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                account_number  TEXT NOT NULL UNIQUE,
                user_id         INTEGER NOT NULL UNIQUE
                                REFERENCES users(id) ON DELETE RESTRICT,
                account_type    TEXT NOT NULL DEFAULT 'savings'
                                CHECK (account_type IN ('savings', 'current')),
                balance_paise   INTEGER NOT NULL DEFAULT 0
                                CHECK (balance_paise >= 0),
                status          TEXT NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'frozen', 'closed')),
                currency        TEXT NOT NULL DEFAULT 'INR'
                                CHECK (currency = 'INR'),
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            )
            """,
            "CREATE INDEX idx_accounts_user ON accounts(user_id)",
            "CREATE INDEX idx_accounts_status ON accounts(status)",
            """
            CREATE TABLE transfers (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                reference           TEXT NOT NULL UNIQUE,
                from_account_id     INTEGER NOT NULL
                                    REFERENCES accounts(id) ON DELETE RESTRICT,
                to_account_id       INTEGER NOT NULL
                                    REFERENCES accounts(id) ON DELETE RESTRICT,
                initiated_by_user_id INTEGER NOT NULL
                                    REFERENCES users(id) ON DELETE RESTRICT,
                amount_paise        INTEGER NOT NULL CHECK (amount_paise > 0),
                description         TEXT,
                idempotency_key     TEXT NOT NULL UNIQUE,
                created_at          TEXT NOT NULL,
                CHECK (from_account_id <> to_account_id)
            )
            """,
            "CREATE INDEX idx_transfers_from ON transfers(from_account_id)",
            "CREATE INDEX idx_transfers_to ON transfers(to_account_id)",
            """
            CREATE TABLE transactions (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                reference               TEXT NOT NULL UNIQUE,
                account_id              INTEGER NOT NULL
                                        REFERENCES accounts(id) ON DELETE RESTRICT,
                counterparty_account_id INTEGER
                                        REFERENCES accounts(id) ON DELETE RESTRICT,
                type                    TEXT NOT NULL CHECK (type IN (
                                            'deposit', 'withdrawal',
                                            'transfer_debit', 'transfer_credit')),
                direction               TEXT NOT NULL CHECK (direction IN ('credit', 'debit')),
                amount_paise            INTEGER NOT NULL CHECK (amount_paise > 0),
                balance_before_paise    INTEGER NOT NULL CHECK (balance_before_paise >= 0),
                balance_after_paise     INTEGER NOT NULL CHECK (balance_after_paise >= 0),
                status                  TEXT NOT NULL DEFAULT 'success'
                                        CHECK (status IN ('success', 'failed')),
                description             TEXT,
                transfer_id             INTEGER REFERENCES transfers(id) ON DELETE RESTRICT,
                idempotency_key         TEXT UNIQUE,
                counts_toward_limit     INTEGER NOT NULL DEFAULT 1
                                        CHECK (counts_toward_limit IN (0, 1)),
                created_at              TEXT NOT NULL,
                CHECK (status <> 'failed' OR balance_before_paise = balance_after_paise),
                CHECK (
                    status <> 'success' OR direction <> 'credit'
                    OR balance_after_paise = balance_before_paise + amount_paise
                ),
                CHECK (
                    status <> 'success' OR direction <> 'debit'
                    OR balance_after_paise = balance_before_paise - amount_paise
                )
            )
            """,
            "CREATE INDEX idx_transactions_account ON transactions(account_id, created_at)",
            "CREATE INDEX idx_transactions_created ON transactions(created_at)",
            "CREATE INDEX idx_transactions_type ON transactions(type)",
            "CREATE INDEX idx_transactions_status ON transactions(status)",
            "CREATE INDEX idx_transactions_transfer ON transactions(transfer_id)",
            """
            CREATE TABLE sessions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token_hash   TEXT NOT NULL UNIQUE,
                csrf_token   TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                expires_at   TEXT NOT NULL,
                revoked_at   TEXT,
                ip_address   TEXT,
                user_agent   TEXT
            )
            """,
            "CREATE INDEX idx_sessions_user ON sessions(user_id)",
            "CREATE INDEX idx_sessions_expiry ON sessions(expires_at)",
            """
            CREATE TABLE audit_logs (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at     TEXT NOT NULL,
                actor_user_id  INTEGER,
                actor_username TEXT NOT NULL,
                action         TEXT NOT NULL,
                entity_type    TEXT,
                entity_id      TEXT,
                outcome        TEXT NOT NULL CHECK (outcome IN ('success', 'failure')),
                details        TEXT
            )
            """,
            "CREATE INDEX idx_audit_created ON audit_logs(created_at)",
            "CREATE INDEX idx_audit_action ON audit_logs(action)",
            "CREATE INDEX idx_audit_actor ON audit_logs(actor_user_id)",
            """
            CREATE TABLE app_settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TRIGGER trg_transactions_no_update
            BEFORE UPDATE ON transactions
            BEGIN
                SELECT RAISE(ABORT, 'ledger entries are append-only');
            END
            """,
            """
            CREATE TRIGGER trg_transactions_no_delete
            BEFORE DELETE ON transactions
            BEGIN
                SELECT RAISE(ABORT, 'ledger entries are append-only');
            END
            """,
            """
            CREATE TRIGGER trg_audit_no_update
            BEFORE UPDATE ON audit_logs
            BEGIN
                SELECT RAISE(ABORT, 'audit log is append-only');
            END
            """,
            """
            CREATE TRIGGER trg_audit_no_delete
            BEFORE DELETE ON audit_logs
            BEGIN
                SELECT RAISE(ABORT, 'audit log is append-only');
            END
            """,
        ),
    ),
    (
        2,
        (
            # External transfers have no GAUR BANK counterparty account, so the
            # ledger keeps the recipient label and account number directly.
            "ALTER TABLE transactions ADD COLUMN counterparty_label TEXT",
            "ALTER TABLE transactions ADD COLUMN counterparty_external_number TEXT",
        ),
    ),
]

DEFAULT_SETTINGS = {
    "next_account_number": "200300400001",
    "theme": "private",
    "bank_name": "GAUR BANK",
}
