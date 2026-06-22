"""Admin collections & delinquency aging (read-only, Phase 1). M5.

Aging across the WHOLE book by DPD bucket. Charge-off, payment-plan, and
promise-to-pay are Phase 2 write actions. Bucketing mirrors the locked
delinquency tiers (current / late / 30-60-90-120) used elsewhere.
"""
from __future__ import annotations

from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.loan import PlatformLoan, PlatformLoanScheduleItem
from app.models.platform.patient import PlatformPatient

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])

# Bucket upper bounds (inclusive lower, exclusive upper). 0 = current.
_BUCKETS = [("current", 0, 1), ("1-29", 1, 30), ("30-59", 30, 60), ("60-89", 60, 90), ("90-119", 90, 120), ("120+", 120, 10**9)]


class AgingBucket(BaseModel):
    bucket: str
    count: int
    outstanding_cents: int


class AgingSummary(BaseModel):
    as_of: date
    buckets: list[AgingBucket]
    total_delinquent_count: int
    total_outstanding_cents: int


def _earliest_unpaid_due(db: Session, loan_id: UUID):
    row = (
        db.query(PlatformLoanScheduleItem.due_date)
        .filter(
            PlatformLoanScheduleItem.loan_id == loan_id,
            PlatformLoanScheduleItem.status.notin_(("paid", "waived")),
        )
        .order_by(PlatformLoanScheduleItem.installment_number.asc())
        .first()
    )
    return row[0] if row else None


def _bucket_for(dpd: int) -> str:
    for name, lo, hi in _BUCKETS:
        if lo <= dpd < hi:
            return name
    return "120+"


@router.get("/aging", response_model=AgingSummary)
def aging(db: Session = Depends(get_db), _user=Depends(get_current_user)):
    """DPD aging across all non-terminal loans, by bucket."""
    today = date.today()
    loans = (
        db.query(PlatformLoan)
        .filter(PlatformLoan.status.in_(("active", "delinquent")))
        .all()
    )
    agg: dict[str, dict[str, int]] = {name: {"count": 0, "out": 0} for name, _, _ in _BUCKETS}
    delinquent_count = 0
    total_out = 0
    for loan in loans:
        due = _earliest_unpaid_due(db, loan.id)
        dpd = max(0, (today - due).days) if due else 0
        bucket = _bucket_for(dpd)
        agg[bucket]["count"] += 1
        agg[bucket]["out"] += loan.principal_balance_cents
        if dpd > 0:
            delinquent_count += 1
            total_out += loan.principal_balance_cents
    return AgingSummary(
        as_of=today,
        buckets=[
            AgingBucket(bucket=name, count=agg[name]["count"], outstanding_cents=agg[name]["out"])
            for name, _, _ in _BUCKETS
        ],
        total_delinquent_count=delinquent_count,
        total_outstanding_cents=total_out,
    )


class CollectionsQueueRow(BaseModel):
    loan_id: UUID
    patient_name: str
    principal_balance_cents: int
    days_past_due: int
    bucket: str


@router.get("/queue", response_model=list[CollectionsQueueRow])
def queue(
    bucket: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Delinquent worklist, worst-first (highest DPD). Optional bucket filter.

    Phase 1 sorts by DPD; a risk/balance-weighted SCORED priority is Phase 4."""
    today = date.today()
    loans = (
        db.query(PlatformLoan, PlatformPatient)
        .filter(PlatformLoan.status.in_(("active", "delinquent")))
        .outerjoin(
            PlatformCreditApplication,
            PlatformLoan.application_id == PlatformCreditApplication.id,
        )
        .outerjoin(PlatformPatient, PlatformCreditApplication.patient_id == PlatformPatient.id)
        .all()
    )
    rows = []
    for loan, patient in loans:
        due = _earliest_unpaid_due(db, loan.id)
        dpd = max(0, (today - due).days) if due else 0
        if dpd <= 0:
            continue
        b = _bucket_for(dpd)
        if bucket and b != bucket:
            continue
        name = (
            " ".join(x for x in [patient.legal_first_name, patient.legal_last_name] if x).strip()
            if patient
            else "—"
        ) or "—"
        rows.append(
            CollectionsQueueRow(
                loan_id=loan.id,
                patient_name=name,
                principal_balance_cents=loan.principal_balance_cents,
                days_past_due=dpd,
                bucket=b,
            )
        )
    rows.sort(key=lambda r: r.days_past_due, reverse=True)
    return rows[:limit]
