"""Vendor dashboard — loan book & payment performance (Phase 2).

Implements the loan-book slice of the vendor performance dashboard
(``docs/vendor_dashboard_spec.md`` §3.1 loan_book/payments blocks + §3.4 the
paginated loan-book list). All KPIs follow the precise definitions in §4,
including the LOCKED delinquency tiers (current/late/delinquent, grace window =
``settings.GRACE_DAYS``).

SCOPING (spec §2 gotcha #2): ``platform_loans`` has **no** ``vendor_id``. Every
query here reaches the vendor by joining
``platform_loans.application_id -> platform_credit_applications.vendor_id`` and
filtering on the caller's ``principal.vendor_id``. There is no other path to a
loan, so a vendor can only ever see its own loans — no cross-vendor leakage.

This module exposes three callables the later ``/dashboard/overview`` endpoint
will import unchanged:

  * ``loan_book_block(db, vendor_id) -> dict``     (spec §3.1 ``loan_book``)
  * ``payments_block(db, vendor_id, frm, to) -> dict`` (spec §3.1 ``payments``)
  * ``days_past_due(schedule_items, as_of) -> int`` (spec §4 dpd, pure)

Money is integer cents throughout.
"""
from __future__ import annotations

import base64
import binascii
from datetime import date, datetime, timezone
from typing import Iterable, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.services.clinic_permissions import require_clinic_permission
from app.core.config import settings
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanPayment,
    PlatformLoanScheduleItem,
)
from app.models.platform.patient import PlatformPatient

router = APIRouter(tags=["clinic-dashboard"])


# ---------------------------------------------------------------------------
# Response schemas (local to this module — DO NOT touch the shared schemas.py).
# Field names mirror the TS contract in the spec (VendorLoanRow / VendorLoanBook).
# ---------------------------------------------------------------------------


class VendorLoanRow(BaseModel):
    loan_id: UUID
    application_id: UUID
    patient_name: str
    principal_cents: int
    principal_balance_cents: int
    status: str
    disbursement_status: str
    disbursed_at: Optional[datetime] = None
    next_due_date: Optional[date] = None
    next_due_cents: Optional[int] = None
    days_past_due: int


class VendorLoanBook(BaseModel):
    rows: list[VendorLoanRow]
    next_cursor: Optional[str] = None


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _patient_name(patient) -> str:
    """Mirror the applications-endpoint convention for a display name."""
    if patient is None:
        return "Unknown patient"
    parts = [
        getattr(patient, "legal_first_name", None),
        getattr(patient, "legal_last_name", None),
    ]
    name = " ".join(p for p in parts if p).strip()
    return name or "Unknown patient"


