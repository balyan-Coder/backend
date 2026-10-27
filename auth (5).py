"""Registration, login, sessions and profile operations."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from app.config import Settings
from app.database import Database
from app.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionError,
    RateLimitError,
    ValidationError,
)
from app.repositories.accounts import AccountRepository
from app.repositories.audit import AuditRepository
from app.repositories.sessions import SessionRepository
from app.repositories.settings import SettingsRepository
from app.repositories.transactions import TransactionRepository
from app.repositories.users import UserRepository
from app.security import (
    new_csrf_token,
    new_session_token,
    dummy_verify,
    hash_password,
    hash_token,
    verify_password,
)
from app.utils.money import mask_account, new_reference
from app.utils.time import iso, parse_iso, utcnow
from app.utils.validation import (
    clean_text,
    normalize_email,
    validate_display_name,
    validate_password,
    validate_username,
)

logger = logging.getLogger(__name__)

# Marks the one-time owner-balance reconciliation so a later restart cannot
# repeat the adjustment after the owner has spent or moved funds.
OWNER_BOOTSTRAP_KEY = "owner_balance_bootstrapped_v2"


@dataclass
class AuthResult:
    user: dict
    account: dict
    csrf_token: str
    expires_at: str
    raw_token: str
    max_age: int


class AuthService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.users = UserRepository()
        self.sessions = SessionRepository()
        self.audit = AuditRepository()
        self.accounts = AccountRepository()
        self.transactions = TransactionRepository()
        self.app_settings = SettingsRepository()

    # -------------------------------------------------------------- registration
    def register(
        self,
        *,
        username: str,
        email: str,
        display_name: str,
        password: str,
        confirm_password: str,
        accept_demo_policy: bool,
        ip_address: str | None,
        user_agent: str | None,
    ) -> AuthResult:
        username = validate_username(username)
        email = normalize_email(email)
        display_name = validate_display_name(display_name)
        password = validate_password(password)
        if password != confirm_password:
            raise ValidationError(
                "Passwords do not match.", field="confirm_password"
            )
        if not accept_demo_policy:
            raise ValidationError(
                "Please acknowledge the simulated demo policy.",
                field="accept_demo_policy",
            )

        password_hash = hash_password(password, self.settings)
        now_dt = utcnow()
        now = iso(now_dt)
        expires_at = iso(now_dt + timedelta(minutes=self.settings.session_minutes))
        raw_token = new_session_token()
        csrf_token = new_csrf_token()
        max_age = self.settings.session_minutes * 60
        is_owner = self.settings.is_owner_username(username)
        starting_balance = (
            self.settings.owner_starting_balance_paise
            if is_owner
            else self.settings.demo_starting_balance_paise
        )

        try:
            with self.db.transaction() as conn:
                if self.users.username_exists(conn, username):
                    raise ConflictError(
                        "That username is already registered.", field="username"
                    )
                if self.users.email_exists(conn, email):
                    raise ConflictError(
                        "That email is already registered.", field="email"
                    )
                user_id = self.users.create(
                    conn,
                    username=username,
                    email=email,
                    display_name=display_name,
                    password_hash=password_hash,
                    now=now,
                )
                account_number = self.app_settings.next_account_number(conn, now)
                account_id = self.accounts.create(
                    conn,
                    account_number=account_number,
                    user_id=user_id,
                    account_type="savings",
                    balance_paise=starting_balance,
                    now=now,
                )
                if starting_balance > 0:
                    self.transactions.insert(
                        conn,
                        reference=new_reference("TXN"),
                        account_id=account_id,
                        counterparty_account_id=None,
                        type_="deposit",
                        direction="credit",
                        amount_paise=starting_balance,
                        balance_before_paise=0,
                        balance_after_paise=starting_balance,
                        status="success",
                        description=(
                            "Simulated owner opening balance"
                            if is_owner
                            else "Simulated demo starting balance"
                        ),
                        transfer_id=None,
                        idempotency_key=None,
                        created_at=now,
                        counts_toward_limit=0,
                    )
                if is_owner:
                    self.app_settings.set(
                        conn,
                        OWNER_BOOTSTRAP_KEY,
                        f"user={user_id};balance={starting_balance}",
                        now,
                    )
                self.audit.log(
                    conn,
                    created_at=now,
                    actor_user_id=user_id,
                    actor_username=username,
                    action="auth.register",
                    outcome="success",
                    entity_type="user",
                    entity_id=user_id,
                    details={
                        "account_number_masked": mask_account(account_number),
                        "starting_balance_paise": starting_balance,
                        "owner_account": is_owner,
                    },
                )
                self.sessions.create(
                    conn,
                    user_id=user_id,
                    token_hash=hash_token(raw_token),
                    csrf_token=csrf_token,
                    created_at=now,
                    expires_at=expires_at,
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
                user = self.users.get_by_id(conn, user_id)
                account = self.accounts.get_by_user_id(conn, user_id)
        except sqlite3.IntegrityError as exc:
            logger.warning("Registration integrity conflict: %s", exc)
            raise ConflictError(
                "That username or email is already registered."
            ) from exc

        assert user is not None and account is not None
        logger.info("New user registered: %s", username)
        return AuthResult(
            user=self.public_user(user),
            account=self.public_account(account),
            csrf_token=csrf_token,
            expires_at=expires_at,
            raw_token=raw_token,
            max_age=max_age,
        )

    # --------------------------------------------------------------------- login
    def login(
        self,
        *,
        identifier: str,
        password: str,
        ip_address: str | None,
        user_agent: str | None,
    ) -> AuthResult:
        login_id = clean_text(identifier, max_length=254).lower()
        if not login_id or not password:
            raise AuthenticationError("Enter your username or email and password.")
        now_dt = utcnow()
        now = iso(now_dt)
        with self.db.connection() as conn:
            user = self.users.find_by_login(conn, login_id)
            if user is None:
                dummy_verify(password, self.settings)
                with self.db.transaction() as audit_conn:
                    self.audit.log(
                        audit_conn,
                        created_at=now,
                        actor_user_id=None,
                        actor_username=login_id or "unknown",
                        action="auth.login",
                        outcome="failure",
                        details={"reason": "unknown_identifier"},
                    )
                raise AuthenticationError("Incorrect username/email or password.")

            locked_until = user.get("locked_until")
            if locked_until:
                try:
                    if parse_iso(str(locked_until)) > now_dt:
                        remaining = max(
                            1,
                            int((parse_iso(str(locked_until)) - now_dt).total_seconds()) + 1,
                        )
                        with self.db.transaction() as audit_conn:
                            self.audit.log(
                                audit_conn,
                                created_at=now,
                                actor_user_id=int(user["id"]),
                                actor_username=str(user["username"]),
                                action="auth.login",
                                outcome="failure",
                                details={"reason": "locked"},
                            )
                        raise RateLimitError(
                            f"Too many failed attempts. Try again in {remaining} seconds.",
                            retry_after=remaining,
                        )
                except ValueError:
                    pass

            if str(user["status"]) != "active":
                raise AuthenticationError(
                    "This account is suspended. Contact the administrator."
                )

            if not verify_password(password, str(user["password_hash"]), self.settings):
                attempts = int(user.get("failed_attempts") or 0) + 1
                locked = None
                if attempts >= self.settings.login_max_attempts:
                    locked = iso(now_dt + timedelta(seconds=self.settings.login_lock_seconds))
                with self.db.transaction() as conn_write:
                    self.users.register_failure(
                        conn_write,
                        int(user["id"]),
                        attempts=attempts,
                        locked_until=locked,
                    )
                    self.audit.log(
                        conn_write,
                        created_at=now,
                        actor_user_id=int(user["id"]),
                        actor_username=str(user["username"]),
                        action="auth.login",
                        outcome="failure",
                        details={"reason": "invalid_password", "attempts": attempts},
                    )
                if locked:
                    raise RateLimitError(
                        "Too many failed attempts. "
                        f"Your sign-in is locked for {self.settings.login_lock_seconds} seconds.",
                        retry_after=self.settings.login_lock_seconds,
                    )
                raise AuthenticationError("Incorrect username/email or password.")

            expires_at = iso(now_dt + timedelta(minutes=self.settings.session_minutes))
            raw_token = new_session_token()
            csrf_token = new_csrf_token()
            max_age = self.settings.session_minutes * 60
            with self.db.transaction() as conn_write:
                self.users.register_success(
                    conn_write, int(user["id"]), str(user["created_at"]), now
                )
                self.sessions.purge_expired(conn_write, now)
                self.sessions.create(
                    conn_write,
                    user_id=int(user["id"]),
                    token_hash=hash_token(raw_token),
                    csrf_token=csrf_token,
                    created_at=now,
                    expires_at=expires_at,
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
                self.audit.log(
                    conn_write,
                    created_at=now,
                    actor_user_id=int(user["id"]),
                    actor_username=str(user["username"]),
                    action="auth.login",
                    outcome="success",
                )
                fresh_user = self.users.get_by_id(conn_write, int(user["id"]))
                account = self.accounts.get_by_user_id(conn_write, int(user["id"]))

        assert fresh_user is not None and account is not None
        logger.info("User signed in: %s", fresh_user["username"])
        return AuthResult(
            user=self.public_user(fresh_user),
            account=self.public_account(account),
            csrf_token=csrf_token,
            expires_at=expires_at,
            raw_token=raw_token,
            max_age=max_age,
        )

    # -------------------------------------------------------------------- logout
    def logout(self, *, token_hash: str, user: dict) -> None:
        now = iso()
        with self.db.transaction() as conn:
            self.sessions.revoke(conn, token_hash, now)
            self.audit.log(
                conn,
                created_at=now,
                actor_user_id=int(user["id"]),
                actor_username=str(user["username"]),
                action="auth.logout",
                outcome="success",
            )

    # ---------------------------------------------------------------- profile
    def profile(self, user_id: int) -> dict:
        with self.db.connection() as conn:
            user = self.users.get_by_id(conn, user_id)
            account = self.accounts.get_by_user_id(conn, user_id)
        if user is None or account is None:
            raise NotFoundError("Profile not found.")
        return {
            "user": self.public_user(user),
            "account": self.public_account(account),
        }

    def update_profile(
        self, *, user_id: int, display_name: str, email: str
    ) -> dict:
        display_name = validate_display_name(display_name)
        email = normalize_email(email)
        now = iso()
        with self.db.transaction() as conn:
            user = self.users.get_by_id(conn, user_id)
            if user is None:
                raise NotFoundError("Profile not found.")
            existing = self.users.find_by_login(conn, email)
            if existing and int(existing["id"]) != user_id:
                raise ConflictError("That email is already registered.", field="email")
            self.users.update_profile(
                conn, user_id, display_name=display_name, email=email, now=now
            )
            self.audit.log(
                conn,
                created_at=now,
                actor_user_id=user_id,
                actor_username=str(user["username"]),
                action="profile.update",
                outcome="success",
            )
            updated = self.users.get_by_id(conn, user_id)
            account = self.accounts.get_by_user_id(conn, user_id)
        assert updated is not None and account is not None
        return {
            "user": self.public_user(updated),
            "account": self.public_account(account),
        }

    def change_password(
        self,
        *,
        user_id: int,
        session_id: int,
        current_password: str,
        new_password: str,
        confirm_new_password: str,
    ) -> None:
        if new_password != confirm_new_password:
            raise ValidationError(
                "New passwords do not match.", field="confirm_new_password"
            )
        new_password = validate_password(new_password)
        with self.db.connection() as conn:
            user = self.users.get_by_id(conn, user_id)
        if user is None:
            raise NotFoundError("Profile not found.")
        if not verify_password(current_password, str(user["password_hash"]), self.settings):
            now = iso()
            with self.db.transaction() as conn:
                self.audit.log(
                    conn,
                    created_at=now,
                    actor_user_id=user_id,
                    actor_username=str(user["username"]),
                    action="profile.password_change",
                    outcome="failure",
                    details={"reason": "current_password_incorrect"},
                )
            raise AuthenticationError("The current password is incorrect.")
        if verify_password(new_password, str(user["password_hash"]), self.settings):
            raise ValidationError(
                "Choose a password different from your current one.", field="new_password"
            )
        new_hash = hash_password(new_password, self.settings)
        now = iso()
        with self.db.transaction() as conn:
            self.users.update_password(conn, user_id, new_hash, now)
            self.sessions.revoke_others(conn, user_id, session_id, now)
            self.audit.log(
                conn,
                created_at=now,
                actor_user_id=user_id,
                actor_username=str(user["username"]),
                action="profile.password_change",
                outcome="success",
            )

    # ------------------------------------------------------- owner / presence
    def is_owner(self, user: dict | None) -> bool:
        return bool(user) and self.settings.is_owner_username(str(user.get("username", "")))

    def bootstrap_owner_balance(self) -> dict | None:
        """Apply a one-time append-only owner opening balance to an existing account.

        The existing GAUR BANK database may already contain the owner with an
        ordinary demo balance.  Instead of rewriting history, this reconciles the
        account to the configured owner amount with a new ledger entry and stores
        a marker so the adjustment is never repeated.
        """

        if not (self.settings.owner_username or "").strip():
            return None
        now = iso()
        outcome: dict | None = None
        with self.db.transaction() as conn:
            if self.app_settings.get(conn, OWNER_BOOTSTRAP_KEY):
                return None
            user = self.users.find_by_login(conn, self.settings.owner_username)
            if user is None:
                return None
            account = self.accounts.get_by_user_id(conn, int(user["id"]))
            if account is None:
                return None
            account_id = int(account["id"])
            current = int(account["balance_paise"])
            target = int(self.settings.owner_starting_balance_paise)
            delta = target - current
            if delta != 0:
                if delta > 0:
                    applied = self.accounts.credit(conn, account_id, delta, now)
                    type_, direction = "deposit", "credit"
                else:
                    applied = self.accounts.debit(conn, account_id, -delta, now)
                    type_, direction = "withdrawal", "debit"
                if not applied:
                    logger.warning(
                        "Owner balance bootstrap skipped for %s: account %s is not active or has insufficient funds.",
                        user["username"],
                        account_id,
                    )
                    return None
                self.transactions.insert(
                    conn,
                    reference=new_reference("TXN"),
                    account_id=account_id,
                    counterparty_account_id=None,
                    type_=type_,
                    direction=direction,
                    amount_paise=abs(delta),
                    balance_before_paise=current,
                    balance_after_paise=target,
                    status="success",
                    description="Owner opening balance adjustment (one-time, simulated)",
                    transfer_id=None,
                    idempotency_key=None,
                    created_at=now,
                    counts_toward_limit=0,
                )
                self.audit.log(
                    conn,
                    created_at=now,
                    actor_user_id=int(user["id"]),
                    actor_username=str(user["username"]),
                    action="owner.balance_bootstrap",
                    outcome="success",
                    entity_type="account",
                    entity_id=account_id,
                    details={
                        "balance_before_paise": current,
                        "balance_after_paise": target,
                        "adjustment_paise": delta,
                    },
                )
                outcome = {
                    "user_id": int(user["id"]),
                    "account_id": account_id,
                    "balance_before_paise": current,
                    "balance_after_paise": target,
                    "adjustment_paise": delta,
                }
            self.app_settings.set(
                conn,
                OWNER_BOOTSTRAP_KEY,
                f"user={int(user['id'])};balance={target}",
                now,
            )
        return outcome

    def members(self, *, requester_user_id: int, online_user_ids: set[int]) -> dict:
        """Owner-only member directory with online/offline presence."""

        with self.db.connection() as conn:
            requester = self.users.get_by_id(conn, requester_user_id)
            if requester is None:
                raise NotFoundError("Profile not found.")
            if not self.is_owner(requester):
                raise PermissionError(
                    "Only the configured owner may view member presence."
                )
            rows = self.users.list_members(conn)

        members: list[dict] = []
        for row in rows:
            user_id = int(row["id"])
            account_number = row.get("account_number")
            members.append(
                {
                    "id": user_id,
                    "username": str(row["username"]),
                    "display_name": str(row["display_name"]),
                    "masked_account_number": mask_account(str(account_number))
                    if account_number
                    else None,
                    "account_status": row.get("account_status"),
                    "user_status": str(row["user_status"]),
                    "is_owner": self.settings.is_owner_username(
                        str(row["username"])
                    ),
                    "online": user_id in online_user_ids,
                    "created_at": str(row["created_at"]),
                    "last_login_at": row.get("last_login_at"),
                }
            )
        members.sort(key=lambda member: (not member["online"], member["username"].casefold()))
        online_count = sum(1 for member in members if member["online"])
        return {
            "members": members,
            "total": len(members),
            "online_count": online_count,
            "offline_count": len(members) - online_count,
            "refreshed_at": iso(),
            "simulated": True,
        }

    # ---------------------------------------------------------------- helpers
    def public_user(self, user: dict) -> dict:
        return {
            "id": int(user["id"]),
            "username": str(user["username"]),
            "email": str(user["email"]),
            "display_name": str(user["display_name"]),
            "created_at": str(user["created_at"]),
            "last_login_at": user.get("last_login_at"),
            "is_owner": self.is_owner(user),
        }

    @staticmethod
    def public_account(account: dict) -> dict:
        return {
            "id": int(account["id"]),
            "account_number": str(account["account_number"]),
            "masked_account_number": mask_account(str(account["account_number"])),
            "account_type": str(account["account_type"]),
            "balance_paise": int(account["balance_paise"]),
            "status": str(account["status"]),
            "currency": str(account["currency"]),
            "created_at": str(account["created_at"]),
        }
