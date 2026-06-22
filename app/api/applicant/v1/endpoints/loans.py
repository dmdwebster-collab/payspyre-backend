"""Borrower loan-servicing + "Pay Now" endpoints.

Lets an authenticated borrower see their active loan(s), the amortization
schedule / what's due, their payment history, and make a payment (Zumrails
collection). Every endpoint enforces the patient JWT and per-patient scope —
a borrower can only touch a loan whose application belongs to their
``patient_id``. (Scoping by ``patient_id`` rather than the token's ``app_ids``
matters because ``app_ids`` excludes approved/terminal applications, and loans
are created from approved applications — see borrower portal PR #94.)

The endpoints are thin: reads project the loan + schedule; the pay action
delegates to ``loan_payments.initiate_payment`` (which owns the Zumrails call +
event emission). Settlement happens out-of-band via the Zumrails webhook.
"""
from __future__ import annotations

from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import ApplicantClaims, get_current_applicant
from app.core.logging import get_logger
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.loan import PlatformLoan, PlatformLoanPayment, PlatformLoanScheduleItem
from app.services import loan_payments

logger = get_logger(__name__)
router = APIRouter(prefix="/loans", tags=["applicant-loans"])


# --- response/request shapes ------------------------------------------------


class ScheduleItemOut(BaseModel):
    installment_number: int
    due_date: date
    total_cents: int
    paid_cents: int
    status: str


class LoanSummary(BaseModel):
    id: UUID
    status: str
    principal_cents: int
    principal_balance_cents: int
    annual_rate_bps: int
    term_months: int
    currency: str
    outstanding_cents: int
    next_due_date: Optional[date]
    next_due_cents: Optional[int]


class LoanDetail(LoanSummary):
    schedule: list[ScheduleItemOut]


class PaymentOut(BaseModel):
    id: UUID
    amount_cents: int
    received_at: str
    method: str
    external_ref: Optional[str]


class PayNowBody(BaseModel):
    amount_cents: int = Field(..., gt=0, description="Amount to pay, in cents")


class PayNowResponse(BaseModel):
    transaction_id: str
    status: str
    amount_cents: int


# --- helpers ----------------------------------------------------------------


def _load_authorized_loan(db: Session, loan_id: UUID, claims: ApplicantClaims) -> PlatformLoan:
    """404 if the loan doesn't exist OR isn't owned by this patient.

    Ownership = the loan's application belongs to ``claims.patient_id``. We
    return 404 (not 403) for the not-owned case so we don't leak the existence
    of other borrowers' loans.
    """
    loan = (
        db.query(PlatformLoan)
        .join(PlatformCreditApplication, PlatformLoan.application_id == PlatformCreditApplication.id)
        .filter(PlatformLoan.id == loan_id)
        .filter(PlatformCreditApplication.patient_id == claims.patient_id)
        .first()
    )
    if loan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Loan not found")
    return loan


def _next_due(db: Session, loan: PlatformLoan):
    item = (
        db.query(PlatformLoanScheduleItem)
        .filter(PlatformLoanScheduleItem.loan_id == loan.id)
        .filter(PlatformLoanScheduleItem.status.in_(loan_payments._OPEN_ITEM_STATUSES))
        .order_by(PlatformLoanScheduleItem.installment_number.asc())
        .first()
    )
    if item is None:
        return None, None
    return item.due_date, max(0, (item.total_cents or 0) - (item.paid_cents or 0))


def _summary(db: Session, loan: PlatformLoan) -> LoanSummary:
    next_date, next_cents = _next_due(db, loan)
    return LoanSummary(
        id=loan.id,
        status=loan.status,
        principal_cents=loan.principal_cents,
        principal_balance_cents=loan.principal_balance_cents,
        annual_rate_bps=loan.annual_rate_bps,
        term_months=loan.term_months,
        currency=loan.currency,
        outstanding_cents=loan_payments.outstanding_cents(db, loan),
        next_due_date=next_date,
        next_due_cents=next_cents,
    )


# --- endpoints --------------------------------------------------------------


@router.get("", response_model=list[LoanSummary])
def list_loans(
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    """All loans whose application belongs to this patient."""
    loans = (
        db.query(PlatformLoan)
        .join(PlatformCreditApplication, PlatformLoan.application_id == PlatformCreditApplication.id)
        .filter(PlatformCreditApplication.patient_id == claims.patient_id)
        .all()
    )
    return [_summary(db, loan) for loan in loans]


@router.get("/{loan_id}", response_model=LoanDetail)
def get_loan(
    loan_id: UUID,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    loan = _load_authorized_loan(db, loan_id, claims)
    items = (
        db.query(PlatformLoanScheduleItem)
        .filter(PlatformLoanScheduleItem.loan_id == loan.id)
        .order_by(PlatformLoanScheduleItem.installment_number.asc())
        .all()
    )
    base = _summary(db, loan)
    return LoanDetail(
        **base.model_dump(),
        schedule=[
            ScheduleItemOut(
                installment_number=i.installment_number,
                due_date=i.due_date,
                total_cents=i.total_cents,
                paid_cents=i.paid_cents or 0,
                status=i.status,
            )
            for i in items
        ],
    )


@router.get("/{loan_id}/payments", response_model=list[PaymentOut])
def list_payments(
    loan_id: UUID,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    loan = _load_authorized_loan(db, loan_id, claims)
    payments = (
        db.query(PlatformLoanPayment)
        .filter(PlatformLoanPayment.loan_id == loan.id)
        .order_by(PlatformLoanPayment.received_at.desc())
        .all()
    )
    return [
        PaymentOut(
            id=p.id,
            amount_cents=p.amount_cents,
            received_at=p.received_at.isoformat() if p.received_at else "",
            method=p.method,
            external_ref=p.external_ref,
        )
        for p in payments
    ]


@router.post("/{loan_id}/payments", response_model=PayNowResponse, status_code=status.HTTP_202_ACCEPTED)
def pay_now(
    loan_id: UUID,
    body: PayNowBody,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    """Initiate a borrower payment (Zumrails collection). The payment settles
    out-of-band when the Zumrails webhook confirms it; this returns the
    in-flight transaction so the UI can poll/refresh."""
    loan = _load_authorized_loan(db, loan_id, claims)
    try:
        result = loan_payments.initiate_payment(db, loan, body.amount_cents)
    except loan_payments.PaymentValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except loan_payments.PaymentProviderUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    return PayNowResponse(**result)
