"""Borrower portal — read-only loan-servicing API (Borrower Portal §4).

A logged-in borrower (patient) can see and understand their loan(s): balance,
schedule, payments, statements. Every route is patient-JWT authed via
``get_current_applicant`` and **scoped to ``claims.patient_id``** by joining
``platform_loans.application_id -> platform_credit_applications.patient_id``.

Scoping is the security boundary (spec §4): a loan whose patient != the caller
is **404, not 403** — we never reveal that someone else's loan exists. Migrated
loans (``application_id IS NULL``) resolve to no patient and so never surface.

The days-past-due / next-due computation mirrors the clinic loan-book
(``dashboard_loanbook.py``) for spec parity — same definitions, patient-scoped
queries instead of vendor-scoped. Money is integer cents throughout;
``annual_rate_bps`` is basis points (1% = 100 bps).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from app.api.applicant.v1.deps import ApplicantClaims, get_current_applicant
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanPayment,
    PlatformLoanScheduleItem,
    PlatformLoanStatement,
)

router = APIRouter(tags=["borrower-loans"])


# ---------------------------------------------------------------------------
# Response schemas — local to this module (DO NOT touch shared schemas).
# Field names mirror the TS contract in the spec (§4).
# ---------------------------------------------------------------------------


class BorrowerLoanSummary(BaseModel):
    loan_id: UUID
    status: str
    principal_cents: int
    principal_balance_cents: int
    currency: str
    disbursed_at: Optional[datetime] = None
    next_due_date: Optional[date] = None
    next_due_cents: Optional[int] = None
    days_past_due: int


class BorrowerLoans(BaseModel):
    loans: list[BorrowerLoanSummary]


class BorrowerLoanDetail(BorrowerLoanSummary):
    annual_rate_bps: int
    apr_percent: float
    term_months: int
    agreement_status: str
    disbursement_status: str
    amount_paid_cents: int
    installments_total: int
    installments_paid: int
    product_name: Optional[str] = None


class ScheduleRow(BaseModel):
    installment_number: int
    due_date: date
    principal_cents: int
    interest_cents: int
    total_cents: int
    paid_cents: int
    status: str


class LoanSchedule(BaseModel):
    rows: list[ScheduleRow]


class PaymentRow(BaseModel):
    amount_cents: int
    received_at: datetime
    method: str


class LoanPayments(BaseModel):
    rows: list[PaymentRow]


class StatementRow(BaseModel):
    period_start: date
    period_end: date
    opening_balance_cents: int
    closing_balance_cents: int
    principal_paid_cents: int
    interest_paid_cents: int
    generated_at: datetime


class LoanStatements(BaseModel):
    rows: list[StatementRow]


# ---------------------------------------------------------------------------
# Pure helpers (mirror dashboard_loanbook.py for spec parity)
# ---------------------------------------------------------------------------


def _next_due_item(
    schedule_items: Iterable[PlatformLoanScheduleItem],
) -> Optional[PlatformLoanScheduleItem]:
    """Next payment (spec §6): earliest schedule row whose status is not in
    {paid, waived}, by ``installment_number``."""
    outstanding = [s for s in schedule_items if s.status not in ("paid", "waived")]
    if not outstanding:
        return None
    return min(outstanding, key=lambda s: s.installment_number)


def _days_past_due(
    next_item: Optional[PlatformLoanScheduleItem], as_of: date
) -> int:
    """days_past_due (spec §6) = ``max(0, today - next-payment due_date)``.

    The next payment is the earliest unpaid installment; if it's not yet due
    (or there is none), the loan is current -> 0.
    """
    if next_item is None:
        return 0
    return max(0, (as_of - next_item.due_date).days)


def _to_summary(loan: PlatformLoan, as_of: date) -> BorrowerLoanSummary:
    nxt = _next_due_item(loan.schedule)
    return BorrowerLoanSummary(
        loan_id=loan.id,
        status=loan.status,
        principal_cents=loan.principal_cents,
        principal_balance_cents=loan.principal_balance_cents,
        currency=loan.currency,
        disbursed_at=loan.disbursed_at,
        next_due_date=nxt.due_date if nxt else None,
        next_due_cents=(nxt.total_cents - nxt.paid_cents) if nxt else None,
        days_past_due=_days_past_due(nxt, as_of),
    )


# ---------------------------------------------------------------------------
# Scoping helpers — the ONLY path to a loan is patient-scoped.
# ---------------------------------------------------------------------------


def _patient_loans_query(db: Session, patient_id: UUID):
    """Base query for ONE patient's loans, scoped via the application join.

    The join to ``platform_credit_applications`` + the ``patient_id`` filter is
    the ONLY scoping boundary (loans carry no patient_id) — every caller starts
    here so scoping can't be forgotten. Migrated loans (application_id NULL)
    never match the inner join and so never surface.
    """
    return (
        db.query(PlatformLoan)
        .join(
            PlatformCreditApplication,
            PlatformLoan.application_id == PlatformCreditApplication.id,
        )
        .filter(PlatformCreditApplication.patient_id == patient_id)
    )


def _get_owned_loan(db: Session, loan_id: UUID, patient_id: UUID) -> PlatformLoan:
    """Fetch a loan owned by ``patient_id`` or raise 404.

    404 (not 403) by design (spec §4): we don't reveal that a loan exists when
    it belongs to someone else. A nonexistent loan and a not-yours loan are
    indistinguishable to the caller.
    """
    loan = (
        _patient_loans_query(db, patient_id)
        .options(joinedload(PlatformLoan.schedule))
        .filter(PlatformLoan.id == loan_id)
        .first()
    )
    if loan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Loan not found")
    return loan


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/loans", response_model=BorrowerLoans)
def list_loans(
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
) -> BorrowerLoans:
    """The caller's loans (spec §4.1). Scoped to ``claims.patient_id``."""
    loans = (
        _patient_loans_query(db, claims.patient_id)
        .options(joinedload(PlatformLoan.schedule))
        .order_by(PlatformLoan.created_at.desc(), PlatformLoan.id.desc())
        .all()
    )
    as_of = datetime.now(timezone.utc).date()
    return BorrowerLoans(loans=[_to_summary(loan, as_of) for loan in loans])


