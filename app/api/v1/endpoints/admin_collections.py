"""Admin collections & delinquency aging (read-only, Phase 1). M5.

Aging across the WHOLE book by DPD bucket. Charge-off, payment-plan, and
promise-to-pay are Phase 2 write actions. Bucketing mirrors the locked
delinquency tiers (current / late / 30-60-90-120) used elsewhere.

WS-H layers Dave's MONTH-END bucket state machine on top: the legacy rolling
DPD buckets below are kept intact (the nightly aging job still owns them);
``/aging`` additionally returns month-end bucket aggregates + the debt-roll
comparison from ``platform_loan_delinquency_snapshots``, and the queue rows
carry the month-end bucket + insolvency classification.
"""
from __future__ import annotations

from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.models.platform.collections_work import PlatformCollectorAssignment
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanDelinquencySnapshot,
    PlatformLoanScheduleItem,
)
from app.models.platform.patient import PlatformPatient
from app.services.delinquency_buckets import compute_roll_rates, effective_bucket

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])

# Bucket upper bounds (inclusive lower, exclusive upper). 0 = current.
_BUCKETS = [("current", 0, 1), ("1-29", 1, 30), ("30-59", 30, 60), ("60-89", 60, 90), ("90-119", 90, 120), ("120+", 120, 10**9)]


class AgingBucket(BaseModel):
    bucket: str
    count: int
    outstanding_cents: int


class MonthEndBucketAggregate(BaseModel):
    """One month-end bucket's roll-up (WS-H snapshots)."""

    bucket: str
    count: int
    outstanding_principal_cents: int
    amount_past_due_cents: int


class RollRate(BaseModel):
    """Debt-roll between two snapshot months for one ladder pair (Dave: "the
    percentage of current month late that roll into a new month and become
    POT 30s"). ``roll_rate_bps`` is integer basis points (10_000 = 100%)."""

    from_bucket: str
    to_bucket: str
    base_count: int
    rolled_count: int
    roll_rate_bps: int


class MonthEndBucketReport(BaseModel):
    """Month-end snapshot aggregates: latest snapshot month vs the prior one,
    plus per-ladder-pair debt-roll rates. ``None`` until the first snapshot
    job run."""

    snapshot_month: date
    buckets: list[MonthEndBucketAggregate]
    prior_snapshot_month: Optional[date] = None
    prior_buckets: list[MonthEndBucketAggregate] = []
    roll_rates: list[RollRate] = []


class AgingSummary(BaseModel):
    as_of: date
    buckets: list[AgingBucket]
    total_delinquent_count: int
    total_outstanding_cents: int
    # WS-H: month-end bucket state machine aggregates (None before the first
    # snapshot run — the legacy rolling-DPD buckets above are unaffected).
    month_end_buckets: Optional[MonthEndBucketReport] = None


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


def _bucket_aggregates_for_month(db: Session, month: date) -> list[MonthEndBucketAggregate]:
    rows = (
        db.query(
            PlatformLoanDelinquencySnapshot.bucket,
            func.count(PlatformLoanDelinquencySnapshot.id),
            func.coalesce(
                func.sum(PlatformLoanDelinquencySnapshot.outstanding_principal_cents), 0
            ),
            func.coalesce(
                func.sum(PlatformLoanDelinquencySnapshot.amount_past_due_cents), 0
            ),
        )
        .filter(PlatformLoanDelinquencySnapshot.snapshot_month == month)
        .group_by(PlatformLoanDelinquencySnapshot.bucket)
        .all()
    )
    return [
        MonthEndBucketAggregate(
            bucket=bucket,
            count=count,
            outstanding_principal_cents=outstanding,
            amount_past_due_cents=past_due,
        )
        for bucket, count, outstanding, past_due in rows
    ]


def _loan_buckets_for_month(db: Session, month: date) -> dict:
    return dict(
        db.query(
            PlatformLoanDelinquencySnapshot.loan_id,
            PlatformLoanDelinquencySnapshot.bucket,
        )
        .filter(PlatformLoanDelinquencySnapshot.snapshot_month == month)
        .all()
    )