def days_past_due(
    schedule_items: Iterable[PlatformLoanScheduleItem], as_of: Optional[date] = None
) -> int:
    """Days past due for a loan (spec §4 ``dpd``), PURE / DB-free.

    dpd = ``as_of - earliest unpaid (overdue) schedule item due_date``; ``0`` if
    nothing is overdue. "Unpaid" = a schedule item whose status is not in
    {paid, waived} (i.e. scheduled / partial / late) — any uncovered balance
    past its due date is past due. Future-dated unpaid items don't count.
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc).date()

    earliest_overdue: Optional[date] = None
    for item in schedule_items:
        if item.status in ("paid", "waived"):
            continue
        if item.due_date >= as_of:
            continue
        if earliest_overdue is None or item.due_date < earliest_overdue:
            earliest_overdue = item.due_date

    if earliest_overdue is None:
        return 0
    return (as_of - earliest_overdue).days


def delinquency_tier(dpd: int, grace_days: Optional[int] = None) -> str:
    """Map a days-past-due value to a LOCKED delinquency tier (spec §4).

      * ``current``    — dpd == 0
      * ``late``       — 1 <= dpd <= GRACE_DAYS  (also covers 16..29; "delinquent"
                          only begins at 30 to align with bureau reporting)
      * ``delinquent`` — dpd >= 30  (bureau-reportable; bucketed elsewhere)

    Note the spec gap between GRACE_DAYS (15) and 30 is still counted as
    ``late`` — delinquency starts at 30, not GRACE_DAYS+1.
    """
    if grace_days is None:
        grace_days = settings.GRACE_DAYS
    if dpd <= 0:
        return "current"
    if dpd >= 30:
        return "delinquent"
    return "late"


def _next_due_item(
    schedule_items: Iterable[PlatformLoanScheduleItem],
) -> Optional[PlatformLoanScheduleItem]:
    """The next outstanding installment (oldest unpaid by installment_number)."""
    outstanding = [s for s in schedule_items if s.status not in ("paid", "waived")]
    if not outstanding:
        return None
    return min(outstanding, key=lambda s: s.installment_number)


def _vendor_loans_query(db: Session, vendor_id: UUID):
    """Base query for ONE vendor's loans, scoped via the application join.

    The ``join`` to ``platform_credit_applications`` + the ``vendor_id`` filter
    is the ONLY scoping boundary (loans carry no vendor_id) — every caller in
    this module starts here so scoping can't be forgotten.
    """
    return (
        db.query(PlatformLoan)
        .join(
            PlatformCreditApplication,
            PlatformLoan.application_id == PlatformCreditApplication.id,
        )
        .filter(PlatformCreditApplication.vendor_id == vendor_id)
    )


# ---------------------------------------------------------------------------
# Overview blocks (imported by the later /dashboard/overview endpoint)
# ---------------------------------------------------------------------------


def loan_book_block(db: Session, vendor_id: UUID) -> dict:
    """Aggregate loan-book stats for ONE vendor (spec §3.1 ``loan_book``).

    Returns a dict with EXACTLY these keys (all integer cents for money):
      * ``active_count``                    — loans with status='active'
      * ``active_principal_balance_cents``  — Σ principal_balance over active loans
      * ``disbursed_count``                 — loans with disbursed_at set
      * ``disbursed_amount_cents``          — Σ principal over disbursed loans
      * ``paid_off_count``                  — loans with status='paid_off'
      * ``delinquent_count``                — loans with status='delinquent'

    "Disbursed" is keyed off ``disbursed_at IS NOT NULL`` (the moment funds went
    out), not the in-flight ``disbursement_status`` lifecycle — a loan counts as
    disbursed once and stays counted even after it moves to active/paid_off.
    """
    base = _vendor_loans_query(db, vendor_id)

    active_count = base.filter(PlatformLoan.status == "active").count()
    active_balance = (
        base.with_entities(
            func.coalesce(func.sum(PlatformLoan.principal_balance_cents), 0)
        )
        .filter(PlatformLoan.status == "active")
        .scalar()
    )

    disbursed_count = base.filter(PlatformLoan.disbursed_at.isnot(None)).count()
    disbursed_amount = (
        base.with_entities(func.coalesce(func.sum(PlatformLoan.principal_cents), 0))
        .filter(PlatformLoan.disbursed_at.isnot(None))
        .scalar()
    )

    paid_off_count = base.filter(PlatformLoan.status == "paid_off").count()
    delinquent_count = base.filter(PlatformLoan.status == "delinquent").count()

    return {
        "active_count": int(active_count),
        "active_principal_balance_cents": int(active_balance or 0),
        "disbursed_count": int(disbursed_count),
        "disbursed_amount_cents": int(disbursed_amount or 0),
        "paid_off_count": int(paid_off_count),
        "delinquent_count": int(delinquent_count),
    }


def payments_block(db: Session, vendor_id: UUID, frm: datetime, to: datetime) -> dict:
    """Payment-performance stats for ONE vendor over ``[frm, to]`` (spec §3.1
    ``payments``, KPI defs §4).

    Returns a dict with EXACTLY these keys:
      * ``on_time_rate``      — (paid schedule items with due_date in window) ÷
                                (all due schedule items in window); ``None`` if
                                no items were due in the window. A schedule item
                                counts as on-time when its status == 'paid'
                                ('late'/'partial'/'scheduled' do not).
      * ``late_count``        — distinct vendor loans whose current dpd puts them
                                in the ``late`` tier (1..GRACE..<30).
      * ``delinquent_count``  — distinct vendor loans currently in the
                                ``delinquent`` tier (dpd >= 30).
      * ``collected_cents``   — Σ payment.amount_cents received in the window.

    ``frm``/``to`` are tz-aware datetimes (the dashboard window). The schedule
    "window" compares on ``due_date`` (a DATE) against the window's date bounds.
    late/delinquent counts are a point-in-time snapshot (current dpd), not
    windowed — they reflect the book's state now, matching the spec's overview
    intent.
    """
    base = _vendor_loans_query(db, vendor_id)
    frm_date = frm.date()
    to_date = to.date()

    # --- on_time_rate: schedule items DUE in the window -----------------------
    sched_base = (
        db.query(PlatformLoanScheduleItem)
        .join(PlatformLoan, PlatformLoanScheduleItem.loan_id == PlatformLoan.id)
        .join(
            PlatformCreditApplication,
            PlatformLoan.application_id == PlatformCreditApplication.id,
        )
        .filter(
            PlatformCreditApplication.vendor_id == vendor_id,
            PlatformLoanScheduleItem.due_date >= frm_date,
            PlatformLoanScheduleItem.due_date <= to_date,
        )
    )
    due_total = sched_base.count()
    paid_on = sched_base.filter(PlatformLoanScheduleItem.status == "paid").count()
    on_time_rate = (paid_on / due_total) if due_total else None

    # --- collected_cents: payments RECEIVED in the window --------------------
    collected = (
        db.query(func.coalesce(func.sum(PlatformLoanPayment.amount_cents), 0))
        .join(PlatformLoan, PlatformLoanPayment.loan_id == PlatformLoan.id)
        .join(
            PlatformCreditApplication,
            PlatformLoan.application_id == PlatformCreditApplication.id,
        )
        .filter(
            PlatformCreditApplication.vendor_id == vendor_id,
            PlatformLoanPayment.received_at >= frm,
            PlatformLoanPayment.received_at <= to,
        )
        .scalar()
    )

    # --- late / delinquent counts: current-dpd tier per loan -----------------
    as_of = datetime.now(timezone.utc).date()
    loans = base.options(joinedload(PlatformLoan.schedule)).all()
    late_count = 0
    delinquent_count = 0
    for loan in loans:
        tier = delinquency_tier(days_past_due(loan.schedule, as_of))
        if tier == "late":
            late_count += 1
        elif tier == "delinquent":
            delinquent_count += 1

    return {
        "on_time_rate": on_time_rate,
        "late_count": late_count,
        "delinquent_count": delinquent_count,
        "collected_cents": int(collected or 0),
    }


# ---------------------------------------------------------------------------
# Cursor pagination on (created_at, id)
# ---------------------------------------------------------------------------


def _encode_cursor(created_at: datetime, loan_id: UUID) -> str:
    raw = f"{created_at.isoformat()}|{loan_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        created_raw, id_raw = raw.split("|", 1)
        return datetime.fromisoformat(created_raw), UUID(id_raw)
    except (ValueError, binascii.Error) as exc:  # malformed cursor -> 422
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid cursor",
        ) from exc


def _to_row(loan: PlatformLoan, patient, as_of: date) -> VendorLoanRow:
    nxt = _next_due_item(loan.schedule)
    return VendorLoanRow(
        loan_id=loan.id,
        application_id=loan.application_id,
        patient_name=_patient_name(patient),
        principal_cents=loan.principal_cents,
        principal_balance_cents=loan.principal_balance_cents,
        status=loan.status,
        disbursement_status=loan.disbursement_status,
        disbursed_at=loan.disbursed_at,
        next_due_date=nxt.due_date if nxt else None,
        next_due_cents=(nxt.total_cents - nxt.paid_cents) if nxt else None,
        days_past_due=days_past_due(loan.schedule, as_of),
    )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get(
    "/dashboard/loan-book",
    response_model=VendorLoanBook,
    dependencies=[
        Depends(
            require_clinic_permission("loan_servicing", "collection", "monitoring")
        )
    ],
)
def loan_book(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
) -> VendorLoanBook:
    """Paginated loan list for the caller's vendor (spec §3.4).

    Cursor pagination on ``(created_at, id)`` descending — stable across
    inserts. ``status`` optionally filters on ``PlatformLoan.status``.
    """
    # Select (loan, patient) tuples: loan -> application (scoping) -> patient.
    # The patient join is OUTER so a loan still lists even if its patient row is
    # somehow missing (name falls back to "Unknown patient").
    q = (
        db.query(PlatformLoan, PlatformPatient)
        .join(
            PlatformCreditApplication,
            PlatformLoan.application_id == PlatformCreditApplication.id,
        )
        .outerjoin(
            PlatformPatient,
            PlatformCreditApplication.patient_id == PlatformPatient.id,
        )
        .filter(PlatformCreditApplication.vendor_id == principal.vendor_id)
        .options(joinedload(PlatformLoan.schedule))
        .order_by(PlatformLoan.created_at.desc(), PlatformLoan.id.desc())
    )

    if status is not None:
        q = q.filter(PlatformLoan.status == status)

    if cursor is not None:
        c_created, c_id = _decode_cursor(cursor)
        # Keyset: rows strictly "after" the cursor in (created_at desc, id desc).
        q = q.filter(
            (PlatformLoan.created_at < c_created)
            | ((PlatformLoan.created_at == c_created) & (PlatformLoan.id < c_id))
        )

    # Fetch one extra to know whether another page exists.
    results = q.limit(limit + 1).all()
    has_more = len(results) > limit
    page = results[:limit]

    as_of = datetime.now(timezone.utc).date()
    rows = [_to_row(loan, patient, as_of) for loan, patient in page]

    if has_more and page:
        last_loan = page[-1][0]
        next_cursor = _encode_cursor(last_loan.created_at, last_loan.id)
    else:
        next_cursor = None
    return VendorLoanBook(rows=rows, next_cursor=next_cursor)
