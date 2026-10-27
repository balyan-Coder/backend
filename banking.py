"""Dashboard, deposits, withdrawals, transfers and ledger endpoints."""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, Response

from app.deps import (
    SessionContext,
    banking_rate_limit,
    banking_write_rate_limit,
    get_banking_service,
    require_csrf,
)
from app.schemas import (
    DepositIn,
    FundRequestIn,
    ResolveTransferIn,
    TransferIn,
    WithdrawIn,
)
from app.services.banking import BankingService
from app.utils.money import paise_to_string
from app.utils.time import utcnow

router = APIRouter(tags=["banking"])

TransactionType = Literal[
    "all", "deposit", "withdrawal", "transfer_debit", "transfer_credit"
]
TransactionStatus = Literal["all", "success", "failed"]
Direction = Literal["all", "credit", "debit"]


@router.get("/dashboard")
def dashboard(
    period: Literal["7d", "30d", "90d", "365d"] = Query("30d"),
    session: SessionContext = Depends(banking_rate_limit),
    service: BankingService = Depends(get_banking_service),
) -> dict:
    return service.dashboard(user_id=session.user_id, period=period)


@router.post("/deposits")
async def deposit(
    payload: DepositIn,
    request: Request,
    session: SessionContext = Depends(require_csrf),
    _: SessionContext = Depends(banking_write_rate_limit),
    service: BankingService = Depends(get_banking_service),
) -> dict:
    outcome = service.deposit(
        user_id=session.user_id,
        amount=payload.amount,
        description=payload.description,
        idempotency_key=payload.idempotency_key,
    )
    await request.app.state.realtime.publish(outcome.notifications)
    return {
        "message": "Simulated deposit completed.",
        "receipt": outcome.receipt,
        "simulated": True,
    }


@router.post("/withdrawals")
async def withdraw(
    payload: WithdrawIn,
    request: Request,
    session: SessionContext = Depends(require_csrf),
    _: SessionContext = Depends(banking_write_rate_limit),
    service: BankingService = Depends(get_banking_service),
) -> dict:
    outcome = service.withdraw(
        user_id=session.user_id,
        amount=payload.amount,
        description=payload.description,
        idempotency_key=payload.idempotency_key,
    )
    await request.app.state.realtime.publish(outcome.notifications)
    return {
        "message": "Simulated withdrawal completed.",
        "receipt": outcome.receipt,
        "simulated": True,
    }


@router.post("/transfers/resolve")
def resolve_transfer(
    payload: ResolveTransferIn,
    session: SessionContext = Depends(banking_rate_limit),
    service: BankingService = Depends(get_banking_service),
) -> dict:
    return service.resolve_recipient(
        user_id=session.user_id,
        account_number=payload.recipient_account_number,
    )


@router.post("/transfers")
async def create_transfer(
    payload: TransferIn,
    request: Request,
    session: SessionContext = Depends(require_csrf),
    _: SessionContext = Depends(banking_write_rate_limit),
    service: BankingService = Depends(get_banking_service),
) -> dict:
    outcome = service.transfer(
        user_id=session.user_id,
        recipient_account_number=payload.recipient_account_number,
        amount=payload.amount,
        description=payload.description,
        idempotency_key=payload.idempotency_key,
        external=payload.external,
        recipient_name=payload.recipient_name,
    )
    await request.app.state.realtime.publish(outcome.notifications)
    return {
        "message": "Simulated transfer completed."
        if not outcome.receipt.get("duplicate")
        else "This transfer was already completed.",
        "receipt": outcome.receipt,
        "simulated": True,
    }


@router.get("/transactions/export")
def export_transactions(
    session: SessionContext = Depends(banking_rate_limit),
    service: BankingService = Depends(get_banking_service),
    search: str = Query("", max_length=100),
    type_filter: TransactionType = Query("all", alias="type"),
    status_filter: TransactionStatus = Query("all", alias="status"),
    direction: Direction = Query("all"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
) -> Response:
    rows = service.all_transactions(
        user_id=session.user_id,
        search=search,
        type_filter=type_filter,
        status=status_filter,
        date_from=date_from,
        date_to=date_to,
    )
    buffer = io.StringIO()
    buffer.write("\ufeff")
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "Reference",
            "Created (UTC)",
            "Type",
            "Direction",
            "Status",
            "Amount (INR)",
            "Balance before (INR)",
            "Balance after (INR)",
            "Description",
            "Counterparty",
            "Transfer reference",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row["reference"],
                row["created_at"],
                row["type"],
                row["direction"],
                row["status"],
                paise_to_string(row["amount_paise"]),
                paise_to_string(row["balance_before_paise"]),
                paise_to_string(row["balance_after_paise"]),
                row.get("description") or "",
                row.get("counterparty_name") or "",
                row.get("transfer_reference") or "",
            ]
        )
    filename = f"gaur-bank-transactions-{utcnow().date().isoformat()}.csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Simulated": "true",
        },
    )


