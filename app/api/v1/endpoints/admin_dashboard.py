"""Admin operations dashboard — portfolio KPIs across the WHOLE book.

Lender/admin portal Phase 1 (docs/lender_admin_portal_spec.md M1). Unlike the
clinic dashboard (scoped to one vendor), this is the god-view: aggregates over
ALL applications and loans. Read-only; admin/staff gated at the router level.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.loan import PlatformLoan

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])

_DECIDED = ("approved", "declined")


class ApplicationsBlock(BaseModel):
    total: int
    by_status: dict[str, int]
    approval_rate: float | None  # approved / (approved + declined); None if no decisions


class LoanBookBlock(BaseModel):
    total: int
    by_status: dict[str, int]
    active_principal_balance_cents: int
    disbursed_amount_cents: int


class WorkQueueBlock(BaseModel):
    under_review: int          # applications needing a human decision
    pending_disbursement: int  # loans awaiting funding
    delinquent: int            # loans past due


class AdminOverview(BaseModel):
    applications: ApplicationsBlock
    loan_book: LoanBookBlock
    work_queue: WorkQueueBlock


def _counts_by(db: Session, model, status_col) -> dict[str, int]:
    rows = db.query(status_col, func.count()).group_by(status_col).all()
    return {str(s): int(c) for s, c in rows}


@router.get("/overview", response_model=AdminOverview)
def overview(db: Session = Depends(get_db), _user=Depends(get_current_user)):
    """One-shot portfolio overview for the admin home screen."""
    app_counts = _counts_by(db, PlatformCreditApplication, PlatformCreditApplication.status)
    app_total = sum(app_counts.values())
    approved = app_counts.get("approved", 0)
    declined = app_counts.get("declined", 0)
    approval_rate = (approved / (approved + declined)) if (approved + declined) else None

    loan_counts = _counts_by(db, PlatformLoan, PlatformLoan.status)
    loan_total = sum(loan_counts.values())
    active_balance = (
        db.query(func.coalesce(func.sum(PlatformLoan.principal_balance_cents), 0))
        .filter(PlatformLoan.status == "active")
        .scalar()
        or 0
    )
    disbursed_amount = (
        db.query(func.coalesce(func.sum(PlatformLoan.principal_cents), 0))
        .filter(PlatformLoan.disbursed_at.isnot(None))
        .scalar()
        or 0
    )

    return AdminOverview(
        applications=ApplicationsBlock(
            total=app_total, by_status=app_counts, approval_rate=approval_rate
        ),
        loan_book=LoanBookBlock(
            total=loan_total,
            by_status=loan_counts,
            active_principal_balance_cents=int(active_balance),
            disbursed_amount_cents=int(disbursed_amount),
        ),
        work_queue=WorkQueueBlock(
            under_review=app_counts.get("under_review", 0),
            pending_disbursement=loan_counts.get("pending_disbursement", 0),
            delinquent=loan_counts.get("delinquent", 0),
        ),
    )
