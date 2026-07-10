"""Admin loan book + servicing detail (read-only, Phase 1). M3.

Whole-book loan list (no vendor scoping) with status/DPD, and a detail view
reusing the existing loan-servicing status builder. Servicing WRITE actions
(payments, reschedule, payoff) are Phase 2.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanDelinquencySnapshot,
    PlatformLoanPayment,
    PlatformLoanScheduleItem,
    PlatformLoanStatement,
)
from app.models.platform.patient import PlatformPatient
from app.services import delinquency_buckets
from app.services import loan_ledger as ledger_service
from app.services.loan_servicing import get_loan_status

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])


class AdminLoanRow(BaseModel):
    loan_id: UUID
    application_id: Optional[UUID] = None
    patient_name: str
    vendor_name: str
    principal_cents: int
    principal_balance_cents: int
    status: str
    disbursed_at: Optional[datetime] = None
    days_past_due: int
    # WS-H: last month-end snapshot bucket + insolvency classification.
    current_bucket: str = "current"
    insolvency_status: Optional[str] = None


def _patient_name(p) -> str:
    if p is None:
        return "—"
    return " ".join(x for x in [p.legal_first_name, p.legal_last_name] if x).strip() or "—"


def _days_past_due(db: Session, loan_id: UUID) -> int:
    row = (
        db.query(PlatformLoanScheduleItem.due_date)
        .filter(
            PlatformLoanScheduleItem.loan_id == loan_id,
            PlatformLoanScheduleItem.status.notin_(("paid", "waived")),
        )
        .order_by(PlatformLoanScheduleItem.installment_number.asc())
        .first()
    )
    if row is None or row[0] is None:
        return 0
    return max(0, (date.today() - row[0]).days)


@router.get("", response_model=list[AdminLoanRow])
def list_loans(
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Whole-book loan list, newest first. Optional status filter."""
    q = (
        db.query(PlatformLoan, PlatformPatient, Vendor.business_name)
        .outerjoin(
            PlatformCreditApplication,
            PlatformLoan.application_id == PlatformCreditApplication.id,
        )
        .outerjoin(PlatformPatient, PlatformCreditApplication.patient_id == PlatformPatient.id)
        .outerjoin(Vendor, PlatformCreditApplication.vendor_id == Vendor.id)
    )
    if status_filter:
        q = q.filter(PlatformLoan.status == status_filter)
    rows = q.order_by(PlatformLoan.created_at.desc()).limit(limit).all()
    return [
        AdminLoanRow(
            loan_id=loan.id,
            application_id=loan.application_id,
            patient_name=_patient_name(patient),
            vendor_name=vendor_name or "—",
            principal_cents=loan.principal_cents,
            principal_balance_cents=loan.principal_balance_cents,
            status=loan.status,
            disbursed_at=loan.disbursed_at,
            days_past_due=_days_past_due(db, loan.id),
            current_bucket=loan.current_bucket or "current",
            insolvency_status=loan.insolvency_status,
        )
        for loan, patient, vendor_name in rows
    ]


def _delinquency_block(loan: PlatformLoan, today: date) -> dict:
    """WS-H: live delinquency view for one loan (no extra queries — walks the
    loaded schedule).

    * ``current_bucket`` — the last MONTH-END snapshot assignment (buckets
      move only at month-end).
    * ``effective_bucket`` — what collections should treat the loan as right
      now: insolvency/written_off override immediately, and a FULL catch-up
      (no past-due unpaid installment) cures to ``current`` immediately.
    * ``days_past_due`` / ``amount_past_due_cents`` — LIVE values (shown
      separately from the month-end bucket by design).
    """
    items = sorted(loan.schedule or [], key=lambda s: s.installment_number)
    not_owed = delinquency_buckets.NOT_OWED_STATUSES
    past_due = [
        s
        for s in items
        if s.status not in not_owed and s.paid_cents < s.total_cents and s.due_date < today
    ]
    dpd = max(((today - s.due_date).days for s in past_due), default=0)
    amount_past_due = sum(s.total_cents - s.paid_cents for s in past_due)
    return {
        "current_bucket": loan.current_bucket,
        "effective_bucket": delinquency_buckets.effective_bucket(
            loan.status, loan.insolvency_status, loan.current_bucket, bool(past_due)
        ),
        "days_past_due": dpd,
        "amount_past_due_cents": amount_past_due,
        "insolvency_status": loan.insolvency_status,
        "insolvency_marked_at": (
            loan.insolvency_marked_at.isoformat() if loan.insolvency_marked_at else None
        ),
        "insolvency_marked_by": loan.insolvency_marked_by,
    }


