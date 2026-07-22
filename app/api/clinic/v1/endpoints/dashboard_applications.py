"""Clinic dashboard — applications analytics (vendor performance, Phase 1).

Implements the Phase-1 "applications" slice of the vendor performance dashboard
spec (``docs/vendor_dashboard_spec.md`` §3.1 applications block + §3.2 timeseries).
Pure ``platform_credit_applications`` reads — zero new joins.

GET /clinic/v1/dashboard/applications/timeseries — volume + outcome trend.

Plus ``applications_block(db, vendor_id, frm, to)`` — a pure helper returning the
spec §3.1 ``applications`` dict. It is imported by the (separately-built)
``/dashboard/overview`` endpoint, so its signature and returned keys are a stable
contract — do not change them without updating that consumer.

SCOPING: clinics ARE the ``vendors`` table (spec §13). Every query filters on
``PlatformCreditApplication.vendor_id`` (the originating clinic). The caller's
``vendor_id`` comes from ``get_current_clinic_user`` (resolved via
``platform_clinic_memberships``); no cross-vendor data is ever returned. We use
the direct ``vendor_id`` FK — NOT the legacy ``Vendor.applications`` ORM
relationship, which points at the wrong/legacy ``LoanApplication`` table
(spec §2 gotcha #1).

STATUS VOCAB: KPIs use the FULL 9-value platform status enum, NOT the 4 collapsed
console buckets (spec §2 gotcha #3). ``in_review`` here means a human/automated
review is in flight: ``verifying | pre_qualified | awaiting_hard_pull |
under_review``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.services.clinic_permissions import require_clinic_permission
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication

router = APIRouter(tags=["clinic-dashboard"])

Granularity = Literal["day", "week", "month"]

# Full 9-value platform application status vocab (spec §2 gotcha #3).
# We map each raw status into ONE performance bucket. ``approved`` and
# ``declined`` are terminal decisions; the four in-flight review states collapse
# to ``in_review``; ``started`` stands alone; ``withdrawn``/``expired`` are dead
# and counted in neither a positive bucket nor in_review (they still count toward
# ``total``).
_IN_REVIEW_STATUSES = frozenset(
    {
        "verifying", "pre_qualified", "awaiting_hard_pull", "under_review",
        # Dave's Status Flow v1.00 (migration 068): the three parallel gates and
        # the two post-underwriting steps are all "a review is in flight".
        "credit_report", "bank_verification", "application_verification",
        "underwriting", "offer_acceptance", "agreement_signature",
    }
)

# An application that reached Active (or one of the six closed states off Active)
# WAS approved — the funnel must keep counting it as such (migration 068).
_APPROVED_STATUSES = frozenset(
    {
        "approved", "active",
        "repaid", "renewed", "refinanced", "transferred", "settlement", "written_off",
    }
)


# --- schemas (kept in THIS module; do NOT add to the shared schemas.py) ------


class AppTimeseriesPoint(BaseModel):
    """Mirror of the frontend ``AppTimeseriesPoint`` interface (spec §3.2)."""

    bucket: str  # ISO date at bucket start
    started: int = 0
    approved: int = 0
    declined: int = 0
    in_review: int = 0
    requested_amount_cents: int = 0


class AppTimeseries(BaseModel):
    """Mirror of the frontend ``AppTimeseries`` type (spec §3.2)."""

    granularity: Granularity
    points: list[AppTimeseriesPoint]


# --- window helpers ----------------------------------------------------------


def _default_window(
    frm: Optional[datetime], to: Optional[datetime]
) -> tuple[datetime, datetime]:
    """Resolve the effective [frm, to] window, defaulting to the last 30 days.

    Naive datetimes (no tzinfo) are assumed UTC so comparisons against the
    timezone-aware ``created_at`` / ``decision_at`` columns are well-defined.
    """
    now = datetime.now(timezone.utc)
    to = to or now
    frm = frm or (to - timedelta(days=30))
    if frm.tzinfo is None:
        frm = frm.replace(tzinfo=timezone.utc)
    if to.tzinfo is None:
        to = to.replace(tzinfo=timezone.utc)
    return frm, to


# --- query helpers (vendor-scoped) -------------------------------------------


def query_applications_in_window(
    db: Session, vendor_id: UUID, frm: datetime, to: datetime
) -> list[PlatformCreditApplication]:
    """Applications for ONE clinic created within [frm, to].

    Clinic-scoped: filters on ``vendor_id``. Bucketed/aggregated by ``created_at``
    (the application-origination timestamp). Ordered ascending so the caller can
    bucket without re-sorting.
    """
    return (
        db.query(PlatformCreditApplication)
        .filter(
            PlatformCreditApplication.vendor_id == vendor_id,
            PlatformCreditApplication.created_at >= frm,
            PlatformCreditApplication.created_at <= to,
        )
        .order_by(PlatformCreditApplication.created_at.asc())
        .all()
    )


def query_decided_in_window(
    db: Session, vendor_id: UUID, frm: datetime, to: datetime
) -> list[PlatformCreditApplication]:
    """Applications for ONE clinic whose ``decision_at`` falls within [frm, to].

    Used for ``approval_rate`` (spec §4): the denominator is apps *decided* in the
    window, regardless of when they were created.
    """
    return (
        db.query(PlatformCreditApplication)
        .filter(
            PlatformCreditApplication.vendor_id == vendor_id,
            PlatformCreditApplication.decision_at.isnot(None),
            PlatformCreditApplication.decision_at >= frm,
            PlatformCreditApplication.decision_at <= to,
        )
        .all()
    )


# --- bucketing ---------------------------------------------------------------


def _bucket_start(dt: datetime, granularity: Granularity) -> datetime:
    """Truncate ``dt`` to the start of its day/week/month bucket (UTC).

    Week buckets start on Monday (ISO). Returns a tz-aware UTC datetime at
    midnight of the bucket's first day.
    """
    dt = dt.astimezone(timezone.utc)
    day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "day":
        return day
    if granularity == "week":
        return day - timedelta(days=day.weekday())
    if granularity == "month":
        return day.replace(day=1)
    raise ValueError(f"unsupported granularity: {granularity!r}")


def _outcome_bucket(status: str, decision_by: Optional[str] = None) -> Optional[str]:
    """Map a raw 9-value platform status to a timeseries outcome key, or None.

    Returns one of ``started | approved | declined | in_review`` — or ``None`` for
    dead states (``withdrawn``/``expired``) which contribute amount but no
    outcome count. Unknown statuses also return ``None`` (forward-compatible: a
    new enum value never 500s the chart).

    WS-I silent escalation (10__Vendor_Access.md): an AUTO decline pending human
    review must not be shown to the vendor as a final decline anywhere on the
    vendor surface — it buckets as ``in_review`` until a human confirms
    (``decision_by`` becomes the human actor).
    """
    if status in _APPROVED_STATUSES:
        return "approved"
    if status == "declined":
        return "in_review" if decision_by == "auto" else "declined"
    if status == "started":
        return "started"
    if status in _IN_REVIEW_STATUSES:
        return "in_review"
    return None


def build_timeseries(
    rows: list[PlatformCreditApplication], granularity: Granularity
) -> AppTimeseries:
    """Bucket ``rows`` (by ``created_at``) into an ``AppTimeseries``.

    Each row contributes its ``requested_amount_cents`` to its bucket and, when it
    maps to an outcome (``_outcome_bucket``), increments that outcome's count.
    Points are emitted in ascending bucket order. Empty buckets between populated
    ones are NOT synthesized (sparse series; the frontend fills gaps).
    """
    buckets: dict[datetime, dict[str, int]] = {}
    for row in rows:
        key = _bucket_start(row.created_at, granularity)
        point = buckets.setdefault(
            key,
            {"started": 0, "approved": 0, "declined": 0, "in_review": 0,
             "requested_amount_cents": 0},
        )
        point["requested_amount_cents"] += row.requested_amount_cents or 0
        outcome = _outcome_bucket(row.status, getattr(row, "decision_by", None))
        if outcome is not None:
            point[outcome] += 1

    points = [
        AppTimeseriesPoint(bucket=key.date().isoformat(), **vals)
        for key, vals in sorted(buckets.items())
    ]
    return AppTimeseries(granularity=granularity, points=points)


# --- §3.1 applications block (pure helper, stable contract) ------------------


def applications_block(
    db: Session, vendor_id: UUID, frm: datetime, to: datetime
) -> dict:
    """Spec §3.1 ``applications`` block for ONE clinic over the window [frm, to].

    STABLE CONTRACT — imported by the ``/dashboard/overview`` endpoint. The
    signature and the returned dict keys must not change without updating that
    consumer.

    Args:
        db: SQLAlchemy session.
        vendor_id: the caller's clinic (scopes every query; no cross-vendor data).
        frm, to: inclusive window bounds (tz-aware UTC recommended).

    Returns a dict with EXACTLY these keys:
        total                       — apps created in window (count)
        approved                    — created-in-window with status 'approved'
        declined                    — created-in-window with status 'declined'
        in_review                   — created-in-window in {verifying, pre_qualified,
                                      awaiting_hard_pull, under_review}
        started                     — created-in-window with status 'started'
        approval_rate               — approved/(approved+declined) over apps whose
                                      DECISION_AT ∈ window (spec §4); None if no
                                      decisions in window
        avg_requested_amount_cents  — mean requested_amount_cents over created-in-
                                      window apps (int, floored); 0 when none
        total_requested_amount_cents— sum requested_amount_cents over created-in-
                                      window apps

    NOTE: count/amount buckets are over apps CREATED in the window; ``approval_rate``
    is over apps DECIDED in the window (different population, per spec §4).
    """
    created = query_applications_in_window(db, vendor_id, frm, to)

    total = len(created)
    approved = 0
    declined = 0
    in_review = 0
    started = 0
    total_amount = 0
    for row in created:
        total_amount += row.requested_amount_cents or 0
        # WS-I silent escalation: an auto-decline pending human review buckets
        # as in_review on the vendor surface, never as a final decline.
        outcome = _outcome_bucket(row.status, getattr(row, "decision_by", None))
        if outcome == "approved":
            approved += 1
        elif outcome == "declined":
            declined += 1
        elif outcome == "started":
            started += 1
        elif outcome == "in_review":
            in_review += 1
        # withdrawn / expired: counted in total only.

    avg_amount = total_amount // total if total else 0

    # approval_rate: over apps DECIDED in the window (spec §4). Auto-declines
    # pending human review are NOT vendor-visible as decided (WS-I), so they are
    # excluded from the denominator until a human confirms.
    decided = query_decided_in_window(db, vendor_id, frm, to)
    dec_approved = sum(1 for r in decided if r.status == "approved")
    dec_declined = sum(
        1
        for r in decided
        if r.status == "declined" and getattr(r, "decision_by", None) != "auto"
    )
    denom = dec_approved + dec_declined
    approval_rate = (dec_approved / denom) if denom else None

    return {
        "total": total,
        "approved": approved,
        "declined": declined,
        "in_review": in_review,
        "started": started,
        "approval_rate": approval_rate,
        "avg_requested_amount_cents": avg_amount,
        "total_requested_amount_cents": total_amount,
    }


# --- routes ------------------------------------------------------------------


@router.get(
    "/dashboard/applications/timeseries",
    response_model=AppTimeseries,
    dependencies=[Depends(require_clinic_permission("monitoring", "loan_servicing"))],
)
def applications_timeseries(
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
    frm: Optional[datetime] = Query(None, alias="from"),
    to: Optional[datetime] = Query(None, alias="to"),
    granularity: Granularity = Query("day"),
):
    """Application volume + outcome trend for charts (spec §3.2).

    Default window = last 30 days. Buckets by ``created_at`` at the requested
    granularity. Clinic-scoped to ``principal.vendor_id``.
    """
    frm, to = _default_window(frm, to)
    rows = query_applications_in_window(db, principal.vendor_id, frm, to)
    return build_timeseries(rows, granularity)
