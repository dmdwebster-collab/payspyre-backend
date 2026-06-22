"""Phase 3 vendor dashboard — marketplace funnel & revenue (spec §3.3, §3.5, §3.1).

Two staff-authenticated, vendor-scoped reads plus one pure helper:

    GET /clinic/v1/dashboard/funnel?from&to   -> VendorFunnel   (spec §3.3)
    GET /clinic/v1/dashboard/revenue?from&to  -> VendorRevenue  (spec §3.5)
    marketplace_block(db, vendor_id, frm, to) -> dict           (spec §3.1 `marketplace`)

SCOPING (hard rule): every query filters on ``principal.vendor_id``. There is no
cross-vendor leakage. Note ``PlatformMarketplaceVendorInterest.vendor_id`` is a
plain column (no FK) and is filtered directly. The application-track counts come
from ``PlatformCreditApplication.vendor_id`` (the originating clinic).

The funnel has TWO parallel tracks (spec §3.3):
  * application track — status counts on ``platform_credit_applications``
    (started, verifying, pre_qualified, approved) + a derived ``disbursed``.
  * marketplace track — interest/listing row counts
    (leads_viewed, interest_expressed, appointment_booked, charged).

`disbursed` derivation: counted as approved apps (scoped to this vendor) that
have a linked ``PlatformLoan`` (via ``application_id``) whose ``disbursed_at`` is
set and within the window. Loans carry no ``vendor_id`` so we join through the
application. If no such loans exist the count is simply 0.

Money is integer cents throughout.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.loan import PlatformLoan
from app.models.platform.marketplace import (
    PlatformMarketplaceListing,
    PlatformMarketplaceVendorInterest,
)

router = APIRouter(tags=["clinic-dashboard"])

_DEFAULT_WINDOW_DAYS = 30


# --- response schemas (local; do NOT touch shared schemas.py) ---------------


class VendorFunnel(BaseModel):
    # application track
    started: int
    verifying: int
    pre_qualified: int
    approved: int
    disbursed: int
    # marketplace track (parallel)
    leads_viewed: int
    interest_expressed: int
    appointment_booked: int
    charged: int


class RevenuePoint(BaseModel):
    bucket: str  # ISO date at bucket start (day granularity)
    charges_cents: int
    charge_count: int


class VendorRevenue(BaseModel):
    total_charges_cents: int
    charge_count: int
    by_trigger: dict[str, int]  # charge_trigger -> cents
    timeseries: list[RevenuePoint]


# --- window helpers ---------------------------------------------------------


def _resolve_window(
    frm: Optional[str], to: Optional[str]
) -> tuple[datetime, datetime]:
    """Resolve the effective [from, to) window (default = last 30 days).

    Accepts ISO-8601 strings; naive inputs are treated as UTC. ``to`` is the
    upper bound (inclusive of rows at/below it via ``<=``).
    """
    now = datetime.now(timezone.utc)
    to_dt = _parse_iso(to) if to else now
    frm_dt = _parse_iso(frm) if frm else (to_dt - timedelta(days=_DEFAULT_WINDOW_DAYS))
    return frm_dt, to_dt


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --- core computations ------------------------------------------------------


def _application_track(
    db: Session, vendor_id: UUID, frm: datetime, to: datetime
) -> dict:
    """Application-track funnel counts for ONE vendor, scoped to the window.

    Status counts are keyed off ``created_at`` ∈ [frm, to]. ``disbursed`` is the
    count of this vendor's approved apps with a linked disbursed loan in window.
    """
    base = (
        db.query(PlatformCreditApplication.status, func.count())
        .filter(PlatformCreditApplication.vendor_id == vendor_id)
        .filter(PlatformCreditApplication.created_at >= frm)
        .filter(PlatformCreditApplication.created_at <= to)
        .group_by(PlatformCreditApplication.status)
    )
    counts = {status: count for status, count in base.all()}

    disbursed = (
        db.query(func.count(PlatformLoan.id))
        .join(
            PlatformCreditApplication,
            PlatformLoan.application_id == PlatformCreditApplication.id,
        )
        .filter(PlatformCreditApplication.vendor_id == vendor_id)
        .filter(PlatformLoan.disbursed_at.isnot(None))
        .filter(PlatformLoan.disbursed_at >= frm)
        .filter(PlatformLoan.disbursed_at <= to)
        .scalar()
        or 0
    )

    return {
        "started": int(counts.get("started", 0)),
        "verifying": int(counts.get("verifying", 0)),
        "pre_qualified": int(counts.get("pre_qualified", 0)),
        "approved": int(counts.get("approved", 0)),
        "disbursed": int(disbursed),
    }


def _marketplace_track(
    db: Session, vendor_id: UUID, frm: datetime, to: datetime
) -> dict:
    """Marketplace-track funnel counts for ONE vendor, scoped to the window.

    leads_viewed         — distinct listings this vendor has interest rows for.
    interest_expressed   — interest rows with ``expressed_at`` ∈ window.
    appointment_booked   — interest rows with ``appointment_booked_at`` ∈ window.
    charged              — interest rows with ``lead_charged_at`` ∈ window.
    """
    iface = PlatformMarketplaceVendorInterest
    vendor_q = db.query(iface).filter(iface.vendor_id == vendor_id)

    leads_viewed = (
        db.query(func.count(func.distinct(iface.listing_id)))
        .filter(iface.vendor_id == vendor_id)
        .scalar()
        or 0
    )
    interest_expressed = (
        vendor_q.filter(iface.expressed_at >= frm)
        .filter(iface.expressed_at <= to)
        .count()
    )
    appointment_booked = (
        db.query(func.count(iface.id))
        .filter(iface.vendor_id == vendor_id)
        .filter(iface.appointment_booked_at.isnot(None))
        .filter(iface.appointment_booked_at >= frm)
        .filter(iface.appointment_booked_at <= to)
        .scalar()
        or 0
    )
    charged = (
        db.query(func.count(iface.id))
        .filter(iface.vendor_id == vendor_id)
        .filter(iface.lead_charged_at.isnot(None))
        .filter(iface.lead_charged_at >= frm)
        .filter(iface.lead_charged_at <= to)
        .scalar()
        or 0
    )
    return {
        "leads_viewed": int(leads_viewed),
        "interest_expressed": int(interest_expressed),
        "appointment_booked": int(appointment_booked),
        "charged": int(charged),
    }


def marketplace_block(db: Session, vendor_id: UUID, frm: datetime, to: datetime) -> dict:
    """Spec §3.1 `marketplace` block for the overview endpoint (pure helper).

    Stable, documented signature — imported by a later overview endpoint.

    Args:
        db:        active SQLAlchemy session.
        vendor_id: the caller's clinic/vendor; EVERY count is scoped to it.
        frm, to:   inclusive datetime window bounds (UTC-aware recommended).

    Returns a dict with exactly these keys (spec §3.1):
        leads_available     — distinct listings this vendor has interest in.
        interests_expressed — interest rows expressed in window.
        appointments_booked — interest rows with an appointment booked in window.
        charges_cents       — Σ lead_charge_cents charged in window (integer cents).
    """
    iface = PlatformMarketplaceVendorInterest
    track = _marketplace_track(db, vendor_id, frm, to)

    charges_cents = (
        db.query(func.coalesce(func.sum(iface.lead_charge_cents), 0))
        .filter(iface.vendor_id == vendor_id)
        .filter(iface.lead_charged_at.isnot(None))
        .filter(iface.lead_charged_at >= frm)
        .filter(iface.lead_charged_at <= to)
        .scalar()
        or 0
    )

    return {
        "leads_available": track["leads_viewed"],
        "interests_expressed": track["interest_expressed"],
        "appointments_booked": track["appointment_booked"],
        "charges_cents": int(charges_cents),
    }


def _revenue(db: Session, vendor_id: UUID, frm: datetime, to: datetime) -> VendorRevenue:
    """Spec §3.5 revenue — lead-charge totals/breakdown/day-bucketed series.

    Built off the same charged interest rows ``vendor_billing`` reads (interests
    with ``lead_charged_at`` set), scoped to this vendor and the window.
    """
    iface = PlatformMarketplaceVendorInterest
    charged = (
        db.query(iface)
        .filter(iface.vendor_id == vendor_id)
        .filter(iface.lead_charged_at.isnot(None))
        .filter(iface.lead_charged_at >= frm)
        .filter(iface.lead_charged_at <= to)
        .order_by(iface.lead_charged_at.asc())
        .all()
    )

    total_cents = 0
    by_trigger: dict[str, int] = {}
    buckets: dict[str, dict] = {}  # iso-date -> {cents, count}

    for row in charged:
        cents = int(row.lead_charge_cents or 0)
        total_cents += cents

        trigger = row.charge_trigger or "unknown"
        by_trigger[trigger] = by_trigger.get(trigger, 0) + cents

        day = row.lead_charged_at.date().isoformat()
        b = buckets.setdefault(day, {"charges_cents": 0, "charge_count": 0})
        b["charges_cents"] += cents
        b["charge_count"] += 1

    timeseries = [
        RevenuePoint(
            bucket=day,
            charges_cents=b["charges_cents"],
            charge_count=b["charge_count"],
        )
        for day, b in sorted(buckets.items())
    ]

    return VendorRevenue(
        total_charges_cents=total_cents,
        charge_count=len(charged),
        by_trigger=by_trigger,
        timeseries=timeseries,
    )


# --- routes -----------------------------------------------------------------


@router.get("/dashboard/funnel", response_model=VendorFunnel)
def dashboard_funnel(
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
):
    """Conversion funnel (application origination + marketplace) for the vendor."""
    frm, to_dt = _resolve_window(from_, to)
    app_track = _application_track(db, principal.vendor_id, frm, to_dt)
    mkt_track = _marketplace_track(db, principal.vendor_id, frm, to_dt)
    return VendorFunnel(**app_track, **mkt_track)


@router.get("/dashboard/revenue", response_model=VendorRevenue)
def dashboard_revenue(
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
):
    """Marketplace earnings / lead-charge history (spec §3.5), day-bucketed."""
    frm, to_dt = _resolve_window(from_, to)
    return _revenue(db, principal.vendor_id, frm, to_dt)