@router.get("/{loan_id}")
def get_loan(
    loan_id: UUID,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Full servicing detail — reuses loan_servicing.get_loan_status (schedule,
    payments, balances) plus the WS-H delinquency-bucket block (month-end
    bucket, live DPD, insolvency, snapshot history). 404 if the loan doesn't
    exist."""
    detail = get_loan_status(db, loan_id)
    if detail is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Loan not found")

    loan = db.query(PlatformLoan).filter(PlatformLoan.id == loan_id).first()
    block = _delinquency_block(loan, date.today())
    snapshots = (
        db.query(PlatformLoanDelinquencySnapshot)
        .filter(PlatformLoanDelinquencySnapshot.loan_id == loan_id)
        .order_by(PlatformLoanDelinquencySnapshot.snapshot_month.desc())
        .limit(12)
        .all()
    )
    block["snapshots"] = [
        {
            "snapshot_month": s.snapshot_month.isoformat(),
            "bucket": s.bucket,
            "days_past_due": s.days_past_due,
            "amount_past_due_cents": s.amount_past_due_cents,
            "outstanding_principal_cents": s.outstanding_principal_cents,
            "bureau_reportable": s.bureau_reportable,
            "snapshotted_at": s.snapshotted_at.isoformat() if s.snapshotted_at else None,
        }
        for s in snapshots
    ]
    detail["delinquency"] = block
    return detail


class InsolvencyBody(BaseModel):
    """Mark (or clear) a loan's segregated insolvency classification.

    ``status='none'`` clears — admin-only. A comment is MANDATORY either way
    (the determination is a human judgement; the why must be on the record)."""

    status: Literal["consumer_proposal", "bankruptcy", "credit_counseling", "none"]
    comment: str = Field(min_length=3, max_length=2000)


@router.post("/{loan_id}/insolvency")
def set_loan_insolvency(
    loan_id: UUID,
    body: InsolvencyBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin", "staff")),
):
    """Manually mark a loan insolvent (consumer proposal / bankruptcy / credit
    counseling) or clear the marking (WS-H).

    Insolvency OVERRIDES bucket derivation (the segregated insolvency
    portfolio, per Dave) — marking flips ``current_bucket`` immediately;
    clearing re-derives from the live schedule. Staff may mark; CLEARING
    requires admin (undoing a legal-status determination is the more dangerous
    direction). Fully audited via ``platform_events``.
    """
    loan = db.query(PlatformLoan).filter(PlatformLoan.id == loan_id).first()
    if loan is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Loan not found")

    if body.status == "none":
        roles = {r.role.name for r in user.roles}
        if "admin" not in roles:
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail="Clearing an insolvency marking requires the admin role.",
            )

    try:
        delinquency_buckets.set_insolvency(
            db, loan, body.status, body.comment, str(getattr(user, "id", "") or "unknown")
        )
    except ValueError as exc:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    db.commit()
    return {
        "loan_id": str(loan.id),
        "insolvency_status": loan.insolvency_status,
        "current_bucket": loan.current_bucket,
    }


def _require_loan(db: Session, loan_id: UUID) -> None:
    if db.query(PlatformLoan.id).filter(PlatformLoan.id == loan_id).first() is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Loan not found")


class ScheduleRow(BaseModel):
    installment_number: int
    due_date: date
    principal_cents: int
    interest_cents: int
    total_cents: int
    paid_cents: int
    status: str


class PaymentRow(BaseModel):
    amount_cents: int
    received_at: datetime
    method: str
    external_ref: Optional[str] = None


class StatementRow(BaseModel):
    period_start: date
    period_end: date
    opening_balance_cents: int
    principal_paid_cents: int
    interest_paid_cents: int
    closing_balance_cents: int


@router.get("/{loan_id}/schedule", response_model=list[ScheduleRow])
def loan_schedule(loan_id: UUID, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    """The full amortization schedule — the calculation engine made visible."""
    _require_loan(db, loan_id)
    return (
        db.query(PlatformLoanScheduleItem)
        .filter(PlatformLoanScheduleItem.loan_id == loan_id)
        .order_by(PlatformLoanScheduleItem.installment_number)
        .all()
    )


@router.get("/{loan_id}/payments", response_model=list[PaymentRow])
def loan_payments(loan_id: UUID, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    """The payment ledger (cash receipts), newest first."""
    _require_loan(db, loan_id)
    return (
        db.query(PlatformLoanPayment)
        .filter(PlatformLoanPayment.loan_id == loan_id)
        .order_by(PlatformLoanPayment.received_at.desc())
        .all()
    )


@router.get("/{loan_id}/ledger")
def loan_ledger_view(
    loan_id: UUID,
    as_of: Optional[date] = None,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """The immutable money ledger (WS-A): every transaction with its allocation
    split, dual dates (effective vs processing), reference number, and running
    category balances — plus the actuals-engine balance view at ``as_of``
    (outstanding principal + interest due + fees due + add-on == payoff)."""
    loan = db.query(PlatformLoan).filter(PlatformLoan.id == loan_id).first()
    if loan is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Loan not found")
    return ledger_service.ledger_view(loan, as_of or date.today())


@router.get("/{loan_id}/statements", response_model=list[StatementRow])
def loan_statements(loan_id: UUID, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    """Billing statements (opening/closing balance + principal/interest split)."""
    _require_loan(db, loan_id)
    return (
        db.query(PlatformLoanStatement)
        .filter(PlatformLoanStatement.loan_id == loan_id)
        .order_by(PlatformLoanStatement.period_start.desc())
        .all()
    )