@router.get("/transactions")
def list_transactions(
    session: SessionContext = Depends(banking_rate_limit),
    service: BankingService = Depends(get_banking_service),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=5, le=100),
    search: str = Query("", max_length=100),
    type_filter: TransactionType = Query("all", alias="type"),
    status_filter: TransactionStatus = Query("all", alias="status"),
    direction: Direction = Query("all"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    sort: Literal["created_at", "amount_paise", "type", "status"] = Query("created_at"),
    order: Literal["asc", "desc"] = Query("desc"),
) -> dict:
    return service.list_transactions(
        user_id=session.user_id,
        page=page,
        page_size=page_size,
        search=search,
        type_filter=type_filter,
        status=status_filter,
        direction=direction,
        date_from=date_from,
        date_to=date_to,
        sort=sort,
        order=order,
    )


@router.get("/transactions/{transaction_id}")
def get_transaction(
    transaction_id: int,
    session: SessionContext = Depends(banking_rate_limit),
    service: BankingService = Depends(get_banking_service),
) -> dict:
    return service.get_transaction(
        user_id=session.user_id, transaction_id=transaction_id
    )


@router.post("/fund-requests")
async def create_fund_request(
    payload: FundRequestIn,
    request: Request,
    session: SessionContext = Depends(require_csrf),
    _: SessionContext = Depends(banking_write_rate_limit),
    service: BankingService = Depends(get_banking_service),
) -> dict:
    result = service.create_fund_request(
        user_id=session.user_id,
        amount=payload.amount,
        reason=payload.reason,
    )
    await request.app.state.realtime.publish([])
    return {"message": "Fund request submitted.", "request": result, "simulated": True}


@router.get("/fund-requests/mine")
def list_my_fund_requests(
    session: SessionContext = Depends(banking_rate_limit),
    service: BankingService = Depends(get_banking_service),
) -> dict:
    return {"requests": service.list_my_fund_requests(user_id=session.user_id)}


@router.get("/fund-requests")
def list_fund_requests(
    session: SessionContext = Depends(banking_rate_limit),
    service: BankingService = Depends(get_banking_service),
    status: str | None = Query(None),
) -> dict:
    return {"requests": service.list_fund_requests(user_id=session.user_id, status=status)}


@router.post("/fund-requests/{request_id}/approve")
async def approve_fund_request(
    request_id: int,
    request: Request,
    session: SessionContext = Depends(require_csrf),
    _: SessionContext = Depends(banking_write_rate_limit),
    service: BankingService = Depends(get_banking_service),
) -> dict:
    result = service.approve_fund_request(owner_user_id=session.user_id, request_id=request_id)
    await request.app.state.realtime.publish([])
    return {"message": "Fund request approved.", "request": result}


@router.post("/fund-requests/{request_id}/reject")
async def reject_fund_request(
    request_id: int,
    request: Request,
    session: SessionContext = Depends(require_csrf),
    _: SessionContext = Depends(banking_write_rate_limit),
    service: BankingService = Depends(get_banking_service),
) -> dict:
    result = service.reject_fund_request(owner_user_id=session.user_id, request_id=request_id)
    await request.app.state.realtime.publish([])
    return {"message": "Fund request rejected.", "request": result}


@router.post("/fund-requests/{request_id}/fulfill")
async def fulfill_fund_request(
    request_id: int,
    request: Request,
    session: SessionContext = Depends(require_csrf),
    _: SessionContext = Depends(banking_write_rate_limit),
    service: BankingService = Depends(get_banking_service),
) -> dict:
    result = service.fulfill_fund_request(owner_user_id=session.user_id, request_id=request_id)
    await request.app.state.realtime.publish([])
    return {"message": "Fund request fulfilled and credited.", "request": result}
