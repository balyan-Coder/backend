"""Pydantic transport schemas.  Money enters as decimal rupees but leaves as paise."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, EmailStr, Field

Amount = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2)]


class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=20)
    email: EmailStr
    display_name: str = Field(min_length=2, max_length=60)
    password: str = Field(min_length=10, max_length=128)
    confirm_password: str = Field(min_length=10, max_length=128)
    accept_demo_policy: bool = False


class LoginIn(BaseModel):
    identifier: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class ProfileUpdateIn(BaseModel):
    display_name: str = Field(min_length=2, max_length=60)
    email: EmailStr


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)
    confirm_new_password: str = Field(min_length=10, max_length=128)


class DepositIn(BaseModel):
    amount: Amount
    description: str | None = Field(default=None, max_length=200)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class WithdrawIn(BaseModel):
    amount: Amount
    description: str | None = Field(default=None, max_length=200)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class TransferIn(BaseModel):
    recipient_account_number: str = Field(min_length=12, max_length=24)
    amount: Amount
    description: str | None = Field(default=None, max_length=200)
    idempotency_key: str = Field(min_length=8, max_length=128)
    # When true the recipient is outside GAUR BANK and only the sender is debited.
    external: bool = False
    recipient_name: str | None = Field(default=None, max_length=60)


class ResolveTransferIn(BaseModel):
    recipient_account_number: str = Field(min_length=12, max_length=24)


class UserOut(BaseModel):
    id: int
    username: str
    email: str
    display_name: str
    created_at: str
    last_login_at: str | None = None
    is_owner: bool = False


class AuthOut(BaseModel):
    user: UserOut
    csrf_token: str
    expires_at: str


class PublicConfigOut(BaseModel):
    bank_name: str
    currency: str
    demo_starting_balance_paise: int
    transaction_limit_paise: int
    daily_deposit_limit_paise: int
    allow_demo_deposits: bool
    disclaimer: str
    version: str


class AccountOut(BaseModel):
    id: int
    account_number: str
    masked_account_number: str
    account_type: str
    balance_paise: int
    status: str
    currency: str
    created_at: str


class ProfileOut(BaseModel):
    user: UserOut
    account: AccountOut


class MemberOut(BaseModel):
    id: int
    username: str
    display_name: str
    masked_account_number: str | None = None
    account_status: str | None = None
    user_status: str
    is_owner: bool = False
    online: bool
    created_at: str
    last_login_at: str | None = None


class PresenceOut(BaseModel):
    members: list[MemberOut]
    total: int
    online_count: int
    offline_count: int
    refreshed_at: str
    simulated: bool = True


class TransactionOut(BaseModel):
    id: int
    reference: str
    type: str
    direction: str
    amount_paise: int
    balance_before_paise: int
    balance_after_paise: int
    status: str
    description: str | None = None
    transfer_reference: str | None = None
    counterparty_name: str | None = None
    counterparty_account_number: str | None = None
    created_at: str


class TransferResolveOut(BaseModel):
    display_name: str
    masked_account_number: str


class ReceiptOut(BaseModel):
    reference: str
    type: str
    direction: str
    amount_paise: int
    balance_paise: int
    description: str | None = None
    counterparty_name: str | None = None
    counterparty_masked_account: str | None = None
    created_at: str
    duplicate: bool = False


class MessageOut(BaseModel):
    message: str


class FundRequestIn(BaseModel):
    amount: Amount
    reason: str | None = Field(default=None, max_length=200)


class FundRequestOut(BaseModel):
    id: int
    user_id: int
    account_id: int
    amount_paise: int
    reason: str | None = None
    status: str
    requested_by: int
    reviewed_by: int | None = None
    reviewed_at: str | None = None
    fulfilled_by: int | None = None
    fulfilled_at: str | None = None
    transaction_id: int | None = None
    created_at: str
    updated_at: str
    username: str | None = None
    display_name: str | None = None
    account_number: str | None = None
    reviewed_by_username: str | None = None
    fulfilled_by_username: str | None = None


class ErrorOut(BaseModel):
    detail: str
    code: str
    fields: dict[str, str] = Field(default_factory=dict)
