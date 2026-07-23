"""WS-H reports depth — the video-05 report set Dave re-specified (full parity).

Mounted under the same ``/admin/analytics`` prefix as the phase-4 suite:

  * /reports/profit-split      gross / vendor / PaySpyre revenue split, per
                               period AND per vendor (Dave's dropdown ask)
  * /reports/buckets           month-end bucket time series (CML → POT30/60/90
                               → default / insolvency / written-off) + debt-roll
                               % between adjacent buckets — replaces "bad debt"
  * /reports/officer-overrides per-underwriting-officer decisions + overrides
                               (high-side / low-side) + realized bad outcomes
  * /reports/vendor-overrides  vendor "request reprocessing" overrides → what
                               eventually happened (Dave's new-report ask)
  * /reports/ai-decisioning    automated vs human decision share over time +
                               outcome performance by decision source
  * /reports/geo               originations / portfolio / delinquency by
                               province, city and FSA (heat-map data)

All endpoints honor the shared ``ReportFilters`` (interval + vendor
multi-select rollup + province) and are admin/staff gated. Money in integer
cents; shares in bps or ``_pct`` percentages. SQL is entirely SQLAlchemy
expression API over whitelisted vocabulary — no string-built SQL.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.core.config import settings
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanDelinquencySnapshot,
    PlatformLoanTransaction,
)
from app.api.v1.endpoints.admin_analytics import (
    ReportFilters,
    _period_key,
    _scope_apps,
)
from app.services.metrics import analytics_reports as R
from app.services.metrics import reports_depth as RD

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])

_FUNDED = ("active", "delinquent", "paid_off", "charged_off")
_LIVE = ("active", "delinquent")


def _apply_app_scope(q, f: ReportFilters):
    """Vendor/province filters for queries that ALREADY join the application
    (outer). Mirrors ``_scope_loans`` semantics without a second join."""
    if f.vendor_scope is not None:
        q = q.filter(PlatformCreditApplication.vendor_id.in_(f.vendor_scope))
    if f.province is not None:
        q = q.join(Vendor, PlatformCreditApplication.vendor_id == Vendor.id)
        q = q.filter(Vendor.province == f.province)
    return q


def _vendor_names(db: Session, vendor_ids) -> dict:
    ids = [v for v in vendor_ids if v is not None]
    if not ids:
        return {}
    return {
        vid: name
        for vid, name in db.query(Vendor.id, Vendor.business_name)
        .filter(Vendor.id.in_(ids))
        .all()
    }


# ---------------------------------------------------------------------------
# 1) PROFIT SPLIT — Dave: "a drop down list here of gross or vendor or PaySpyre
#    so that we can differentiate and understand our profitability per vendor
#    and know how much that vendor has made themselves using our services."
# ---------------------------------------------------------------------------


class ProfitSplitReport(BaseModel):
    interval: str
    series: list[dict]
    by_vendor: list[dict]
    totals: dict
    notes: list[str] = []


@router.get("/reports/profit-split", response_model=ProfitSplitReport)
def report_profit_split(f: ReportFilters = Depends(), db: Session = Depends(get_db)):
    """Gross revenue / vendor share / PaySpyre share, per period and per vendor.

    * Gross revenue = interest + fees COLLECTED, from the immutable money
      ledger (payment rows net of compensating reversal rows), bucketed by
      effective date. Ledger-less pre-cutover loans have no revenue rows here
      (documented in notes — the ledger is the revenue source of truth).
    * The split is derived from the originating product's ``funding_source``
      (see ``reports_depth.DEFAULT_VENDOR_SHARE_BPS``) with the
      ``PROFIT_SPLIT_VENDOR_SHARE_BPS`` settings override — a structural
      derivation flagged for Dave, not a negotiated split.
    """
    overrides = RD.parse_share_overrides(settings.PROFIT_SPLIT_VENDOR_SHARE_BPS)

    period = _period_key(PlatformLoanTransaction.effective_date, f.trunc)
    revenue_cents = PlatformLoanTransaction.interest_cents + PlatformLoanTransaction.fees_cents
    signed = case(
        (PlatformLoanTransaction.txn_type == "payment", revenue_cents),
        else_=-revenue_cents,  # reversal rows net the original back out
    )
    q = (
        db.query(
            period.label("p"),
            PlatformCreditApplication.vendor_id.label("vid"),
            PlatformCreditProduct.funding_source.label("fs"),
            func.coalesce(func.sum(signed), 0).label("gross"),
        )
        .join(PlatformLoan, PlatformLoanTransaction.loan_id == PlatformLoan.id)
        .outerjoin(
            PlatformCreditApplication,
            PlatformLoan.application_id == PlatformCreditApplication.id,
        )
        .outerjoin(
            PlatformCreditProduct,
            PlatformCreditApplication.credit_product_id == PlatformCreditProduct.id,
        )
        .filter(PlatformLoanTransaction.txn_type.in_(("payment", "reversal")))
    )
    q = _apply_app_scope(q, f)
    rows = q.group_by("p", "vid", "fs").order_by("p").all()

    per_period: dict[str, dict[str, int]] = {}
    per_vendor: dict[Optional[object], dict[str, int]] = {}
    for p, vid, fs, gross in rows:
        gross = int(gross or 0)
        share = RD.vendor_share_bps(fs, overrides)
        vendor_cents, payspyre_cents = RD.split_revenue(gross, share)
        cell = per_period.setdefault(
            p, {"gross_cents": 0, "vendor_cents": 0, "payspyre_cents": 0}
        )
        cell["gross_cents"] += gross
        cell["vendor_cents"] += vendor_cents
        cell["payspyre_cents"] += payspyre_cents
        vcell = per_vendor.setdefault(
            vid, {"gross_cents": 0, "vendor_cents": 0, "payspyre_cents": 0}
        )
        vcell["gross_cents"] += gross
        vcell["vendor_cents"] += vendor_cents
        vcell["payspyre_cents"] += payspyre_cents

    series = [
        {"period": p, **per_period[p]} for p in sorted(per_period)
    ][-f.periods:]

    names = _vendor_names(db, per_vendor.keys())
    by_vendor = sorted(
        (
            {
                "vendor_id": str(vid) if vid is not None else None,
                "vendor_name": names.get(vid, "(unattributed)"),
                **vals,
            }
            for vid, vals in per_vendor.items()
        ),
        key=lambda r: -r["gross_cents"],
    )
    totals = {
        "gross_cents": sum(v["gross_cents"] for v in per_vendor.values()),
        "vendor_cents": sum(v["vendor_cents"] for v in per_vendor.values()),
        "payspyre_cents": sum(v["payspyre_cents"] for v in per_vendor.values()),
    }
    return ProfitSplitReport(
        interval=f.interval,
        series=series,
        by_vendor=by_vendor,
        totals=totals,
        notes=[
            "gross revenue = interest + fees collected per the money ledger (payment "
            "rows net of reversals); pre-ledger loans without transaction rows are "
            "not represented.",
            "split derives from the product funding_source (clinic_self → vendor, "
            "else PaySpyre) with PROFIT_SPLIT_VENDOR_SHARE_BPS overrides — "
            "percentages pending Dave, see PR assumptions.",
        ],
    )


# ---------------------------------------------------------------------------
# 2) BUCKET TIME SERIES + DEBT ROLL — Dave: "we want the different queues going
#    current month late … POT 30s … POT 60s … POT 90s … default … insolvency …
#    written off"; "debt roll is the percentage of current month late that roll
#    into a new month and become POT 30s."
# ---------------------------------------------------------------------------


class BucketReport(BaseModel):
    months: int
    series: list[dict]
    rolls: list[dict]
    notes: list[str] = []


@router.get("/reports/buckets", response_model=BucketReport)
def report_buckets(
    months: int = Query(12, ge=1, le=60),
    f: ReportFilters = Depends(),
    db: Session = Depends(get_db),
):
    """Month-end delinquency-bucket time series + adjacent-bucket debt roll,
    from the ``platform_loan_delinquency_snapshots`` month-end history."""
    today = datetime.now(timezone.utc).date()
    start = (today.replace(day=1) - timedelta(days=31 * max(months - 1, 0))).replace(day=1)

    def _scoped(q):
        if f.needs_app_join:
            q = q.join(
                PlatformLoan,
                PlatformLoanDelinquencySnapshot.loan_id == PlatformLoan.id,
            ).join(
                PlatformCreditApplication,
                PlatformLoan.application_id == PlatformCreditApplication.id,
            )
            q = _apply_app_scope(q, f)
        return q

    agg_rows = (
        _scoped(
            db.query(
                PlatformLoanDelinquencySnapshot.snapshot_month,
                PlatformLoanDelinquencySnapshot.bucket,
                func.count(PlatformLoanDelinquencySnapshot.id),
                func.coalesce(
                    func.sum(PlatformLoanDelinquencySnapshot.outstanding_principal_cents), 0
                ),
                func.coalesce(
                    func.sum(PlatformLoanDelinquencySnapshot.amount_past_due_cents), 0
                ),
            ).filter(PlatformLoanDelinquencySnapshot.snapshot_month >= start)
        )
        .group_by(
            PlatformLoanDelinquencySnapshot.snapshot_month,
            PlatformLoanDelinquencySnapshot.bucket,
        )
        .all()
    )
    series = RD.bucket_series(agg_rows)

    loan_rows = (
        _scoped(
            db.query(
                PlatformLoanDelinquencySnapshot.snapshot_month,
                PlatformLoanDelinquencySnapshot.loan_id,
                PlatformLoanDelinquencySnapshot.bucket,
            ).filter(PlatformLoanDelinquencySnapshot.snapshot_month >= start)
        )
        .all()
    )
    per_month: dict[date, dict[str, str]] = {}
    for month, loan_id, bucket in loan_rows:
        per_month.setdefault(month, {})[str(loan_id)] = bucket
    rolls = RD.roll_series(per_month)

    return BucketReport(
        months=months,
        series=series,
        rolls=rolls,
        notes=[
            "buckets are Dave's month-end snapshots (delinquency_buckets policy); "
            "the current month appears only after its month-end snapshot runs.",
            "roll rates are computed only between calendar-adjacent snapshot months; "
            "roll_rate_bps is integer basis points (10000 = 100%).",
        ],
    )


# ---------------------------------------------------------------------------
# 3) OFFICER DECISIONS + OVERRIDES — TL's "Underwriter monitoring" +
#    "Overrides by underwriters" tables, from the admin decision audit events.
# ---------------------------------------------------------------------------


class OfficerOverridesReport(BaseModel):
    officers: list[dict]
    totals: dict
    notes: list[str] = []


@router.get("/reports/officer-overrides", response_model=OfficerOverridesReport)
def report_officer_overrides(
    f: ReportFilters = Depends(), db: Session = Depends(get_db)
):
    """Per-underwriting-officer decision + override counts and realized outcomes.

    Source of truth: the append-only ``admin_application_decision`` audit
    events (actor = the officer, payload = outcome / override flag / loan id).
    Override direction is derived from the final outcome (approved → low-side,
    declined → high-side) — see ``reports_depth.override_side``.
    """
    ev_q = (
        db.query(
            PlatformEvent.actor,
            PlatformEvent.application_id,
            PlatformEvent.payload,
        )
        .filter(PlatformEvent.event_type == "admin_application_decision")
    )
    if f.needs_app_join:
        ev_q = ev_q.join(
            PlatformCreditApplication,
            PlatformEvent.application_id == PlatformCreditApplication.id,
        )
        ev_q = _apply_app_scope(ev_q, f)
    events = ev_q.all()

    officers: dict[str, dict] = {}
    decided_app_ids: dict[str, set] = {}
    for actor, application_id, payload in events:
        payload = payload or {}
        outcome = payload.get("outcome")
        is_override = bool(payload.get("override"))
        o = officers.setdefault(
            actor,
            {
                "officer": actor,
                "decided": 0,
                "approved": 0,
                "rejected": 0,
                "overrides": 0,
                "high_side": 0,
                "low_side": 0,
                "funded": 0,
                "bad": 0,
            },
        )
        o["decided"] += 1
        if outcome == "approved":
            o["approved"] += 1
        elif outcome == "declined":
            o["rejected"] += 1
        if is_override:
            o["overrides"] += 1
            side = RD.override_side(outcome)
            if side in ("high_side", "low_side"):
                o[side] += 1
        if application_id is not None:
            decided_app_ids.setdefault(actor, set()).add(application_id)

    # Realized outcomes of the loans those decisions produced.
    all_app_ids = sorted({a for ids in decided_app_ids.values() for a in ids}, key=str)
    outcome_by_app: dict = {}
    if all_app_ids:
        for app_id, status, bucket in (
            db.query(
                PlatformLoan.application_id,
                PlatformLoan.status,
                PlatformLoan.current_bucket,
            )
            .filter(PlatformLoan.application_id.in_(all_app_ids))
            .all()
        ):
            outcome_by_app[app_id] = (status, bucket)
    for actor, ids in decided_app_ids.items():
        for app_id in ids:
            got = outcome_by_app.get(app_id)
            if got is None:
                continue
            status, bucket = got
            if status in _FUNDED:
                officers[actor]["funded"] += 1
            if status in ("delinquent", "charged_off") or (
                bucket is not None and bucket != "current"
            ):
                officers[actor]["bad"] += 1

    rows = []
    total_decided = sum(o["decided"] for o in officers.values()) or 0
    for o in sorted(officers.values(), key=lambda x: -x["decided"]):
        o["decided_share_pct"] = R.pct(o["decided"], total_decided)
        o["override_rate_pct"] = R.pct(o["overrides"], o["decided"])
        o["bad_rate_pct"] = R.pct(o["bad"], o["funded"])
        rows.append(o)

    return OfficerOverridesReport(
        officers=rows,
        totals={
            "decided": total_decided,
            "overrides": sum(o["overrides"] for o in officers.values()),
        },
        notes=[
            "source: admin_application_decision audit events (actor = officer); "
            "auto/AI decisions never emit this event — see /reports/ai-decisioning.",
            "override direction is derived from the final outcome (approved → "
            "low-side, declined → high-side); the counterfactual machine score is "
            "not persisted.",
            "bad = the decision's loan is delinquent/charged-off or sits in any "
            "month-end bucket past current.",
        ],
    )


# ---------------------------------------------------------------------------
# 4) VENDOR OVERRIDES — Dave: "a category here for vendor overrides so that we
#    can track all the list of vendors and how many accounts are getting
#    overridden, how many of those are going bad."
# ---------------------------------------------------------------------------


class VendorOverridesReport(BaseModel):
    vendors: list[dict]
    totals: dict
    notes: list[str] = []


@router.get("/reports/vendor-overrides", response_model=VendorOverridesReport)
def report_vendor_overrides(
    f: ReportFilters = Depends(), db: Session = Depends(get_db)
):
    """Vendor "request reprocessing" overrides and their eventual outcomes.

    The vendor override surface is the reprocessing request (the only vendor
    underwriting action, WS-vendor-origination); each request is a durable
    ``vendor_reprocessing_requested`` audit event. This report follows those
    files to their final application status and, when funded, the loan's
    realized performance.
    """
    ev_q = db.query(PlatformEvent.application_id.label("app_id")).filter(
        PlatformEvent.event_type == "vendor_reprocessing_requested",
        PlatformEvent.application_id.isnot(None),
    )
    app_ids = sorted({row.app_id for row in ev_q.all()}, key=str)

    vendors: dict[Optional[object], dict] = {}
    if app_ids:
        apps_q = db.query(
            PlatformCreditApplication.id,
            PlatformCreditApplication.vendor_id,
            PlatformCreditApplication.status,
        ).filter(PlatformCreditApplication.id.in_(app_ids))
        apps_q = _apply_app_scope(apps_q, f)
        apps = apps_q.all()

        loan_by_app: dict = {}
        scoped_ids = [a.id for a in apps]
        if scoped_ids:
            for app_id, status, bucket in (
                db.query(
                    PlatformLoan.application_id,
                    PlatformLoan.status,
                    PlatformLoan.current_bucket,
                )
                .filter(PlatformLoan.application_id.in_(scoped_ids))
                .all()
            ):
                loan_by_app[app_id] = (status, bucket)

        for app_id, vendor_id, app_status in apps:
            v = vendors.setdefault(
                vendor_id,
                {
                    "vendor_id": str(vendor_id) if vendor_id is not None else None,
                    "requested": 0,
                    "approved": 0,
                    "rejected": 0,
                    "in_progress": 0,
                    "funded": 0,
                    "bad": 0,
                },
            )
            v["requested"] += 1
            loan = loan_by_app.get(app_id)
            if app_status == "approved" or loan is not None:
                v["approved"] += 1
            elif app_status == "rejected":
                v["rejected"] += 1
            else:
                v["in_progress"] += 1
            if loan is not None:
                status, bucket = loan
                if status in _FUNDED:
                    v["funded"] += 1
                if status in ("delinquent", "charged_off") or (
                    bucket is not None and bucket != "current"
                ):
                    v["bad"] += 1

    names = _vendor_names(db, vendors.keys())
    rows = []
    for vendor_id, v in vendors.items():
        v["vendor_name"] = names.get(vendor_id, "(unattributed)")
        v["override_success_pct"] = R.pct(v["approved"], v["requested"])
        v["bad_rate_pct"] = R.pct(v["bad"], v["funded"])
        rows.append(v)
    rows.sort(key=lambda r: -r["requested"])

    return VendorOverridesReport(
        vendors=rows,
        totals={
            "requested": sum(v["requested"] for v in vendors.values()),
            "funded": sum(v["funded"] for v in vendors.values()),
            "bad": sum(v["bad"] for v in vendors.values()),
        },
        notes=[
            "a vendor override = a reprocessing request (the only vendor "
            "underwriting action); events are the durable record — the on-file "
            "flag is cleared when staff re-decide.",
            "approved = the file reached approved status or produced a loan after "
            "the request; bad = that loan is delinquent/charged-off or in any "
            "bucket past current.",
        ],
    )


# ---------------------------------------------------------------------------
# 5) AI DECISIONING — share of decisions automated vs human over time, and
#    outcome performance (delinquency by decision source).
# ---------------------------------------------------------------------------


class AiDecisioningReport(BaseModel):
    interval: str
    series: list[dict]
    outcomes: dict
    notes: list[str] = []


@router.get("/reports/ai-decisioning", response_model=AiDecisioningReport)
def report_ai_decisioning(f: ReportFilters = Depends(), db: Session = Depends(get_db)):
    """Automated (flow-engine) vs human decision volume per period + realized
    delinquency by decision source. ``decision_by == 'auto'`` is the AI path;
    any human actor id is a manual/override decision."""
    src = case(
        (PlatformCreditApplication.decision_by == "auto", "auto"), else_="human"
    )
    per = _period_key(PlatformCreditApplication.decision_at, f.trunc)
    dec_rows = (
        _scope_apps(
            db.query(per.label("p"), src.label("s"), func.count(PlatformCreditApplication.id))
            .filter(PlatformCreditApplication.decision_at.isnot(None)),
            f,
        )
        .group_by("p", "s")
        .order_by("p")
        .all()
    )
    per_period: dict[str, dict[str, int]] = {}
    for p, s, n in dec_rows:
        per_period.setdefault(p, {"auto": 0, "human": 0})[s] = int(n)
    series = []
    for p in sorted(per_period)[-f.periods:]:
        auto, human = per_period[p]["auto"], per_period[p]["human"]
        series.append(
            {
                "period": p,
                "auto": auto,
                "human": human,
                "total": auto + human,
                "auto_share_pct": R.pct(auto, auto + human),
            }
        )

    # Realized performance of the loans each path produced, bucket-aware.
    loan_rows = (
        _scope_apps(
            db.query(
                src.label("s"),
                PlatformLoan.status,
                PlatformLoan.current_bucket,
                func.count(PlatformLoan.id),
            )
            .join(
                PlatformCreditApplication,
                PlatformLoan.application_id == PlatformCreditApplication.id,
            )
            .filter(PlatformCreditApplication.decision_at.isnot(None)),
            f,
        )
        .group_by("s", PlatformLoan.status, PlatformLoan.current_bucket)
        .all()
    )
    outcomes: dict[str, dict] = {
        s: {"funded": 0, "bad": 0, "by_bucket": {}} for s in ("auto", "human")
    }
    for s, status, bucket, n in loan_rows:
        n = int(n)
        cell = outcomes[s]
        if status in _FUNDED:
            cell["funded"] += n
        label = bucket or ("delinquent" if status == "delinquent" else "current")
        cell["by_bucket"][label] = cell["by_bucket"].get(label, 0) + n
        if status in ("delinquent", "charged_off") or (
            bucket is not None and bucket != "current"
        ):
            cell["bad"] += n
    for cell in outcomes.values():
        cell["bad_rate_pct"] = R.pct(cell["bad"], cell["funded"])

    return AiDecisioningReport(
        interval=f.interval,
        series=series,
        outcomes=outcomes,
        notes=[
            "auto = decision_by == 'auto' (flow-engine decision); human = a staff "
            "actor id (manual decision or override).",
            "by_bucket uses the loan's current month-end bucket where snapshotted; "
            "loans never snapshotted fall back to a status-derived label.",
        ],
    )


# ---------------------------------------------------------------------------
# 6) GEO — heat-map data: originations / portfolio / delinquency by province,
#    city and FSA. Dave: "the default here should actually be portfolio."
# ---------------------------------------------------------------------------


class GeoReport(BaseModel):
    by_province: list[dict]
    by_city: list[dict]
    by_fsa: list[dict]
    notes: list[str] = []


@router.get("/reports/geo", response_model=GeoReport)
def report_geo(f: ReportFilters = Depends(), db: Session = Depends(get_db)):
    """Geographic aggregation of applications + the loans they produced, keyed
    on the applicant's residence address (province / city / FSA)."""
    rows = (
        _scope_apps(
            db.query(
                PlatformCreditApplication.id,
                PlatformCreditApplication.residence_province,
                PlatformCreditApplication.residence_city,
                PlatformCreditApplication.residence_postal_code,
                PlatformLoan.status,
                PlatformLoan.principal_cents,
                PlatformLoan.principal_balance_cents,
                PlatformLoan.current_bucket,
            ).outerjoin(
                PlatformLoan,
                PlatformLoan.application_id == PlatformCreditApplication.id,
            ),
            f,
        )
        .all()
    )

    def _blank():
        return {
            "applications": 0,
            "funded_count": 0,
            "funded_cents": 0,
            "outstanding_cents": 0,
            "delinquent_count": 0,
            "delinquent_cents": 0,
        }

    groups: dict[str, dict] = {"province": {}, "city": {}, "fsa": {}}
    seen_apps: dict[str, set] = {"province": set(), "city": set(), "fsa": set()}

    for app_id, province, city, postal, status, principal, balance, bucket in rows:
        prov = RD.norm_place(province)
        keys = {
            "province": prov,
            "city": f"{RD.norm_place(city)}, {prov}" if RD.norm_place(city) else None,
            "fsa": RD.fsa(postal),
        }
        for dim, key in keys.items():
            if key is None:
                continue
            cell = groups[dim].setdefault(key, _blank())
            marker = (key, app_id)
            if marker not in seen_apps[dim]:
                seen_apps[dim].add(marker)
                cell["applications"] += 1
            if status in _FUNDED:
                cell["funded_count"] += 1
                cell["funded_cents"] += int(principal or 0)
            if status in _LIVE:
                cell["outstanding_cents"] += int(balance or 0)
            if status is not None and (
                status in ("delinquent", "charged_off")
                or (bucket is not None and bucket != "current")
            ):
                cell["delinquent_count"] += 1
                cell["delinquent_cents"] += int(balance or 0)

    def _rows(dim: str) -> list[dict]:
        out = [{"key": k, **v} for k, v in groups[dim].items()]
        out.sort(key=lambda r: -r["outstanding_cents"])
        return out

    return GeoReport(
        by_province=_rows("province"),
        by_city=_rows("city"),
        by_fsa=_rows("fsa"),
        notes=[
            "keyed on the applicant's residence address from the credit "
            "application; applications without an address (or non-postal codes) "
            "drop out of the affected grouping only.",
            "delinquent = loan status delinquent/charged-off or any month-end "
            "bucket past current; outstanding covers live (active+delinquent) "
            "balances.",
        ],
    )