@router.get("/loans/{loan_id}", response_model=BorrowerLoanDetail)
def get_loan(
    loan_id: UUID,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
) -> BorrowerLoanDetail:
    """Full detail for one owned loan (spec §4.2). 404 if not the caller's."""
    loan = _get_owned_loan(db, loan_id, claims.patient_id)
    as_of = datetime.now(timezone.utc).date()
    summary = _to_summary(loan, as_of)

    amount_paid_cents = sum(p.amount_cents for p in loan.payments)
    installments_total = len(loan.schedule)
    installments_paid = sum(1 for s in loan.schedule if s.status == "paid")

    product_name: Optional[str] = None
    if loan.application_id is not None:
        product_name = (
            db.query(PlatformCreditProduct.name)
            .join(
                PlatformCreditApplication,
                PlatformCreditApplication.credit_product_id == PlatformCreditProduct.id,
            )
            .filter(PlatformCreditApplication.id == loan.application_id)
            .scalar()
        )

    return BorrowerLoanDetail(
        **summary.model_dump(),
        annual_rate_bps=loan.annual_rate_bps,
        apr_percent=loan.annual_rate_bps / 100,
        term_months=loan.term_months,
        agreement_status=loan.agreement_status,
        disbursement_status=loan.disbursement_status,
        amount_paid_cents=amount_paid_cents,
        installments_total=installments_total,
        installments_paid=installments_paid,
        product_name=product_name,
    )


@router.get("/loans/{loan_id}/schedule", response_model=LoanSchedule)
def get_schedule(
    loan_id: UUID,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
) -> LoanSchedule:
    """Amortization schedule (spec §4.3), ordered by installment_number."""
    _get_owned_loan(db, loan_id, claims.patient_id)  # 404 if not owned
    rows = (
        db.query(PlatformLoanScheduleItem)
        .filter(PlatformLoanScheduleItem.loan_id == loan_id)
        .order_by(PlatformLoanScheduleItem.installment_number.asc())
        .all()
    )
    return LoanSchedule(
        rows=[
            ScheduleRow(
                installment_number=r.installment_number,
                due_date=r.due_date,
                principal_cents=r.principal_cents,
                interest_cents=r.interest_cents,
                total_cents=r.total_cents,
                paid_cents=r.paid_cents,
                status=r.status,
            )
            for r in rows
        ]
    )


@router.get("/loans/{loan_id}/payments", response_model=LoanPayments)
def get_payments(
    loan_id: UUID,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
) -> LoanPayments:
    """Payment history (spec §4.4), most-recent first (received_at desc)."""
    _get_owned_loan(db, loan_id, claims.patient_id)  # 404 if not owned
    rows = (
        db.query(PlatformLoanPayment)
        .filter(PlatformLoanPayment.loan_id == loan_id)
        .order_by(PlatformLoanPayment.received_at.desc())
        .all()
    )
    return LoanPayments(
        rows=[
            PaymentRow(
                amount_cents=p.amount_cents,
                received_at=p.received_at,
                method=p.method,
            )
            for p in rows
        ]
    )


@router.get("/loans/{loan_id}/statements", response_model=LoanStatements)
def get_statements(
    loan_id: UUID,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
) -> LoanStatements:
    """Periodic statements (spec §4.5), oldest period first."""
    _get_owned_loan(db, loan_id, claims.patient_id)  # 404 if not owned
    rows = (
        db.query(PlatformLoanStatement)
        .filter(PlatformLoanStatement.loan_id == loan_id)
        .order_by(PlatformLoanStatement.period_start.asc())
        .all()
    )
    return LoanStatements(
        rows=[
            StatementRow(
                period_start=s.period_start,
                period_end=s.period_end,
                opening_balance_cents=s.opening_balance_cents,
                closing_balance_cents=s.closing_balance_cents,
                principal_paid_cents=s.principal_paid_cents,
                interest_paid_cents=s.interest_paid_cents,
                generated_at=s.generated_at,
            )
            for s in rows
        ]
    )