def _month_end_bucket_report(db: Session) -> Optional[MonthEndBucketReport]:
    """WS-H aggregates: latest snapshot month vs prior month + debt-roll %."""
    months = [
        m
        for (m,) in db.query(PlatformLoanDelinquencySnapshot.snapshot_month)
        .distinct()
        .order_by(PlatformLoanDelinquencySnapshot.snapshot_month.desc())
        .limit(2)
        .all()
    ]
    if not months:
        return None
    latest = months[0]
    prior = months[1] if len(months) > 1 else None
    report = MonthEndBucketReport(
        snapshot_month=latest,
        buckets=_bucket_aggregates_for_month(db, latest),
        prior_snapshot_month=prior,
    )
    if prior is not None:
        report.prior_buckets = _bucket_aggregates_for_month(db, prior)
        report.roll_rates = [
            RollRate(**r)
            for r in compute_roll_rates(
                _loan_buckets_for_month(db, prior), _loan_buckets_for_month(db, latest)
            )
        ]
    return report


@router.get("/aging", response_model=AgingSummary)
def aging(db: Session = Depends(get_db), _user=Depends(get_current_user)):
    """DPD aging across all non-terminal loans, by bucket — plus the WS-H
    month-end bucket aggregates and debt-roll comparison (once snapshots
    exist)."""
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
        month_end_buckets=_month_end_bucket_report(db),
    )


class CollectionsQueueRow(BaseModel):
    loan_id: UUID
    patient_name: str
    principal_balance_cents: int
    days_past_due: int
    bucket: str
    # WS-H month-end state machine: the loan's effective month-end bucket
    # (last snapshot, with the immediate insolvency/written-off/full-cure
    # overrides applied) + its insolvency classification, if any.
    month_end_bucket: str = "current"
    insolvency_status: Optional[str] = None
    # WS-C collector assignment surfacing (Turnkey's "A" avatar column).
    assigned_collector_user_id: Optional[UUID] = None
    assignment_tier: Optional[str] = None


@router.get("/queue", response_model=list[CollectionsQueueRow])
def queue(
    bucket: Optional[str] = Query(None),
    month_end_bucket: Optional[str] = Query(None),
    include_insolvency: bool = Query(
        False,
        description=(
            "WS-C: the insolvency portfolio is SEGREGATED from normal "
            "collections (Dave) — excluded here by default; use "
            "/admin/collections/insolvency (or pass true / filter "
            "month_end_bucket=insolvency explicitly)."
        ),
    ),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Delinquent worklist, worst-first (highest DPD). Optional filters on the
    legacy rolling-DPD ``bucket`` or the WS-H ``month_end_bucket``.

    WS-C: insolvency rows are segregated OUT by default (Dave: "essentially
    going to be separated out of those other collection queues") — the
    dedicated ``/insolvency`` endpoint serves that portfolio. Explicitly
    filtering ``month_end_bucket=insolvency`` (or ``include_insolvency=true``)
    still returns them here for back-compat.

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
    # WS-C: surface the active collector assignment per loan (one query).
    assignments = {
        a.loan_id: a
        for a in db.query(PlatformCollectorAssignment)
        .filter(PlatformCollectorAssignment.active.is_(True))
        .all()
    }
    rows = []
    for loan, patient in loans:
        due = _earliest_unpaid_due(db, loan.id)
        dpd = max(0, (today - due).days) if due else 0
        me_bucket = effective_bucket(
            loan.status, loan.insolvency_status, loan.current_bucket, dpd > 0
        )
        # WS-C: the insolvency portfolio is SEGREGATED out of the normal
        # queue (Dave) unless explicitly asked for — the dedicated
        # /insolvency endpoint maintains it (sorted principal-then-DPD).
        if me_bucket == "insolvency" and not (
            include_insolvency or month_end_bucket == "insolvency"
        ):
            continue
        # Insolvent accounts stay in the worklist even at 0 DPD — Dave's
        # segregated insolvency portfolio is maintained, not dropped.
        if dpd <= 0 and me_bucket != "insolvency":
            continue
        b = _bucket_for(dpd)
        if bucket and b != bucket:
            continue
        if month_end_bucket and me_bucket != month_end_bucket:
            continue
        name = (
            " ".join(x for x in [patient.legal_first_name, patient.legal_last_name] if x).strip()
            if patient
            else "—"
        ) or "—"
        assignment = assignments.get(loan.id)
        rows.append(
            CollectionsQueueRow(
                loan_id=loan.id,
                patient_name=name,
                principal_balance_cents=loan.principal_balance_cents,
                days_past_due=dpd,
                bucket=b,
                month_end_bucket=me_bucket,
                insolvency_status=loan.insolvency_status,
                assigned_collector_user_id=(
                    assignment.collector_user_id if assignment else None
                ),
                assignment_tier=str(assignment.tier) if assignment else None,
            )
        )
    rows.sort(key=lambda r: r.days_past_due, reverse=True)
    return rows[:limit]
