"""Simulated banking operations with atomic ledger updates.

Every successful movement is a single SQLite transaction: conditional account
updates plus append-only ledger rows.  Failed attempts are recorded separately
without changing balances.  Transfer idempotency prevents double-spend on retry.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.config import Settings
from app.database import Database
from app.errors import BankingError, ConflictError, NotFoundError, PermissionError, ValidationError
from app.repositories.accounts import AccountRepository
from app.repositories.audit import AuditRepository
from app.repositories.fund_requests import FundRequestRepository
from app.repositories.settings import SettingsRepository
from app.repositories.transactions import TransactionRepository
from app.repositories.transfers import TransferRepository
from app.utils.money import (
    decimal_to_paise,
    mask_account,
    new_reference,
    paise_to_string,
)
from app.utils.time import (
    end_of_day,
    iso,
    start_of_day,
    utcnow,
)
from app.utils.validation import (
    clean_text,
    validate_account_number,
    validate_description,
)

logger = logging.getLogger(__name__)


@dataclass
class OperationOutcome:
    receipt: dict[str, Any]
    notifications: list[tuple[int, dict[str, Any]]]


class BankingService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.accounts = AccountRepository()
        self.transactions = TransactionRepository()
        self.transfers = TransferRepository()
        self.audit = AuditRepository()
        self.app_settings = SettingsRepository()
        self.fund_requests = FundRequestRepository()

    # ------------------------------------------------------------------ helpers
    def _own_account(self, conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
        account = self.accounts.get_by_user_id(conn, user_id)
        if account is None:
            raise NotFoundError("No bank account is linked to this profile.")
        return account

    @staticmethod
    def public_account(account: dict[str, Any]) -> dict[str, Any]:
        number = str(account["account_number"])
        return {
            "id": int(account["id"]),
            "account_number": number,
            "masked_account_number": mask_account(number),
            "account_type": str(account["account_type"]),
            "balance_paise": int(account["balance_paise"]),
            "status": str(account["status"]),
            "currency": str(account["currency"]),
            "created_at": str(account["created_at"]),
        }

    @staticmethod
    def public_transaction(row: dict[str, Any]) -> dict[str, Any]:
        counterparty = row.get("counterparty_account_number") or row.get(
            "counterparty_external_number"
        )
        counterparty_name = row.get("counterparty_name") or row.get("counterparty_label")
        return {
            "id": int(row["id"]),
            "reference": str(row["reference"]),
            "type": str(row["type"]),
            "direction": str(row["direction"]),
            "amount_paise": int(row["amount_paise"]),
            "balance_before_paise": int(row["balance_before_paise"]),
            "balance_after_paise": int(row["balance_after_paise"]),
            "status": str(row["status"]),
            "description": row.get("description"),
            "transfer_reference": row.get("transfer_reference"),
            "counterparty_name": counterparty_name,
            "counterparty_account_number": mask_account(str(counterparty)) if counterparty else None,
            "created_at": str(row["created_at"]),
        }

    @staticmethod
    def _receipt(
        *,
        reference: str,
        type_: str,
        direction: str,
        amount_paise: int,
        balance_paise: int,
        description: str | None,
        counterparty_name: str | None,
        counterparty_account: str | None,
        created_at: str,
        duplicate: bool = False,
        external: bool = False,
    ) -> dict[str, Any]:
        return {
            "reference": reference,
            "type": type_,
            "direction": direction,
            "amount_paise": amount_paise,
            "balance_paise": balance_paise,
            "description": description,
            "counterparty_name": counterparty_name,
            "counterparty_masked_account": mask_account(counterparty_account)
            if counterparty_account
            else None,
            "created_at": created_at,
            "duplicate": duplicate,
            "external": external,
        }

    @staticmethod
    def _event(
        *,
        transaction_id: int,
        reference: str,
        type_: str,
        direction: str,
        amount_paise: int,
        balance_paise: int,
        counterparty_name: str | None,
        description: str | None,
        created_at: str,
    ) -> dict[str, Any]:
        return {
            "type": "transaction",
            "event_id": reference,
            "transaction_id": transaction_id,
            "transaction_type": type_,
            "direction": direction,
            "amount_paise": amount_paise,
            "balance_paise": balance_paise,
            "reference": reference,
            "counterparty_name": counterparty_name,
            "description": description,
            "created_at": created_at,
        }

    def _record_failed(
        self,
        *,
        user_id: int,
        account_id: int,
        type_: str,
        direction: str,
        amount_paise: int,
        description: str | None,
        counterparty_account_id: int | None = None,
    ) -> None:
        """Best-effort append-only record of a declined operation."""
        try:
            now = iso()
            with self.db.transaction() as conn:
                account = self.accounts.get_by_id(conn, account_id)
                if account is None:
                    return
                balance = int(account["balance_paise"])
                username_row = conn.execute(
                    "SELECT username FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                self.transactions.insert(
                    conn,
                    reference=new_reference("TXN"),
                    account_id=account_id,
                    counterparty_account_id=counterparty_account_id,
                    type_=type_,
                    direction=direction,
                    amount_paise=amount_paise,
                    balance_before_paise=balance,
                    balance_after_paise=balance,
                    status="failed",
                    description=description,
                    transfer_id=None,
                    idempotency_key=None,
                    created_at=now,
                )
                self.audit.log(
                    conn,
                    created_at=now,
                    actor_user_id=user_id,
                    actor_username=str(username_row["username"]) if username_row else str(user_id),
                    action=f"bank.{type_}.failed",
                    outcome="failure",
                    entity_type="account",
                    entity_id=account_id,
                    details={"amount_paise": amount_paise},
                )
        except Exception:  # noqa: BLE001
            logger.exception("Could not record failed %s attempt", type_)

    # ------------------------------------------------------------- resolution
    def resolve_recipient(self, *, user_id: int, account_number: str) -> dict[str, Any]:
        number = validate_account_number(account_number)
        with self.db.connection() as conn:
            own = self.accounts.get_by_user_id(conn, user_id)
            if own and str(own["account_number"]) == number:
                raise ValidationError(
                    "You cannot transfer to your own account.", field="account_number"
                )
            account = self.accounts.get_by_number(conn, number)
            if account is None or str(account["status"]) != "active":
                raise NotFoundError("No active GAUR BANK account matches that number.")
            user = conn.execute(
                "SELECT display_name FROM users WHERE id = ?", (account["user_id"],)
            ).fetchone()
        return {
            "display_name": str(user["display_name"]) if user else "GAUR BANK customer",
            "masked_account_number": mask_account(number),
        }

    # --------------------------------------------------------------- dashboard
    def dashboard(self, *, user_id: int, period: str = "30d") -> dict[str, Any]:
        allowed = {"7d", "30d", "90d", "365d"}
        period = period if period in allowed else "30d"
        days = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}[period]
        since = start_of_day(date.fromordinal(utcnow().date().toordinal() - days + 1))
        with self.db.connection() as conn:
            account = self._own_account(conn, user_id)
            display_name = self._display_name(conn, user_id)
            is_owner = self._is_owner(conn, user_id)
            totals = self.transactions.totals(conn, int(account["id"]))
            recent = self.transactions.recent(conn, int(account["id"]), limit=8)
            monthly = self.transactions.monthly_cashflow(conn, int(account["id"]), since)
            daily = self.transactions.daily_activity(conn, int(account["id"]), since)
            failed_recent = conn.execute(
                """
                SELECT COUNT(*) FROM transactions
                WHERE account_id = ? AND status = 'failed'
                  AND created_at >= ?
                """,
                (int(account["id"]), since),
            ).fetchone()[0]
        account_public = self.public_account(account)
        alerts: list[dict[str, Any]] = []
        if account_public["status"] == "frozen":
            alerts.append(
                {
                    "tone": "warning",
                    "title": "Account frozen",
                    "detail": "Deposits, withdrawals and transfers are disabled.",
                }
            )
        if int(failed_recent) > 0:
            alerts.append(
                {
                    "tone": "info",
                    "title": "Declined attempts",
                    "detail": f"{failed_recent} recent operation(s) were declined by banking rules.",
                }
            )
        if not alerts:
            alerts.append(
                {
                    "tone": "success",
                    "title": "Account in good standing",
                    "detail": "No operational alerts for this period.",
                }
            )
        return {
            "user": {
                "id": user_id,
                "display_name": display_name,
                "is_owner": is_owner,
            },
            "account": account_public,
            "totals": {
                "deposits_paise": int(totals["deposits_paise"]),
                "withdrawals_paise": int(totals["withdrawals_paise"]),
                "transfers_in_paise": int(totals["transfers_in_paise"]),
                "transfers_out_paise": int(totals["transfers_out_paise"]),
                "successful_count": int(totals["successful_count"]),
                "failed_count": int(totals["failed_count"]),
            },
            "recent_transactions": [self.public_transaction(row) for row in recent],
            "cashflow": self._fill_cashflow(monthly, months=6),
            "activity": self._fill_activity(daily, days=14),
            "alerts": alerts,
            "period": period,
            "limits": {
                # The owner account has no per-operation or daily funding cap.
                # ``None`` means unlimited; the UI must not format it as money.
                "transaction_limit_paise": (
                    None if is_owner else self.settings.transaction_limit_paise
                ),
                "daily_deposit_limit_paise": (
                    None if is_owner else self.settings.daily_deposit_limit_paise
                ),
                "unlimited": is_owner,
                "allow_demo_deposits": self.settings.allow_demo_deposits,
            },
            "demo_policy": {
                "starting_balance_paise": (
                    self.settings.owner_starting_balance_paise
                    if is_owner
                    else self.settings.demo_starting_balance_paise
                ),
                "is_owner": is_owner,
                "currency": "INR",
            },
        }

    @staticmethod
    def _display_name(conn: sqlite3.Connection, user_id: int) -> str:
        row = conn.execute("SELECT display_name FROM users WHERE id = ?", (user_id,)).fetchone()
        return str(row["display_name"]) if row else "Customer"

    def _is_owner(self, conn: sqlite3.Connection, user_id: int) -> bool:
        row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        return bool(row) and self.settings.is_owner_username(str(row["username"]))

    def _is_owner_user(self, user_id: int) -> bool:
        """Owner checks for pre-transaction limit validation.

        Usernames are immutable, so reading the owner flag on its own connection
        before the write transaction cannot race with a rename.
        """

        with self.db.connection() as conn:
            return self._is_owner(conn, user_id)

    @staticmethod
    def _fill_cashflow(rows: list[dict[str, Any]], *, months: int) -> list[dict[str, Any]]:
        by_month = {str(row["month"]): row for row in rows}
        today = utcnow().date()
        output: list[dict[str, Any]] = []
        for offset in range(months - 1, -1, -1):
            month_index = today.year * 12 + (today.month - 1) - offset
            year, month_zero = divmod(month_index, 12)
            key = f"{year:04d}-{month_zero + 1:02d}"
            row = by_month.get(key)
            output.append(
                {
                    "month": key,
                    "label": f"{['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][month_zero]} {str(year)[2:]}",
                    "deposits_paise": int(row["deposits_paise"]) if row else 0,
                    "withdrawals_paise": int(row["withdrawals_paise"]) if row else 0,
                }
            )
        return output

    @staticmethod
    def _fill_activity(rows: list[dict[str, Any]], *, days: int) -> list[dict[str, Any]]:
        by_day = {str(row["day"]): row for row in rows}
        today = utcnow().date()
        output: list[dict[str, Any]] = []
        for offset in range(days - 1, -1, -1):
            day = date.fromordinal(today.toordinal() - offset)
            key = day.isoformat()
            row = by_day.get(key)
            output.append(
                {
                    "day": key,
                    "label": day.strftime("%d %b"),
                    "total": int(row["total"]) if row else 0,
                    "credits": int(row["credits"]) if row else 0,
                    "debits": int(row["debits"]) if row else 0,
                    "volume_paise": int(row["volume_paise"]) if row else 0,
                }
            )
        return output

    # --------------------------------------------------------------- deposits
    def deposit(
        self,
        *,
        user_id: int,
        amount: Decimal,
        description: str | None,
        idempotency_key: str | None,
    ) -> OperationOutcome:
        if not self.settings.allow_demo_deposits:
            raise BankingError(
                "Simulated deposits are disabled on this deployment.",
                code="deposits_disabled",
            )
        amount_paise = decimal_to_paise(amount)
        is_owner = self._is_owner_user(user_id)
        if not is_owner and amount_paise > self.settings.transaction_limit_paise:
            raise BankingError(
                f"Amount exceeds the per-operation limit of "
                f"₹{paise_to_string(self.settings.transaction_limit_paise)}.",
                code="limit_exceeded",
                field="amount",
            )
        description = validate_description(description)
        if idempotency_key:
            duplicate = self._duplicate_deposit(
                user_id=user_id, key=idempotency_key, expected_type="deposit"
            )
            if duplicate:
                return duplicate
        account_id: int | None = None
        try:
            now = iso()
            reference = new_reference("TXN")
            with self.db.transaction() as conn:
                account = self._own_account(conn, user_id)
                account_id = int(account["id"])
                username_row = conn.execute(
                    "SELECT username FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                username = str(username_row["username"]) if username_row else str(user_id)
                if str(account["status"]) != "active":
                    raise BankingError(
                        "This account is not active. Transactions are disabled.",
                        code="account_inactive",
                    )
                today_total = self.transactions.successful_deposits_since(
                    conn, account_id, start_of_day()
                )
                if not is_owner and today_total + amount_paise > self.settings.daily_deposit_limit_paise:
                    raise BankingError(
                        "This deposit would exceed today's simulated funding limit.",
                        code="daily_limit_exceeded",
                        field="amount",
                    )
                before = int(account["balance_paise"])
                if not self.accounts.credit(conn, account_id, amount_paise, now):
                    raise BankingError("The deposit could not be completed.", code="deposit_failed")
                after = before + amount_paise
                transaction_id = self.transactions.insert(
                    conn,
                    reference=reference,
                    account_id=account_id,
                    counterparty_account_id=None,
                    type_="deposit",
                    direction="credit",
                    amount_paise=amount_paise,
                    balance_before_paise=before,
                    balance_after_paise=after,
                    status="success",
                    description=description or "Simulated deposit",
                    transfer_id=None,
                    idempotency_key=idempotency_key or None,
                    created_at=now,
                )
                self.audit.log(
                    conn,
                    created_at=now,
                    actor_user_id=user_id,
                    actor_username=username,
                    action="bank.deposit",
                    outcome="success",
                    entity_type="transaction",
                    entity_id=transaction_id,
                    details={"amount_paise": amount_paise},
                )
            receipt = self._receipt(
                reference=reference,
                type_="deposit",
                direction="credit",
                amount_paise=amount_paise,
                balance_paise=after,
                description=description or "Simulated deposit",
                counterparty_name=None,
                counterparty_account=None,
                created_at=now,
            )
            event = self._event(
                transaction_id=transaction_id,
                reference=reference,
                type_="deposit",
                direction="credit",
                amount_paise=amount_paise,
                balance_paise=after,
                counterparty_name=None,
                description=description or "Simulated deposit",
                created_at=now,
            )
            return OperationOutcome(receipt=dict(receipt), notifications=[(user_id, event)])
        except sqlite3.IntegrityError:
            if idempotency_key:
                duplicate = self._duplicate_deposit(
                    user_id=user_id, key=idempotency_key, expected_type="deposit"
                )
                if duplicate:
                    return duplicate
            raise
        except BankingError:
            if account_id is not None:
                self._record_failed(
                    user_id=user_id,
                    account_id=account_id,
                    type_="deposit",
                    direction="credit",
                    amount_paise=amount_paise,
                    description=description or "Simulated deposit",
                )
            raise

    def _duplicate_deposit(
        self, *, user_id: int, key: str, expected_type: str
    ) -> OperationOutcome | None:
        with self.db.connection() as conn:
            existing = self.transactions.get_by_idempotency(conn, key)
            if existing is None:
                return None
            account = self._own_account(conn, user_id)
            if int(existing["account_id"]) != int(account["id"]):
                raise ConflictError("That request key is already in use.")
            if str(existing["type"]) != expected_type:
                raise ConflictError(
                    "That request key was already used for a different operation.",
                    field="idempotency_key",
                )
            if str(existing["status"]) != "success":
                return None
            receipt = self._receipt(
                reference=str(existing["reference"]),
                type_=str(existing["type"]),
                direction=str(existing["direction"]),
                amount_paise=int(existing["amount_paise"]),
                balance_paise=int(account["balance_paise"]),
                description=existing.get("description"),
                counterparty_name=None,
                counterparty_account=None,
                created_at=str(existing["created_at"]),
                duplicate=True,
            )
            return OperationOutcome(receipt=receipt, notifications=[])

    # ------------------------------------------------------------- withdrawals
    def withdraw(
        self,
        *,
        user_id: int,
        amount: Decimal,
        description: str | None,
        idempotency_key: str | None,
    ) -> OperationOutcome:
        amount_paise = decimal_to_paise(amount)
        if not self._is_owner_user(user_id) and amount_paise > self.settings.transaction_limit_paise:
            raise BankingError(
                f"Amount exceeds the per-operation limit of "
                f"₹{paise_to_string(self.settings.transaction_limit_paise)}.",
                code="limit_exceeded",
                field="amount",
            )
        description = validate_description(description)
        if idempotency_key:
            duplicate = self._duplicate_deposit(
                user_id=user_id, key=idempotency_key, expected_type="withdrawal"
            )
            if duplicate:
                return duplicate
        account_id: int | None = None
        try:
            now = iso()
            reference = new_reference("TXN")
            with self.db.transaction() as conn:
                account = self._own_account(conn, user_id)
                account_id = int(account["id"])
                username_row = conn.execute(
                    "SELECT username FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                username = str(username_row["username"]) if username_row else str(user_id)
                if str(account["status"]) != "active":
                    raise BankingError(
                        "This account is not active. Transactions are disabled.",
                        code="account_inactive",
                    )
                before = int(account["balance_paise"])
                if before < amount_paise:
                    raise BankingError(
                        "Insufficient available balance for this withdrawal.",
                        code="insufficient_funds",
                        field="amount",
                    )
                if not self.accounts.debit(conn, account_id, amount_paise, now):
                    raise BankingError(
                        "Insufficient available balance for this withdrawal.",
                        code="insufficient_funds",
                        field="amount",
                    )
                after = before - amount_paise
                transaction_id = self.transactions.insert(
                    conn,
                    reference=reference,
                    account_id=account_id,
                    counterparty_account_id=None,
                    type_="withdrawal",
                    direction="debit",
                    amount_paise=amount_paise,
                    balance_before_paise=before,
                    balance_after_paise=after,
                    status="success",
                    description=description or "Simulated withdrawal",
                    transfer_id=None,
                    idempotency_key=idempotency_key or None,
                    created_at=now,
                )
                self.audit.log(
                    conn,
                    created_at=now,
                    actor_user_id=user_id,
                    actor_username=username,
                    action="bank.withdrawal",
                    outcome="success",
                    entity_type="transaction",
                    entity_id=transaction_id,
                    details={"amount_paise": amount_paise},
                )
            receipt = self._receipt(
                reference=reference,
                type_="withdrawal",
                direction="debit",
                amount_paise=amount_paise,
                balance_paise=after,
                description=description or "Simulated withdrawal",
                counterparty_name=None,
                counterparty_account=None,
                created_at=now,
            )
            event = self._event(
                transaction_id=transaction_id,
                reference=reference,
                type_="withdrawal",
                direction="debit",
                amount_paise=amount_paise,
                balance_paise=after,
                counterparty_name=None,
                description=description or "Simulated withdrawal",
                created_at=now,
            )
            return OperationOutcome(receipt=receipt, notifications=[(user_id, event)])
        except sqlite3.IntegrityError:
            if idempotency_key:
                duplicate = self._duplicate_deposit(
                    user_id=user_id, key=idempotency_key, expected_type="withdrawal"
                )
                if duplicate:
                    return duplicate
            raise
        except BankingError:
            if account_id is not None:
                self._record_failed(
                    user_id=user_id,
                    account_id=account_id,
                    type_="withdrawal",
                    direction="debit",
                    amount_paise=amount_paise,
                    description=description or "Simulated withdrawal",
                )
            raise

    # --------------------------------------------------------------- transfers
    def transfer(
        self,
        *,
        user_id: int,
        recipient_account_number: str,
        amount: Decimal,
        description: str | None,
        idempotency_key: str,
        external: bool = False,
        recipient_name: str | None = None,
    ) -> OperationOutcome:
        amount_paise = decimal_to_paise(amount)
        if not self._is_owner_user(user_id) and amount_paise > self.settings.transaction_limit_paise:
            raise BankingError(
                f"Amount exceeds the per-operation limit of "
                f"₹{paise_to_string(self.settings.transaction_limit_paise)}.",
                code="limit_exceeded",
                field="amount",
            )
        if len(idempotency_key) < 8:
            raise ValidationError("A valid idempotency key is required.", field="idempotency_key")
        recipient_number = validate_account_number(recipient_account_number)
        description = validate_description(description)
        if external:
            return self._external_transfer(
                user_id=user_id,
                recipient_number=recipient_number,
                recipient_name=recipient_name,
                amount_paise=amount_paise,
                description=description,
                idempotency_key=idempotency_key,
            )
        existing = self._duplicate_transfer(user_id, idempotency_key)
        if existing:
            return existing

        now = iso()
        reference = new_reference("TRF")
        sender_account_id: int | None = None
        recipient_account_id: int | None = None
        try:
            with self.db.transaction() as conn:
                duplicate = self.transfers.get_by_idempotency(conn, idempotency_key)
                if duplicate:
                    # Another request committed while we waited for the lock.
                    return self._duplicate_transfer(user_id, idempotency_key) or self._outcome_from_transfer(
                        conn, duplicate, duplicate=True
                    )
                sender = self._own_account(conn, user_id)
                sender_account_id = int(sender["id"])
                if str(sender["status"]) != "active":
                    raise BankingError(
                        "Your account is not active. Transfers are disabled.",
                        code="account_inactive",
                    )
                recipient = self.accounts.get_by_number(conn, recipient_number)
                if recipient is None:
                    raise BankingError(
                        "No GAUR BANK account matches that number.",
                        code="recipient_not_found",
                        field="recipient_account_number",
                    )
                recipient_account_id = int(recipient["id"])
                if recipient_account_id == sender_account_id:
                    raise BankingError(
                        "You cannot transfer to your own account.",
                        code="self_transfer",
                        field="recipient_account_number",
                    )
                if str(recipient["status"]) != "active":
                    raise BankingError(
                        "The recipient account is not available for transfers.",
                        code="recipient_inactive",
                        field="recipient_account_number",
                    )
                if str(recipient["currency"]) != str(sender["currency"]):
                    raise BankingError(
                        "Both accounts must use the same currency.", code="currency_mismatch"
                    )
                sender_before = int(sender["balance_paise"])
                recipient_before = int(recipient["balance_paise"])
                if sender_before < amount_paise:
                    raise BankingError(
                        "Insufficient available balance for this transfer.",
                        code="insufficient_funds",
                        field="amount",
                    )
                if not self.accounts.debit(conn, sender_account_id, amount_paise, now):
                    raise BankingError(
                        "Insufficient available balance for this transfer.",
                        code="insufficient_funds",
                        field="amount",
                    )
                if not self.accounts.credit(conn, recipient_account_id, amount_paise, now):
                    raise BankingError(
                        "The recipient account could not receive this transfer.",
                        code="recipient_inactive",
                    )
                transfer_id = self.transfers.create(
                    conn,
                    reference=reference,
                    from_account_id=sender_account_id,
                    to_account_id=recipient_account_id,
                    initiated_by_user_id=user_id,
                    amount_paise=amount_paise,
                    description=description or "Simulated internal transfer",
                    idempotency_key=idempotency_key,
                    created_at=now,
                )
                sender_user = conn.execute(
                    "SELECT display_name FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                recipient_user_id = int(recipient["user_id"])
                recipient_user = conn.execute(
                    "SELECT display_name FROM users WHERE id = ?", (recipient_user_id,)
                ).fetchone()
                sender_after = sender_before - amount_paise
                recipient_after = recipient_before + amount_paise
                debit_txn_id = self.transactions.insert(
                    conn,
                    reference=new_reference("TXN"),
                    account_id=sender_account_id,
                    counterparty_account_id=recipient_account_id,
                    type_="transfer_debit",
                    direction="debit",
                    amount_paise=amount_paise,
                    balance_before_paise=sender_before,
                    balance_after_paise=sender_after,
                    status="success",
                    description=description or f"Transfer to {recipient_number[-4:]}",
                    transfer_id=transfer_id,
                    idempotency_key=None,
                    created_at=now,
                )
                credit_txn_id = self.transactions.insert(
                    conn,
                    reference=new_reference("TXN"),
                    account_id=recipient_account_id,
                    counterparty_account_id=sender_account_id,
                    type_="transfer_credit",
                    direction="credit",
                    amount_paise=amount_paise,
                    balance_before_paise=recipient_before,
                    balance_after_paise=recipient_after,
                    status="success",
                    description=description or f"Transfer from {str(sender['account_number'])[-4:]}",
                    transfer_id=transfer_id,
                    idempotency_key=None,
                    created_at=now,
                )
                self.audit.log(
                    conn,
                    created_at=now,
                    actor_user_id=user_id,
                    actor_username=str(sender_user["display_name"]) if sender_user else str(user_id),
                    action="bank.transfer",
                    outcome="success",
                    entity_type="transfer",
                    entity_id=transfer_id,
                    details={
                        "amount_paise": amount_paise,
                        "recipient_user_id": recipient_user_id,
                        "from_masked": mask_account(str(sender["account_number"])),
                        "to_masked": mask_account(recipient_number),
                    },
                )
                sender_name = str(sender_user["display_name"]) if sender_user else "GAUR BANK customer"
                recipient_name = (
                    str(recipient_user["display_name"]) if recipient_user else "GAUR BANK customer"
                )
            receipt = self._receipt(
                reference=reference,
                type_="transfer_debit",
                direction="debit",
                amount_paise=amount_paise,
                balance_paise=sender_after,
                description=description or "Simulated internal transfer",
                counterparty_name=recipient_name,
                counterparty_account=recipient_number,
                created_at=now,
            )
            sender_event = self._event(
                transaction_id=debit_txn_id,
                reference=reference,
                type_="transfer_debit",
                direction="debit",
                amount_paise=amount_paise,
                balance_paise=sender_after,
                counterparty_name=recipient_name,
                description=description or "Simulated internal transfer",
                created_at=now,
            )
            recipient_event = self._event(
                transaction_id=credit_txn_id,
                reference=reference,
                type_="transfer_credit",
                direction="credit",
                amount_paise=amount_paise,
                balance_paise=recipient_after,
                counterparty_name=sender_name,
                description=description or "Simulated internal transfer",
                created_at=now,
            )
            return OperationOutcome(
                receipt=receipt,
                notifications=[(user_id, sender_event), (recipient_user_id, recipient_event)],
            )
        except sqlite3.IntegrityError as exc:
            duplicate = self._duplicate_transfer(user_id, idempotency_key)
            if duplicate:
                return duplicate
            logger.warning("Transfer integrity error: %s", exc)
            raise ConflictError("The transfer could not be completed.") from exc
        except BankingError:
            if sender_account_id is not None:
                self._record_failed(
                    user_id=user_id,
                    account_id=sender_account_id,
                    type_="transfer_debit",
                    direction="debit",
                    amount_paise=amount_paise,
                    description=description or "Simulated internal transfer",
                    counterparty_account_id=recipient_account_id,
                )
            raise

    # ------------------------------------------------------ external transfers
    def _external_transfer(
        self,
        *,
        user_id: int,
        recipient_number: str,
        recipient_name: str | None,
        amount_paise: int,
        description: str | None,
        idempotency_key: str,
    ) -> OperationOutcome:
        """Send simulated funds to an account outside GAUR BANK.

        Only the sender's account is debited: there is no recipient account in
        this database. The external name and number are stored on the ledger row
        so the transfer stays visible in history and idempotent on retry.
        """

        name = clean_text(recipient_name, max_length=60)
        if len(name) < 2:
            raise ValidationError(
                "Enter the external recipient's name.", field="recipient_name"
            )
        existing = self._duplicate_external_transfer(user_id, idempotency_key)
        if existing:
            return existing

        now = iso()
        reference = new_reference("TRF")
        sender_account_id: int | None = None
        try:
            with self.db.transaction() as conn:
                sender = self._own_account(conn, user_id)
                sender_account_id = int(sender["id"])
                if str(sender["status"]) != "active":
                    raise BankingError(
                        "Your account is not active. Transfers are disabled.",
                        code="account_inactive",
                    )
                if self.accounts.get_by_number(conn, recipient_number) is not None:
                    raise BankingError(
                        "That number belongs to a GAUR BANK account. Use the GAUR BANK transfer option instead.",
                        code="recipient_is_member",
                        field="recipient_account_number",
                    )
                before = int(sender["balance_paise"])
                if before < amount_paise:
                    raise BankingError(
                        "Insufficient available balance for this transfer.",
                        code="insufficient_funds",
                        field="amount",
                    )
                if not self.accounts.debit(conn, sender_account_id, amount_paise, now):
                    raise BankingError(
                        "Insufficient available balance for this transfer.",
                        code="insufficient_funds",
                        field="amount",
                    )
                after = before - amount_paise
                label = description or f"External transfer to {name}"
                sender_user = conn.execute(
                    "SELECT display_name FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                transaction_id = self.transactions.insert(
                    conn,
                    reference=reference,
                    account_id=sender_account_id,
                    counterparty_account_id=None,
                    type_="transfer_debit",
                    direction="debit",
                    amount_paise=amount_paise,
                    balance_before_paise=before,
                    balance_after_paise=after,
                    status="success",
                    description=label,
                    transfer_id=None,
                    idempotency_key=idempotency_key,
                    created_at=now,
                    counterparty_label=name,
                    counterparty_external_number=recipient_number,
                )
                self.audit.log(
                    conn,
                    created_at=now,
                    actor_user_id=user_id,
                    actor_username=str(sender_user["display_name"])
                    if sender_user
                    else str(user_id),
                    action="bank.external_transfer",
                    outcome="success",
                    entity_type="transaction",
                    entity_id=transaction_id,
                    details={
                        "amount_paise": amount_paise,
                        "external": True,
                        "recipient_name": name,
                        "to_masked": mask_account(recipient_number),
                        "from_masked": mask_account(str(sender["account_number"])),
                    },
                )
            receipt = self._receipt(
                reference=reference,
                type_="transfer_debit",
                direction="debit",
                amount_paise=amount_paise,
                balance_paise=after,
                description=label,
                counterparty_name=name,
                counterparty_account=recipient_number,
                created_at=now,
                external=True,
            )
            event = self._event(
                transaction_id=transaction_id,
                reference=reference,
                type_="transfer_debit",
                direction="debit",
                amount_paise=amount_paise,
                balance_paise=after,
                counterparty_name=name,
                description=label,
                created_at=now,
            )
            return OperationOutcome(receipt=receipt, notifications=[(user_id, event)])
        except sqlite3.IntegrityError as exc:
            duplicate = self._duplicate_external_transfer(user_id, idempotency_key)
            if duplicate:
                return duplicate
            logger.warning("External transfer integrity error: %s", exc)
            raise ConflictError("The transfer could not be completed.") from exc
        except BankingError:
            if sender_account_id is not None:
                self._record_failed(
                    user_id=user_id,
                    account_id=sender_account_id,
                    type_="transfer_debit",
                    direction="debit",
                    amount_paise=amount_paise,
                    description=description or f"External transfer to {name}",
                )
            raise

    def _duplicate_external_transfer(
        self, user_id: int, key: str
    ) -> OperationOutcome | None:
        with self.db.connection() as conn:
            existing = self.transactions.get_by_idempotency(conn, key)
            if existing is None:
                return None
            account = self._own_account(conn, user_id)
            if int(existing["account_id"]) != int(account["id"]):
                raise ConflictError("That request key is already in use.")
            if (
                str(existing["type"]) != "transfer_debit"
                or existing.get("transfer_id") is not None
            ):
                raise ConflictError(
                    "That request key was already used for a different operation.",
                    field="idempotency_key",
                )
            if str(existing["status"]) != "success":
                return None
            return OperationOutcome(
                receipt=self._receipt(
                    reference=str(existing["reference"]),
                    type_="transfer_debit",
                    direction="debit",
                    amount_paise=int(existing["amount_paise"]),
                    balance_paise=int(account["balance_paise"]),
                    description=existing.get("description"),
                    counterparty_name=existing.get("counterparty_label"),
                    counterparty_account=existing.get("counterparty_external_number"),
                    created_at=str(existing["created_at"]),
                    duplicate=True,
                    external=True,
                ),
                notifications=[],
            )

    def _duplicate_transfer(self, user_id: int, key: str) -> OperationOutcome | None:
        with self.db.connection() as conn:
            transfer = self.transfers.get_by_idempotency(conn, key)
            if transfer is None:
                return None
            if int(transfer["initiated_by_user_id"]) != user_id:
                raise ConflictError("That request key is already in use.")
            return self._outcome_from_transfer(conn, transfer, duplicate=True)

    def _outcome_from_transfer(
        self, conn: sqlite3.Connection, transfer: dict[str, Any], *, duplicate: bool
    ) -> OperationOutcome:
        rows = self.transfers.ledger_rows(conn, int(transfer["id"]))
        sender_row = next(
            (row for row in rows if int(row["account_id"]) == int(transfer["from_account_id"])),
            None,
        )
        recipient_row = next(
            (row for row in rows if int(row["account_id"]) == int(transfer["to_account_id"])),
            None,
        )
        sender_account = self.accounts.get_by_id(conn, int(transfer["from_account_id"]))
        recipient_account = self.accounts.get_by_id(conn, int(transfer["to_account_id"]))
        recipient_user = (
            conn.execute(
                "SELECT display_name FROM users WHERE id = ?",
                (recipient_account["user_id"],),
            ).fetchone()
            if recipient_account
            else None
        )
        sender_user = (
            conn.execute(
                "SELECT display_name FROM users WHERE id = ?",
                (sender_account["user_id"],),
            ).fetchone()
            if sender_account
            else None
        )
        receipt = self._receipt(
            reference=str(transfer["reference"]),
            type_="transfer_debit",
            direction="debit",
            amount_paise=int(transfer["amount_paise"]),
            balance_paise=int(sender_row["balance_after_paise"])
            if sender_row
            else int(sender_account["balance_paise"]),
            description=transfer.get("description"),
            counterparty_name=str(recipient_user["display_name"]) if recipient_user else None,
            counterparty_account=str(recipient_account["account_number"])
            if recipient_account
            else None,
            created_at=str(transfer["created_at"]),
            duplicate=duplicate,
        )
        return OperationOutcome(receipt=receipt, notifications=[])

    # ------------------------------------------------------------ transaction UI
    def list_transactions(
        self,
        *,
        user_id: int,
        page: int,
        page_size: int,
        search: str = "",
        type_filter: str = "all",
        status: str = "all",
        direction: str = "all",
        date_from: date | None = None,
        date_to: date | None = None,
        sort: str = "created_at",
        order: str = "desc",
    ) -> dict[str, Any]:
        page = max(1, page)
        page_size = min(100, max(5, page_size))
        with self.db.connection() as conn:
            account = self._own_account(conn, user_id)
            rows, total = self.transactions.list(
                conn,
                account_id=int(account["id"]),
                page=page,
                page_size=page_size,
                search=clean_text(search, max_length=100),
                type_filter=type_filter,
                status=status,
                direction=direction,
                date_from=start_of_day(date_from) if date_from else None,
                date_to=end_of_day(date_to) if date_to else None,
                sort=sort,
                order=order,
            )
        pages = max(1, (total + page_size - 1) // page_size)
        return {
            "items": [self.public_transaction(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        }

    def get_transaction(self, *, user_id: int, transaction_id: int) -> dict[str, Any]:
        with self.db.connection() as conn:
            account = self._own_account(conn, user_id)
            row = self.transactions.get_for_account(conn, transaction_id, int(account["id"]))
        if row is None:
            raise NotFoundError("Transaction not found.")
        return self.public_transaction(row)

    # ------------------------------------------------------------ fund requests
    def create_fund_request(
        self,
        *,
        user_id: int,
        amount: Decimal,
        reason: str | None,
    ) -> dict[str, Any]:
        amount_paise = decimal_to_paise(amount)
        reason = validate_description(reason)
        now = iso()
        request_id = 0
        with self.db.transaction() as conn:
            account = self._own_account(conn, user_id)
            if str(account["status"]) != "active":
                raise BankingError(
                    "This account is not active. Fund requests are disabled.",
                    code="account_inactive",
                )
            request_id = self.fund_requests.create(
                conn,
                user_id=user_id,
                account_id=int(account["id"]),
                amount_paise=amount_paise,
                reason=reason or "Funds requested",
                requested_by=user_id,
                now=now,
            )
        with self.db.connection() as conn:
            row = self.fund_requests.get_by_id_with_details(conn, request_id)
        return row or {}

    def list_my_fund_requests(self, *, user_id: int) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            return self.fund_requests.list_by_user(conn, user_id)

    def list_fund_requests(self, *, user_id: int, status: str | None = None) -> list[dict[str, Any]]:
        if not self._is_owner_user(user_id):
            raise PermissionError("Only the owner can view fund requests.")
        with self.db.connection() as conn:
            return self.fund_requests.list_all(conn, status)

    def approve_fund_request(self, *, owner_user_id: int, request_id: int) -> dict[str, Any]:
        if not self._is_owner_user(owner_user_id):
            raise PermissionError("Only the owner can approve fund requests.")
        now = iso()
        with self.db.transaction() as conn:
            fr = self.fund_requests.get_by_id(conn, request_id)
            if not fr:
                raise NotFoundError("Fund request not found.")
            if fr["status"] != "pending":
                raise BankingError("Only pending requests can be approved.", code="invalid_state")
            self.fund_requests.update_status(conn, request_id, "approved", reviewed_by=owner_user_id, now=now)
        with self.db.connection() as conn:
            return self.fund_requests.get_by_id_with_details(conn, request_id)

    def reject_fund_request(self, *, owner_user_id: int, request_id: int) -> dict[str, Any]:
        if not self._is_owner_user(owner_user_id):
            raise PermissionError("Only the owner can reject fund requests.")
        now = iso()
        with self.db.transaction() as conn:
            fr = self.fund_requests.get_by_id(conn, request_id)
            if not fr:
                raise NotFoundError("Fund request not found.")
            if fr["status"] != "pending":
                raise BankingError("Only pending requests can be rejected.", code="invalid_state")
            self.fund_requests.update_status(conn, request_id, "rejected", reviewed_by=owner_user_id, now=now)
        with self.db.connection() as conn:
            return self.fund_requests.get_by_id_with_details(conn, request_id)

    def fulfill_fund_request(self, *, owner_user_id: int, request_id: int) -> dict[str, Any]:
        if not self._is_owner_user(owner_user_id):
            raise PermissionError("Only the owner can fulfill fund requests.")
        amount_paise = 0
        account_id = 0
        user_id_req = 0
        try:
            now = iso()
            reference = new_reference("TXN")
            with self.db.transaction() as conn:
                fr = self.fund_requests.get_by_id(conn, request_id)
                if not fr:
                    raise NotFoundError("Fund request not found.")
                if fr["status"] not in ("approved", "pending"):
                    raise BankingError("Request must be approved or pending to fulfill.", code="invalid_state")
                account_id = int(fr["account_id"])
                user_id_req = int(fr["user_id"])
                amount_paise = int(fr["amount_paise"])
                account = self.accounts.get_by_id(conn, account_id)
                if not account or str(account["status"]) != "active":
                    raise BankingError("Target account is not active.", code="account_inactive")
                username_row = conn.execute(
                    "SELECT username FROM users WHERE id = ?", (owner_user_id,)
                ).fetchone()
                username = str(username_row["username"]) if username_row else str(owner_user_id)
                before = int(account["balance_paise"])
                if not self.accounts.credit(conn, account_id, amount_paise, now):
                    raise BankingError("The deposit could not be completed.", code="deposit_failed")
                after = before + amount_paise
                transaction_id = self.transactions.insert(
                    conn,
                    reference=reference,
                    account_id=account_id,
                    counterparty_account_id=None,
                    type_="deposit",
                    direction="credit",
                    amount_paise=amount_paise,
                    balance_before_paise=before,
                    balance_after_paise=after,
                    status="success",
                    description=f"Fund request #{request_id} fulfilled by owner",
                    transfer_id=None,
                    idempotency_key=None,
                    created_at=now,
                )
                self.fund_requests.update_status(
                    conn,
                    request_id,
                    "fulfilled",
                    reviewed_by=owner_user_id,
                    fulfilled_by=owner_user_id,
                    transaction_id=transaction_id,
                    now=now,
                )
                self.audit.log(
                    conn,
                    created_at=now,
                    actor_user_id=owner_user_id,
                    actor_username=username,
                    action="bank.fund_request.fulfill",
                    outcome="success",
                    entity_type="fund_request",
                    entity_id=request_id,
                    details={"amount_paise": amount_paise, "credited_to_user_id": user_id_req},
                )
            with self.db.connection() as conn:
                return self.fund_requests.get_by_id_with_details(conn, request_id)
        except sqlite3.IntegrityError:
            raise
        except BankingError:
            raise

    def all_transactions(
        self,
        *,
        user_id: int,
        search: str = "",
        type_filter: str = "all",
        status: str = "all",
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            account = self._own_account(conn, user_id)
            rows, _ = self.transactions.list(
                conn,
                account_id=int(account["id"]),
                page=1,
                page_size=50000,
                search=search,
                type_filter=type_filter,
                status=status,
                date_from=start_of_day(date_from) if date_from else None,
                date_to=end_of_day(date_to) if date_to else None,
                sort="created_at",
                order="desc",
            )
        return [self.public_transaction(row) for row in rows]
