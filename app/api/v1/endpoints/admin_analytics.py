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

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.platform.loan import PlatformLoan, PlatformLoanPayment, PlatformLoanScheduleItem

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])

# Loans that are still carrying a live balance we service against.
_LIVE = ("active", "delinquent")
# The origination date of a loan: when it actually funded, else when it was booked.
_ORIG = func.coalesce(PlatformLoan.disbursed_at, PlatformLoan.created_at)


def _ratio(num: int | None, den: int | None) -> float | None:
    num = num or 0
    den = den or 0
    return round(num / den, 4) if den else None


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
def portfolio(db: Session = Depends(get_db)):
    originated_count, originated_principal = db.query(
        func.count(PlatformLoan.id), func.coalesce(func.sum(PlatformLoan.principal_cents), 0)
    ).one()

    outstanding, active_loans, wapr_num = db.query(
        func.coalesce(func.sum(PlatformLoan.principal_balance_cents), 0),
        func.count(PlatformLoan.id),
        func.coalesce(func.sum(PlatformLoan.principal_balance_cents * PlatformLoan.annual_rate_bps), 0),
    ).filter(PlatformLoan.status.in_(_LIVE)).one()

    delinquent_principal = db.query(
        func.coalesce(func.sum(PlatformLoan.principal_balance_cents), 0)
    ).filter(PlatformLoan.status == "delinquent").scalar()

    co_count, co_principal = db.query(
        func.count(PlatformLoan.id), func.coalesce(func.sum(PlatformLoan.principal_cents), 0)
    ).filter(PlatformLoan.status == "charged_off").one()

    paid_off = db.query(func.count(PlatformLoan.id)).filter(PlatformLoan.status == "paid_off").scalar()
    avg_size = db.query(func.avg(PlatformLoan.principal_cents)).scalar()

    by_status = {
        s: c for s, c in db.query(PlatformLoan.status, func.count(PlatformLoan.id))
        .group_by(PlatformLoan.status).all()
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
def vintage(db: Session = Depends(get_db)):
    """Origination cohorts by month — the vintage curve of how each book ages."""
    month = func.to_char(func.date_trunc("month", _ORIG), "YYYY-MM")
    is_co = PlatformLoan.status == "charged_off"
    rows = (
        db.query(
            month.label("cohort"),
            func.count(PlatformLoan.id),
            func.coalesce(func.sum(PlatformLoan.principal_cents), 0),
            func.coalesce(func.sum(
                case((PlatformLoan.status.in_(_LIVE), PlatformLoan.principal_balance_cents), else_=0)), 0),
            func.coalesce(func.sum(case((is_co, 1), else_=0)), 0),
            func.coalesce(func.sum(case((is_co, PlatformLoan.principal_cents), else_=0)), 0),
            func.coalesce(func.sum(case((PlatformLoan.status == "delinquent", 1), else_=0)), 0),
        )
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
def originations(months: int = 12, db: Session = Depends(get_db)):
    """Monthly origination trend (funded count + volume), most recent last."""
    month = func.to_char(func.date_trunc("month", _ORIG), "YYYY-MM")
    rows = (
        db.query(month.label("m"), func.count(PlatformLoan.id),
                 func.coalesce(func.sum(PlatformLoan.principal_cents), 0))
        .group_by("m").order_by("m").all()
    )
    points = [OriginationPoint(month=m, count=n, principal_cents=int(p)) for m, n, p in rows]
    return points[-months:] if months and months > 0 else points


class CeiPoint(BaseModel):
    month: str
    collected_cents: int
    scheduled_due_cents: int
    cei: float | None   # collected / scheduled due (collection-effectiveness proxy)


@router.get("/cei", response_model=list[CeiPoint])
def cei(months: int = 6, db: Session = Depends(get_db)):
    """Collection-effectiveness proxy per month: payments collected vs amount due."""
    today = datetime.now(timezone.utc).date()
    # First-of-month, `months` back.
    start_month = (today.replace(day=1) - timedelta(days=31 * max(months - 1, 0))).replace(day=1)

    pay_m = func.to_char(func.date_trunc("month", PlatformLoanPayment.received_at), "YYYY-MM")
    collected = {
        m: int(v) for m, v in db.query(pay_m, func.coalesce(func.sum(PlatformLoanPayment.amount_cents), 0))
        .filter(PlatformLoanPayment.received_at >= start_month).group_by(pay_m).all()
    }
    due_m = func.to_char(func.date_trunc("month", PlatformLoanScheduleItem.due_date), "YYYY-MM")
    due = {
        m: int(v) for m, v in db.query(due_m, func.coalesce(func.sum(PlatformLoanScheduleItem.total_cents), 0))
        .filter(PlatformLoanScheduleItem.due_date >= start_month).group_by(due_m).all()
    }

    out = []
    for m in sorted(set(collected) | set(due)):
        c, d = collected.get(m, 0), due.get(m, 0)
        out.append(CeiPoint(month=m, collected_cents=c, scheduled_due_cents=d, cei=_ratio(c, d)))
    return out
