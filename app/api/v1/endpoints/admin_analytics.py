"""Lender/admin portal Phase 4 — advanced portfolio analytics.

The god-view KPIs that best-in-class lenders surface, computed from the existing
loan / payment spine (no new tables):

  * /portfolio     headline ratios — outstanding principal, weighted APR, avg
                   loan size, delinquency rate, charge-off rate, status mix
  * /vintage       origination cohorts (by month) with current outstanding,
                   charged-off and delinquent counts — the cohort/vintage curve
  * /originations  monthly origination trend (count + funded volume)
  * /cei           collection-effectiveness proxy per month (collected / due)

Read-only; admin/staff gated at the router level. Money in integer cents; rates
returned as fractions (0..1) and bps where noted.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import case, func
from sqlalchemy.orm import Query as SAQuery, Session

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.loan import PlatformLoan, PlatformLoanPayment, PlatformLoanScheduleItem
from app.services import delinquency_buckets as dq
from app.services.metrics import analytics_reports as R

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])

# Loans that are still carrying a live balance we service against.
_LIVE = ("active", "delinquent")
# The origination date of a loan: when it actually funded, else when it was booked.
_ORIG = func.coalesce(PlatformLoan.disbursed_at, PlatformLoan.created_at)


def _ratio(num: int | None, den: int | None) -> float | None:
    num = num or 0
    den = den or 0
    return round(num / den, 4) if den else None


# ---------------------------------------------------------------------------
# Shared report filters (Dave's report set).
#
#   scope   : All / a specific vendor (drill to provider) / a specific province
#   interval: Daily / Weekly / Monthly / Quarterly / Annually
#
# A loan is tied to a vendor + province through its originating application:
#   PlatformLoan.application_id -> PlatformCreditApplication.vendor_id -> vendors.id
#   vendors.province is the origination province (patient province is not tracked;
#   see the SCORING/note block below).
#
# NOTE (provider drill): the current schema models a single origination entity —
# ``vendor``. There is no distinct provider table yet, so ``vendor_id`` IS the
# drill-down key. When a provider entity lands, add ``provider_id`` here.
# ---------------------------------------------------------------------------


class ReportFilters:
    """Parsed, validated report filters — passed by Depends into each report.

    ``vendor_ids`` is Dave's most-repeated video-05 ask: multi-select vendor
    checkboxes with a multi-location rollup ("Dr. Webster in all of his
    locations… select those three vendor accounts and have a total report").
    It combines with the pre-existing single ``vendor_id`` into one
    de-duplicated scope, so every existing caller keeps working.
    """

    def __init__(
        self,
        interval: str = Query("monthly", description="daily|weekly|monthly|quarterly|annually"),
        vendor_id: Optional[UUID] = Query(None, description="Scope to one vendor (provider drill)"),
        vendor_ids: Optional[list[UUID]] = Query(
            None,
            description="Multi-select vendor scope (repeat the param); rolls the "
            "selected vendors up into one report. Combines with vendor_id.",
        ),
        province: Optional[str] = Query(None, description="Scope to one province (vendor province)"),
        periods: int = Query(12, ge=1, le=60, description="Trendline periods to return"),
    ):
        self.interval = R.valid_interval(interval)
        self.trunc = R.interval_trunc_field(self.interval)
        self.vendor_id = vendor_id
        ids: list[UUID] = list(vendor_ids or [])
        if vendor_id is not None and vendor_id not in ids:
            ids.append(vendor_id)
        # None = all vendors; else the de-duplicated multi-select rollup scope.
        self.vendor_scope: Optional[tuple[UUID, ...]] = tuple(dict.fromkeys(ids)) or None
        self.province = province
        self.periods = periods

    @property
    def needs_app_join(self) -> bool:
        return self.vendor_scope is not None or self.province is not None


def _scope_loans(q: SAQuery, f: ReportFilters) -> SAQuery:
    """Apply vendor/province scope to a query already selecting from PlatformLoan.

    Joins to the originating application (and vendors for province) only when a
    scope is set, so the unfiltered "All" path stays a plain loan scan.
    """
    if f.needs_app_join:
        q = q.join(PlatformCreditApplication,
                   PlatformLoan.application_id == PlatformCreditApplication.id)
        if f.vendor_scope is not None:
            q = q.filter(PlatformCreditApplication.vendor_id.in_(f.vendor_scope))
        if f.province is not None:
            q = q.join(Vendor, PlatformCreditApplication.vendor_id == Vendor.id)
            q = q.filter(Vendor.province == f.province)
    return q


def _scope_apps(q: SAQuery, f: ReportFilters) -> SAQuery:
    """Apply vendor/province scope to a query selecting from PlatformCreditApplication."""
    if f.vendor_scope is not None:
        q = q.filter(PlatformCreditApplication.vendor_id.in_(f.vendor_scope))
    if f.province is not None:
        q = q.join(Vendor, PlatformCreditApplication.vendor_id == Vendor.id)
        q = q.filter(Vendor.province == f.province)
    return q


def _period_key(col, trunc: str):
    """A stable ISO-ish text bucket key for a timestamp column, per interval.

    ``trunc`` (day/week/month/quarter/year) is a validated whitelist value from
    ``R.interval_trunc_field`` — never raw user text — so passing it into
    ``func.date_trunc`` is safe (no f-string / string-format SQL).
    """
    return func.to_char(func.date_trunc(trunc, col), "YYYY-MM-DD")


class PortfolioSummary(BaseModel):
    originated_count: int
    originated_principal_cents: int
    active_loans: int
    outstanding_principal_cents: int
    weighted_avg_apr_bps: int | None
    avg_loan_size_cents: int | None
    delinquent_principal_cents: int
    delinquency_rate: float | None          # delinquent balance / outstanding
    charged_off_count: int
    charged_off_principal_cents: int
    charge_off_rate: float | None           # charged-off principal / originated
    paid_off_count: int
    by_status: dict[str, int]


@router.get("/portfolio", response_model=PortfolioSummary)
def portfolio(f: ReportFilters = Depends(), db: Session = Depends(get_db)):
    originated_count, originated_principal = _scope_loans(db.query(
        func.count(PlatformLoan.id), func.coalesce(func.sum(PlatformLoan.principal_cents), 0)
    ), f).one()

    outstanding, active_loans, wapr_num = _scope_loans(db.query(
        func.coalesce(func.sum(PlatformLoan.principal_balance_cents), 0),
        func.count(PlatformLoan.id),
        func.coalesce(func.sum(PlatformLoan.principal_balance_cents * PlatformLoan.annual_rate_bps), 0),
    ).filter(PlatformLoan.status.in_(_LIVE)), f).one()

    delinquent_principal = _scope_loans(db.query(
        func.coalesce(func.sum(PlatformLoan.principal_balance_cents), 0)
    ).filter(PlatformLoan.status == "delinquent"), f).scalar()

    co_count, co_principal = _scope_loans(db.query(
        func.count(PlatformLoan.id), func.coalesce(func.sum(PlatformLoan.principal_cents), 0)
    ).filter(PlatformLoan.status == "charged_off"), f).one()

    paid_off = _scope_loans(
        db.query(func.count(PlatformLoan.id)).filter(PlatformLoan.status == "paid_off"), f
    ).scalar()
    avg_size = _scope_loans(db.query(func.avg(PlatformLoan.principal_cents)), f).scalar()

    by_status = {
        s: c for s, c in _scope_loans(
            db.query(PlatformLoan.status, func.count(PlatformLoan.id)), f
        ).group_by(PlatformLoan.status).all()
    }

    return PortfolioSummary(
        originated_count=originated_count,
        originated_principal_cents=int(originated_principal),
        active_loans=active_loans,
        outstanding_principal_cents=int(outstanding),
        weighted_avg_apr_bps=int(round(wapr_num / outstanding)) if outstanding else None,
        avg_loan_size_cents=int(round(avg_size)) if avg_size is not None else None,
        delinquent_principal_cents=int(delinquent_principal),
        delinquency_rate=_ratio(delinquent_principal, outstanding),
        charged_off_count=co_count,
        charged_off_principal_cents=int(co_principal),
        charge_off_rate=_ratio(co_principal, originated_principal),
        paid_off_count=paid_off,
        by_status=by_status,
    )


class VintageCohort(BaseModel):
    cohort: str                       # YYYY-MM of origination
    originated_count: int
    originated_principal_cents: int
    outstanding_principal_cents: int
    charged_off_count: int
    charged_off_principal_cents: int
    delinquent_count: int
    charge_off_rate: float | None     # charged-off principal / originated (this cohort)


@router.get("/vintage", response_model=list[VintageCohort])
def vintage(f: ReportFilters = Depends(), db: Session = Depends(get_db)):
    """Origination cohorts by month — the vintage curve of how each book ages."""
    month = func.to_char(func.date_trunc("month", _ORIG), "YYYY-MM")
    is_co = PlatformLoan.status == "charged_off"
    rows = (
        _scope_loans(db.query(
            month.label("cohort"),
            func.count(PlatformLoan.id),
            func.coalesce(func.sum(PlatformLoan.principal_cents), 0),
            func.coalesce(func.sum(
                case((PlatformLoan.status.in_(_LIVE), PlatformLoan.principal_balance_cents), else_=0)), 0),
            func.coalesce(func.sum(case((is_co, 1), else_=0)), 0),
            func.coalesce(func.sum(case((is_co, PlatformLoan.principal_cents), else_=0)), 0),
            func.coalesce(func.sum(case((PlatformLoan.status == "delinquent", 1), else_=0)), 0),
        ), f)
        .group_by("cohort")
        .order_by("cohort")
        .all()
    )
    out = []
    for cohort, n, orig, outst, co_n, co_p, delq in rows:
        out.append(VintageCohort(
            cohort=cohort, originated_count=n, originated_principal_cents=int(orig),
            outstanding_principal_cents=int(outst), charged_off_count=int(co_n),
            charged_off_principal_cents=int(co_p), delinquent_count=int(delq),
            charge_off_rate=_ratio(int(co_p), int(orig)),
        ))
    return out


class OriginationPoint(BaseModel):
    month: str
    count: int
    principal_cents: int


@router.get("/originations", response_model=list[OriginationPoint])
def originations(months: int = 12, f: ReportFilters = Depends(), db: Session = Depends(get_db)):
    """Monthly origination trend (funded count + volume), most recent last."""
    month = func.to_char(func.date_trunc("month", _ORIG), "YYYY-MM")
    rows = (
        _scope_loans(
            db.query(month.label("m"), func.count(PlatformLoan.id),
                     func.coalesce(func.sum(PlatformLoan.principal_cents), 0)),
            f,
        ).group_by("m").order_by("m").all()
    )
    points = [OriginationPoint(month=m, count=n, principal_cents=int(p)) for m, n, p in rows]
    return points[-months:] if months and months > 0 else points


class CeiPoint(BaseModel):
    month: str
    collected_cents: int
    scheduled_due_cents: int
    cei: float | None   # collected / scheduled due (collection-effectiveness proxy)


@router.get("/cei", response_model=list[CeiPoint])
def cei(months: int = 6, f: ReportFilters = Depends(), db: Session = Depends(get_db)):
    """Collection-effectiveness proxy per month: payments collected vs amount due."""
    today = datetime.now(timezone.utc).date()
    # First-of-month, `months` back.
    start_month = (today.replace(day=1) - timedelta(days=31 * max(months - 1, 0))).replace(day=1)

    pay_m = func.to_char(func.date_trunc("month", PlatformLoanPayment.received_at), "YYYY-MM")
    pay_q = (
        db.query(pay_m, func.coalesce(func.sum(PlatformLoanPayment.amount_cents), 0))
        .filter(PlatformLoanPayment.received_at >= start_month)
    )
    if f.needs_app_join:
        pay_q = _scope_loans(
            pay_q.join(PlatformLoan, PlatformLoanPayment.loan_id == PlatformLoan.id), f
        )
    collected = {m: int(v) for m, v in pay_q.group_by(pay_m).all()}
    due_m = func.to_char(func.date_trunc("month", PlatformLoanScheduleItem.due_date), "YYYY-MM")
    due_q = (
        db.query(due_m, func.coalesce(func.sum(PlatformLoanScheduleItem.total_cents), 0))
        .filter(PlatformLoanScheduleItem.due_date >= start_month)
    )
    if f.needs_app_join:
        due_q = _scope_loans(
            due_q.join(PlatformLoan, PlatformLoanScheduleItem.loan_id == PlatformLoan.id), f
        )
    due = {m: int(v) for m, v in due_q.group_by(due_m).all()}

    out = []
    for m in sorted(set(collected) | set(due)):
        c, d = collected.get(m, 0), due.get(m, 0)
        out.append(CeiPoint(month=m, collected_cents=c, scheduled_due_cents=d, cei=_ratio(c, d)))
    return out


# ===========================================================================
# Dave's lender reporting suite. Each report returns dashboard-shaped JSON:
# a trendline ``series`` (ordered points), scalar ``totals``, and prior-period
# ``deltas`` (last interval vs the one before). All are admin/staff gated at the
# router level and honor the shared All/vendor/province + interval filters.
#
# Money in integer cents. Where a datum is not derivable from the current schema
# it is returned explicitly as ``null`` with a clear key + a ``notes`` string,
# never faked. See the per-report NOTE comments for what is stubbed and why.
# ===========================================================================

# Loans that ever funded — the analytics universe for money-movement reports.
_FUNDED = ("active", "delinquent", "paid_off", "charged_off")


class SeriesPoint(BaseModel):
    period: str
    value: float


# --------------------------------------------------------------------------
# 1) PORTFOLIO — size $, disbursed $, repaid $, profit $, profit %, each with a
#    trendline + increase/decrease vs the selected interval.
# --------------------------------------------------------------------------


class PortfolioReport(BaseModel):
    interval: str
    totals: dict
    series: dict[str, list[SeriesPoint]]
    deltas: dict[str, dict]
    notes: list[str] = []


@router.get("/reports/portfolio", response_model=PortfolioReport)
def report_portfolio(f: ReportFilters = Depends(), db: Session = Depends(get_db)):
    """Portfolio size / disbursed / repaid / profit ($ + %) with trendlines.

    * disbursed  = principal of loans that funded, bucketed by disbursement date
    * repaid     = payments received, bucketed by received date
    * size       = live outstanding principal (active + delinquent), snapshot
    * profit     = interest+fees earned less principal charged off (from statements)
    """
    disb_key = _period_key(_ORIG, f.trunc)
    disb_q = _scope_loans(
        db.query(disb_key.label("p"),
                 func.coalesce(func.sum(PlatformLoan.principal_cents), 0))
        .filter(PlatformLoan.status.in_(_FUNDED)),
        f,
    ).group_by("p").order_by("p")
    disbursed = [(p, int(v)) for p, v in disb_q.all()]

    pay_key = _period_key(PlatformLoanPayment.received_at, f.trunc)
    repaid_q = _scope_loans(
        db.query(pay_key.label("p"), func.coalesce(func.sum(PlatformLoanPayment.amount_cents), 0))
        .join(PlatformLoan, PlatformLoanPayment.loan_id == PlatformLoan.id),
        f,
    ).group_by("p").order_by("p")
    repaid = [(p, int(v)) for p, v in repaid_q.all()]

    # Profit series from statements: interest paid, bucketed by period_end. Fees
    # are not separately modeled in the schedule (total = principal + interest),
    # so fees_cents is 0 today — surfaced in notes, not silently folded in.
    from app.models.platform.loan import PlatformLoanStatement

    stmt_key = _period_key(PlatformLoanStatement.period_end, f.trunc)
    stmt_q = _scope_loans(
        db.query(stmt_key.label("p"),
                 func.coalesce(func.sum(PlatformLoanStatement.interest_paid_cents), 0))
        .join(PlatformLoan, PlatformLoanStatement.loan_id == PlatformLoan.id),
        f,
    ).group_by("p").order_by("p")
    interest_earned = [(p, int(v)) for p, v in stmt_q.all()]

    # Charge-offs bucketed by origination period (principal lost).
    co_q = _scope_loans(
        db.query(disb_key.label("p"), func.coalesce(func.sum(PlatformLoan.principal_cents), 0))
        .filter(PlatformLoan.status == "charged_off"),
        f,
    ).group_by("p").order_by("p")
    charged_off = dict((p, int(v)) for p, v in co_q.all())

    # Assemble profit + profit% per period (interest earned − charge-offs).
    profit_series, profit_pct_series = [], []
    disb_map = dict(disbursed)
    for p, earned in interest_earned:
        co = charged_off.get(p, 0)
        prof = earned - co
        profit_series.append((p, prof))
        profit_pct_series.append((p, R.pct(prof, disb_map.get(p, 0)) or 0.0))

    # Portfolio size is a point-in-time snapshot (live outstanding), not a series
    # bucket — we still expose it as the latest series point for the trendline
    # shape, plus the scalar total.
    size_total = _scope_loans(
        db.query(func.coalesce(func.sum(PlatformLoan.principal_balance_cents), 0))
        .filter(PlatformLoan.status.in_(_LIVE)),
        f,
    ).scalar()

    def _pts(pairs):
        pairs = pairs[-f.periods:]
        return [SeriesPoint(period=p, value=float(v)) for p, v in pairs]

    # Portfolio size is a snapshot, not a per-period series; expose it as a single
    # trailing point (labelled with the latest origination period) for chart shape.
    size_point = [(disbursed[-1][0], int(size_total or 0))] if disbursed else []
    series = {
        "portfolio_size_cents": _pts(size_point),
        "disbursed_cents": _pts(disbursed),
        "repaid_cents": _pts(repaid),
        "profit_cents": _pts(profit_series),
        "profit_pct": _pts(profit_pct_series),
    }
    totals = {
        "portfolio_size_cents": int(size_total or 0),
        "disbursed_cents": sum(v for _p, v in disbursed),
        "repaid_cents": sum(v for _p, v in repaid),
        "profit_cents": sum(v for _p, v in profit_series),
        "profit_pct": R.pct(sum(v for _p, v in profit_series),
                            sum(v for _p, v in disbursed)),
    }
    deltas = {
        "disbursed_cents": R.series_delta(disbursed),
        "repaid_cents": R.series_delta(repaid),
        "profit_cents": R.series_delta(profit_series),
        "profit_pct": R.series_delta(profit_pct_series),
    }
    return PortfolioReport(
        interval=f.interval, totals=totals, series=series, deltas=deltas,
        notes=[
            "fees are not separately modeled (schedule total = principal + interest); "
            "profit reflects interest earned less charge-offs only.",
            "portfolio_size_cents is a point-in-time snapshot of live outstanding, not a "
            "historical per-period series.",
        ],
    )


# --------------------------------------------------------------------------
# 2) COLLECTIONS — 1-30 / 31-60 / 61-90 / >91 ageing buckets (#, $, %), write-offs.
# --------------------------------------------------------------------------


class CollectionsReport(BaseModel):
    as_of: date
    total_outstanding_cents: int
    buckets: dict
    write_offs: dict
    notes: list[str] = []


@router.get("/reports/collections", response_model=CollectionsReport)
def report_collections(f: ReportFilters = Depends(), db: Session = Depends(get_db)):
    """Delinquency aging by potential-loss bucket, plus write-offs.

    Aging is derived from the oldest unpaid/overdue schedule installment per loan:
    days-past-due = today - earliest overdue due_date. Buckets are Dave's
    platform-wide ageing vocabulary — ``1-30 / 31-60 / 61-90 / 91plus`` (upper
    bounds inclusive; the not-late ``good`` class is omitted from the table).
    Boundaries come from ``delinquency_buckets.get_policy(db)`` so they are
    admin-tunable.
    """
    today = datetime.now(timezone.utc).date()

    # Earliest overdue installment per loan (schedule rows not fully paid, due in
    # the past). One row per loan carrying its worst days-past-due + live balance.
    overdue_item = (
        db.query(
            PlatformLoanScheduleItem.loan_id.label("loan_id"),
            func.min(PlatformLoanScheduleItem.due_date).label("oldest_due"),
        )
        .filter(PlatformLoanScheduleItem.status.in_(("scheduled", "partial", "late")))
        .filter(PlatformLoanScheduleItem.due_date < today)
        .group_by(PlatformLoanScheduleItem.loan_id)
        .subquery()
    )
    rows_q = _scope_loans(
        db.query(overdue_item.c.oldest_due, PlatformLoan.principal_balance_cents)
        .join(PlatformLoan, PlatformLoan.id == overdue_item.c.loan_id)
        .filter(PlatformLoan.status.in_(_LIVE)),
        f,
    )

    policy = dq.get_policy(db)
    buckets = {b: {"count": 0, "amount_cents": 0} for b in R.COLLECTIONS_BUCKETS}
    for oldest_due, balance in rows_q.all():
        dpd = (today - oldest_due).days if oldest_due else 0
        b = R.collections_bucket(dpd, policy)
        if b == R.BUCKET_GOOD:
            continue
        buckets[b]["count"] += 1
        buckets[b]["amount_cents"] += int(balance or 0)

    total_outstanding = _scope_loans(
        db.query(func.coalesce(func.sum(PlatformLoan.principal_balance_cents), 0))
        .filter(PlatformLoan.status.in_(_LIVE)),
        f,
    ).scalar()

    co_count, co_amount = _scope_loans(
        db.query(func.count(PlatformLoan.id),
                 func.coalesce(func.sum(PlatformLoan.principal_cents), 0))
        .filter(PlatformLoan.status == "charged_off"),
        f,
    ).one()

    return CollectionsReport(
        as_of=today,
        total_outstanding_cents=int(total_outstanding or 0),
        buckets=R.bucket_summary(buckets, int(total_outstanding or 0)),
        write_offs={"count": int(co_count), "amount_cents": int(co_amount)},
        notes=[
            "aging uses the oldest overdue schedule installment per live loan; "
            "loans with no overdue installment are current and excluded from buckets.",
        ],
    )


# --------------------------------------------------------------------------
# 3) APPLICATIONS — funnel #/% , amount bands, risk ranks, repaid-vs-disbursed,
#    approved-vs-rejected, active vs portfolio-size over time, repayment split.
# --------------------------------------------------------------------------


class ApplicationsReport(BaseModel):
    interval: str
    funnel: dict
    by_amount_band: dict
    by_risk_rank: dict
    repaid_vs_disbursed: dict
    active_vs_portfolio_series: list[dict]
    repayment_split_series: list[dict]
    notes: list[str] = []


@router.get("/reports/applications", response_model=ApplicationsReport)
def report_applications(f: ReportFilters = Depends(), db: Session = Depends(get_db)):
    """Application funnel + loan distribution + repayment composition."""
    # Funnel: total / approved / activated / rejected.
    status_rows = _scope_apps(
        db.query(PlatformCreditApplication.status, func.count(PlatformCreditApplication.id)),
        f,
    ).group_by(PlatformCreditApplication.status).all()
    by_status = {s: int(c) for s, c in status_rows}
    total_apps = sum(by_status.values())
    approved = by_status.get("approved", 0)
    rejected = by_status.get("rejected", 0)

    # "Activated" = approved applications that produced a funded loan.
    activated = _scope_loans(
        db.query(func.count(func.distinct(PlatformLoan.application_id)))
        .filter(PlatformLoan.status.in_(_FUNDED)),
        f,
    ).scalar()

    funnel = {
        "total": total_apps,
        "approved": {"count": approved, "pct": R.pct(approved, total_apps)},
        "activated": {"count": int(activated or 0), "pct": R.pct(int(activated or 0), total_apps)},
        "rejected": {"count": rejected, "pct": R.pct(rejected, total_apps)},
        "approved_vs_rejected": {"approved": approved, "rejected": rejected},
    }

    # Loans by amount band.
    band_rows = _scope_loans(
        db.query(PlatformLoan.principal_cents).filter(PlatformLoan.status.in_(_FUNDED)),
        f,
    ).all()
    bands = R.empty_amount_bands()
    for (principal,) in band_rows:
        lbl = R.amount_band(int(principal or 0))
        bands[lbl]["count"] += 1
        bands[lbl]["principal_cents"] += int(principal or 0)

    # Repaid vs disbursed (totals).
    disbursed_total = _scope_loans(
        db.query(func.coalesce(func.sum(PlatformLoan.principal_cents), 0))
        .filter(PlatformLoan.status.in_(_FUNDED)),
        f,
    ).scalar()
    repaid_total = _scope_loans(
        db.query(func.coalesce(func.sum(PlatformLoanPayment.amount_cents), 0))
        .join(PlatformLoan, PlatformLoanPayment.loan_id == PlatformLoan.id),
        f,
    ).scalar()

    # Active-loans-# vs portfolio-size-$ over time (by origination period).
    per = _period_key(_ORIG, f.trunc)
    avp_rows = _scope_loans(
        db.query(per.label("p"),
                 func.count(PlatformLoan.id),
                 func.coalesce(func.sum(PlatformLoan.principal_balance_cents), 0))
        .filter(PlatformLoan.status.in_(_LIVE)),
        f,
    ).group_by("p").order_by("p").all()
    active_vs_portfolio = [
        {"period": p, "active_loans": int(n), "portfolio_size_cents": int(bal)}
        for p, n, bal in avp_rows[-f.periods:]
    ]

    # Repayment per interval split into total / principal / interest / fees.
    from app.models.platform.loan import PlatformLoanStatement

    stmt_per = _period_key(PlatformLoanStatement.period_end, f.trunc)
    split_rows = _scope_loans(
        db.query(stmt_per.label("p"),
                 func.coalesce(func.sum(PlatformLoanStatement.principal_paid_cents), 0),
                 func.coalesce(func.sum(PlatformLoanStatement.interest_paid_cents), 0))
        .join(PlatformLoan, PlatformLoanStatement.loan_id == PlatformLoan.id),
        f,
    ).group_by("p").order_by("p").all()
    repayment_split = [
        {
            "period": p,
            "principal_cents": int(prin),
            "interest_cents": int(intr),
            "fees_cents": 0,  # not separately modeled — see notes
            "total_cents": int(prin) + int(intr),
        }
        for p, prin, intr in split_rows[-f.periods:]
    ]

    return ApplicationsReport(
        interval=f.interval,
        funnel=funnel,
        by_amount_band=bands,
        by_risk_rank=_by_risk_rank(db, f),
        repaid_vs_disbursed={
            "disbursed_cents": int(disbursed_total or 0),
            "repaid_cents": int(repaid_total or 0),
            "repaid_pct_of_disbursed": R.pct(int(repaid_total or 0), int(disbursed_total or 0)),
        },
        active_vs_portfolio_series=active_vs_portfolio,
        repayment_split_series=repayment_split,
        notes=[
            "fees_cents is 0 in the repayment split — fees are not modeled separately "
            "from interest in the amortization schedule/statements.",
            "by_risk_rank is now computed from the persisted risk-score model "
            "(migration 073); loans whose application has no reconstructable score "
            "are reported under by_risk_rank.unscored rather than folded into a band.",
        ],
    )


def _by_risk_rank(db: Session, f: ReportFilters) -> dict:
    """'Number of loans grouped by risk level' — real, off the score model.

    Keeps the legacy ``ranks`` key (the FE's ``PendingData`` guard reads it) and
    adds Dave's five named segments alongside. Both are the same population."""
    from app.services.metrics import risk_reports as RR

    payload = RR.loans_by_risk_band(db, f)
    return {
        "ranks": {
            b["risk_level"]: {
                "count": b["count"], "principal_cents": b["principal_cents"]
            }
            for b in payload["bands"]
        },
        "bands": payload["bands"],
        "unscored": payload["unscored"],
        "as_of": payload["as_of"],
        "notes": "; ".join(payload["notes"]),
    }


# --------------------------------------------------------------------------
# 4) OPERATIONAL — business status (new/returning borrowers), performing vs
#    non-performing, closed loans (paid-in-full / written-off / transferred-out).
# --------------------------------------------------------------------------


class OperationalReport(BaseModel):
    business_status: dict
    performing: dict
    non_performing: dict
    closed: dict
    notes: list[str] = []


@router.get("/reports/operational", response_model=OperationalReport)
def report_operational(f: ReportFilters = Depends(), db: Session = Depends(get_db)):
    """Operating-book status: new/returning borrowers, performing split, closures."""
    # New vs returning: a borrower (patient) is "returning" if they have >1 funded
    # loan; "new" on their first. We pull funded loans with their patient + origination
    # time and classify each by whether it was that patient's first funded loan.
    q = (
        db.query(PlatformCreditApplication.patient_id, PlatformLoan.status,
                 PlatformLoan.principal_cents, PlatformLoan.principal_balance_cents,
                 PlatformLoan.annual_rate_bps, _ORIG.label("orig"))
        .join(PlatformCreditApplication, PlatformLoan.application_id == PlatformCreditApplication.id)
        .filter(PlatformLoan.status.in_(_FUNDED))
    )
    if f.vendor_scope is not None:
        q = q.filter(PlatformCreditApplication.vendor_id.in_(f.vendor_scope))
    if f.province is not None:
        q = q.join(Vendor, PlatformCreditApplication.vendor_id == Vendor.id).filter(
            Vendor.province == f.province)
    loans = q.all()

    # Order per patient by origination to find each patient's first funded loan.
    seen: set = set()
    biz = {
        "new_borrowers": {"new_loans": 0, "approved": 0, "rejected": 0, "disbursed_cents": 0},
        "returning_borrowers": {"new_loans": 0, "approved": 0, "rejected": 0, "disbursed_cents": 0},
    }
    for pid, status, principal, _bal, _rate, orig in sorted(
        loans, key=lambda r: (str(r[0]), r[5] or datetime.min.replace(tzinfo=timezone.utc))
    ):
        bucket = "new_borrowers" if pid not in seen else "returning_borrowers"
        seen.add(pid)
        biz[bucket]["new_loans"] += 1
        biz[bucket]["approved"] += 1  # a funded loan implies an approved application
        biz[bucket]["disbursed_cents"] += int(principal or 0)

    # Performing (active) vs non-performing (delinquent/charged_off) split.
    def _agg(statuses):
        n, amt, bal, wapr = _scope_loans(
            db.query(func.count(PlatformLoan.id),
                     func.coalesce(func.sum(PlatformLoan.principal_cents), 0),
                     func.coalesce(func.sum(PlatformLoan.principal_balance_cents), 0),
                     func.coalesce(func.avg(PlatformLoan.annual_rate_bps), 0))
            .filter(PlatformLoan.status.in_(statuses)),
            f,
        ).one()
        repaid = _scope_loans(
            db.query(func.coalesce(func.sum(PlatformLoanPayment.amount_cents), 0))
            .join(PlatformLoan, PlatformLoanPayment.loan_id == PlatformLoan.id)
            .filter(PlatformLoan.status.in_(statuses)),
            f,
        ).scalar()
        # earned interest+fees ~ repaid minus principal reduced (principal - balance)
        principal_reduced = int(amt) - int(bal)
        earned = max(int(repaid or 0) - principal_reduced, 0)
        return {
            "count": int(n),
            "amount_cents": int(amt),
            "repaid_cents": int(repaid or 0),
            "earned_interest_fees_cents": earned,
            "avg_interest_bps": int(round(float(wapr))) if n else None,
        }

    performing = _agg(("active",))
    non_performing = _agg(("delinquent", "charged_off"))

    # Closed loans: paid-in-full / written-off / transferred-out.
    def _closed(statuses):
        n, amt = _scope_loans(
            db.query(func.count(PlatformLoan.id),
                     func.coalesce(func.sum(PlatformLoan.principal_cents), 0))
            .filter(PlatformLoan.status.in_(statuses)),
            f,
        ).one()
        repaid = _scope_loans(
            db.query(func.coalesce(func.sum(PlatformLoanPayment.amount_cents), 0))
            .join(PlatformLoan, PlatformLoanPayment.loan_id == PlatformLoan.id)
            .filter(PlatformLoan.status.in_(statuses)),
            f,
        ).scalar()
        avg_earned = None
        if n:
            avg_earned = int(round((int(repaid or 0) - int(amt)) / int(n)))
        return {"count": int(n), "amount_cents": int(amt), "avg_earned_cents": avg_earned}

    closed = {
        "paid_in_full": _closed(("paid_off",)),
        "written_off": _closed(("charged_off",)),
        # No 'transferred_out' loan status exists in the enum — return null, don't fake.
        "transferred_out": {"count": 0, "amount_cents": 0, "avg_earned_cents": None},
    }

    return OperationalReport(
        business_status=biz,
        performing=performing,
        non_performing=non_performing,
        closed=closed,
        notes=[
            "new vs returning is by patient's first-ever funded loan; a funded loan "
            "implies an approved application (rejected counts are 0 in this view).",
            "earned_interest_fees is approximated as payments received less principal "
            "reduced; fees are not modeled separately.",
            "transferred_out is null/0 — no such loan status exists in the schema.",
        ],
    )


# --------------------------------------------------------------------------
# 5) RISK — delinquency performance buckets (1-30/31-60/61-90/>91), write-off trend.
# --------------------------------------------------------------------------


class RiskReport(BaseModel):
    interval: str
    as_of: date
    delinquency_buckets: dict
    write_off_trend: list[dict]
    notes: list[str] = []


@router.get("/reports/risk", response_model=RiskReport)
def report_risk(f: ReportFilters = Depends(), db: Session = Depends(get_db)):
    """Delinquency-performance aging buckets + charge-off trend over intervals."""
    today = datetime.now(timezone.utc).date()

    overdue_item = (
        db.query(
            PlatformLoanScheduleItem.loan_id.label("loan_id"),
            func.min(PlatformLoanScheduleItem.due_date).label("oldest_due"),
        )
        .filter(PlatformLoanScheduleItem.status.in_(("scheduled", "partial", "late")))
        .filter(PlatformLoanScheduleItem.due_date < today)
        .group_by(PlatformLoanScheduleItem.loan_id)
        .subquery()
    )
    rows = _scope_loans(
        db.query(overdue_item.c.oldest_due, PlatformLoan.principal_balance_cents)
        .join(PlatformLoan, PlatformLoan.id == overdue_item.c.loan_id)
        .filter(PlatformLoan.status.in_(_LIVE)),
        f,
    ).all()

    policy = dq.get_policy(db)
    buckets = {b: {"count": 0, "amount_cents": 0} for b in R.DELINQUENCY_BUCKETS}
    for oldest_due, balance in rows:
        dpd = (today - oldest_due).days if oldest_due else 0
        b = R.days_past_due_bucket(dpd, policy)
        if b == R.BUCKET_GOOD:
            continue
        buckets[b]["count"] += 1
        buckets[b]["amount_cents"] += int(balance or 0)

    total_outstanding = _scope_loans(
        db.query(func.coalesce(func.sum(PlatformLoan.principal_balance_cents), 0))
        .filter(PlatformLoan.status.in_(_LIVE)),
        f,
    ).scalar()

    # Write-off trend: charged-off principal bucketed by origination period.
    per = _period_key(_ORIG, f.trunc)
    wo_rows = _scope_loans(
        db.query(per.label("p"), func.count(PlatformLoan.id),
                 func.coalesce(func.sum(PlatformLoan.principal_cents), 0))
        .filter(PlatformLoan.status == "charged_off"),
        f,
    ).group_by("p").order_by("p").all()
    write_off_trend = [
        {"period": p, "count": int(n), "amount_cents": int(v)}
        for p, n, v in wo_rows[-f.periods:]
    ]

    return RiskReport(
        interval=f.interval,
        as_of=today,
        delinquency_buckets=R.bucket_summary(buckets, int(total_outstanding or 0)),
        write_off_trend=write_off_trend,
        notes=["delinquency aging derived from oldest overdue schedule installment per live loan."],
    )


# --------------------------------------------------------------------------
# 6) SCORING — Dave's four tables: System stability (PSI), Scorecard accuracy,
#    Delinquency performance by band, Final score. Real computations off the
#    persisted risk-score model (migration 073); was a hard-coded null stub.
# --------------------------------------------------------------------------


class ScoringReport(BaseModel):
    window: dict
    system_stability: dict
    scorecard_accuracy: dict
    delinquency_performance: dict
    final_score: dict
    # Back-compat scalar for any caller still reading the old stub shape.
    scorecard_stability: Optional[float] = None
    notes: list[str]


def _scoring_window(
    from_date: Optional[date], to_date: Optional[date], days: int
) -> tuple[datetime, datetime]:
    """Resolve the reporting window. Defaults to Dave's 'Last month' (30 days)."""
    end = (
        datetime.combine(to_date, datetime.min.time(), tzinfo=timezone.utc)
        + timedelta(days=1)
        if to_date is not None
        else datetime.now(timezone.utc)
    )
    start = (
        datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc)
        if from_date is not None
        else end - timedelta(days=days)
    )
    return (start, end)


@router.get("/reports/scoring", response_model=ScoringReport)
def report_scoring(
    f: ReportFilters = Depends(),
    from_date: Optional[date] = Query(
        None, alias="from", description="Window start (inclusive, ISO date)."
    ),
    to_date: Optional[date] = Query(
        None, alias="to", description="Window end (inclusive, ISO date)."
    ),
    days: int = Query(
        30, ge=1, le=3650, description="Window length in days when from/to are omitted."
    ),
    db: Session = Depends(get_db),
):
    """Reports → Scoring: Dave's four tables, computed off the persisted score model.

    Previously a hard-coded ``null`` stub — there was no persisted numeric score
    or scorecard version to compute anything from. Migration 073
    (``platform_application_risk_scores``) supplies both, so these are now real:

    * ``system_stability``        — PSI: ``Actual %``, ``Expected %``, ``(A-E)``,
      ``(A/E)``, ``Ln(A/E)``, ``Index`` per risk level + a RAG-coloured total.
      Expected = the segment distribution BEFORE the window; Actual = inside it.
      Degenerate rows (0% actual → ``-∞``) are shipped visibly, as Dave ships them.
    * ``scorecard_accuracy``      — ``Accounts / Bad / Bad rate / Expected bad rate``.
    * ``delinquency_performance`` — ``Accounts / good / 1-30 / 31-60 / 61-90 /
      91plus / written_off``, in BOTH ``#`` and ``%`` (his unit toggle), using the
      corrected upper-inclusive bucket vocabulary from #200.
    * ``final_score``             — ``Applicants / Approved / Approved % /
      Low-side % / High-side %`` — the override columns the dual system-vs-
      underwriter decision record exists to make computable.
    """
    from app.services.metrics import risk_reports as RR

    window_start, window_end = _scoring_window(from_date, to_date, days)
    stability = RR.system_stability(db, f, window_start=window_start, window_end=window_end)
    return ScoringReport(
        window={"from": window_start.isoformat(), "to": window_end.isoformat()},
        system_stability=stability,
        scorecard_accuracy=RR.scorecard_accuracy(db, f),
        delinquency_performance=RR.delinquency_by_band(db, f),
        final_score=RR.final_score(db, f, window_start=window_start, window_end=window_end),
        scorecard_stability=stability["total_psi"],
        notes=[
            "Computed from platform_application_risk_scores (migration 073) — the "
            "immutable per-application score snapshot written by the flow engine.",
            "Backfilled rows (score_source='backfill') are EXCLUDED from these four "
            "tables: a reconstructed row has no reliable score, so counting it would "
            "corrupt every rate here.",
        ],
    )


class RiskBandReport(BaseModel):
    as_of: str
    bands: list[dict]
    unscored: dict
    notes: list[str] = []


@router.get("/reports/risk-bands", response_model=RiskBandReport)
def report_risk_bands(f: ReportFilters = Depends(), db: Session = Depends(get_db)):
    """Funded loans grouped by risk level — Portfolio report #4 ("Number of loans
    grouped by risk level"), and the segmentation the Risks "Delinquency
    performance" chart groups its bars by.

    Count, originated principal and outstanding balance per one of Dave's five
    risk segments, plus an honest ``unscored`` residual for loans whose
    application carries no reconstructable score."""
    from app.services.metrics import risk_reports as RR

    return RiskBandReport(**RR.loans_by_risk_band(db, f))


# --------------------------------------------------------------------------
# 7) UNDERWRITING — AI vs user underwriting performance + override rate.
# --------------------------------------------------------------------------


class UnderwritingReport(BaseModel):
    ai: dict
    user: dict
    override_rate: dict
    notes: list[str] = []


@router.get("/reports/underwriting", response_model=UnderwritingReport)
def report_underwriting(f: ReportFilters = Depends(), db: Session = Depends(get_db)):
    """AI (auto) vs user (manual) underwriting performance + override rate.

    ``decision_by`` distinguishes the two: 'auto' = AI-decisioned by the flow
    engine; anything else (a staff/admin actor id) = a human override/manual
    decision (set by the admin-actions path). Performance = the realized outcome
    of the loans each path produced (delinquency + charge-off share).
    """
    decided_by = PlatformCreditApplication.decision_by
    is_auto = decided_by == "auto"

    def _uw(is_ai: bool):
        cond = is_auto if is_ai else (decided_by.isnot(None) & (decided_by != "auto"))
        base = _scope_apps(
            db.query(func.count(PlatformCreditApplication.id))
            .filter(PlatformCreditApplication.decision_at.isnot(None))
            .filter(cond),
            f,
        )
        decided = base.scalar() or 0
        # Outcome of loans from these applications.
        loan_q = (
            db.query(PlatformLoan.status, func.count(PlatformLoan.id))
            .join(PlatformCreditApplication,
                  PlatformLoan.application_id == PlatformCreditApplication.id)
            .filter(cond)
        )
        if f.vendor_scope is not None:
            loan_q = loan_q.filter(PlatformCreditApplication.vendor_id.in_(f.vendor_scope))
        if f.province is not None:
            loan_q = loan_q.join(Vendor, PlatformCreditApplication.vendor_id == Vendor.id).filter(
                Vendor.province == f.province)
        by_status = {s: int(c) for s, c in loan_q.group_by(PlatformLoan.status).all()}
        funded = sum(by_status.get(s, 0) for s in _FUNDED)
        bad = by_status.get("delinquent", 0) + by_status.get("charged_off", 0)
        return {
            "decided_count": int(decided),
            "funded_count": funded,
            "delinquent_or_charged_off": bad,
            "bad_rate_pct": R.pct(bad, funded),
        }

    ai = _uw(True)
    user = _uw(False)

    # Override rate: share of decisioned applications resolved by a human (not auto).
    total_decided = _scope_apps(
        db.query(func.count(PlatformCreditApplication.id))
        .filter(PlatformCreditApplication.decision_at.isnot(None)),
        f,
    ).scalar() or 0
    human = user["decided_count"]

    return UnderwritingReport(
        ai=ai,
        user=user,
        override_rate={
            "human_decided": human,
            "total_decided": int(total_decided),
            "rate_pct": R.pct(human, int(total_decided)),
        },
        notes=[
            "AI = decision_by == 'auto' (flow-engine decision); user = a human actor id "
            "on decision_by (manual/override via the admin-actions path).",
            "performance is the realized bad-rate (delinquent+charged_off / funded) of "
            "loans each path produced.",
        ],
    )
