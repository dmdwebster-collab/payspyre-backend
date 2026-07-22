"""Reports → Scoring: the four tables, computed off the persisted score model.

Before migration 073 ``GET /admin/analytics/reports/scoring`` returned three
hard-coded ``null``s with an honest TODO, because no numeric score, scorecard
version or score history existed. It does now
(``platform_application_risk_scores``), so these are real computations.

The four tables, exactly as Dave lays them out (video 05 §A5):

1. **System stability** — a Population Stability Index table over his five risk
   segments: ``Actual %``, ``Expected %``, ``(A-E)``, ``(A/E)``, ``Ln(A/E)``,
   ``Index``, plus a RAG-coloured total. Formulas + the degenerate ``-∞`` case
   live in :mod:`app.services.risk_scoring` (``psi_row``), verified against the
   numbers on his screen.
2. **Scorecard accuracy** — ``Accounts``, ``Bad``, ``Bad rate``,
   ``Expected bad rate`` per segment. Expected = the mean calibrated PD of the
   accounts in the segment; Actual = the realised bad rate.
3. **Delinquency performance** — ``Accounts``, ``Current``, ``1-30``, ``31-60``,
   ``61-90``, ``91+``, ``Written off`` per segment, in BOTH ``#`` and ``%``
   (his unit toggle). The bucket vocabulary is the CORRECTED one from #200 —
   ``good / 1-30 / 31-60 / 61-90 / 91plus``, upper bounds INCLUSIVE — routed
   through the single mapper ``delinquency_buckets.aging_bucket``.
4. **Final score** — ``Applicants``, ``Approved``, ``Approved %``,
   ``Low-side %``, ``High-side %``. The two override columns are the reason the
   score model persists the SYSTEM and UNDERWRITER decisions separately; see
   ``risk_scores.classify_override``.

SCOPE + WINDOW. Every table is scoped by the report filters (vendor rollup /
province) through the originating application, and the PSI table additionally
needs a BASELINE: ``expected`` is the segment distribution of everything scored
strictly before the reporting window opens. With no history before the window
the expected distribution is undefined and the table says so rather than
comparing a population against itself.

All SQL is built through the SQLAlchemy ORM/expression API (no string
interpolation — bandit B608 clean).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Query as SAQuery, Session

from app.models.loan import Vendor
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.loan import PlatformLoan, PlatformLoanScheduleItem
from app.models.platform.risk_score import PlatformApplicationRiskScore
from app.services import delinquency_buckets as dq
from app.services import risk_scoring as RS
from app.services import risk_scores as RSVC

#: Segment rows in report order, with Dave's score-range label.
SEGMENT_ROWS: tuple[tuple[str, str, str], ...] = tuple(
    (
        key,
        label,
        f"{lower}-{RS.SCALE_MAX}"
        if idx == 0
        else f"{lower}-{RS.SEGMENT_BANDS[idx - 1][2] - 1}",
    )
    for idx, (key, label, lower) in enumerate(RS.SEGMENT_BANDS)
)

_LIVE = ("active", "delinquent")

#: A "bad" account for the accuracy table: charged off, or 91+ days past due.
BAD_DEFINITION = (
    "charged_off status on the loan, or an oldest overdue installment 91+ days "
    "past due (the corrected 91plus open-ended bucket)"
)


def _scope(q: SAQuery, f: Any) -> SAQuery:
    """Apply the report filters through the originating application."""
    scope = getattr(f, "vendor_scope", None)
    province = getattr(f, "province", None)
    if scope is not None:
        q = q.filter(PlatformCreditApplication.vendor_id.in_(scope))
    if province is not None:
        q = q.join(Vendor, PlatformCreditApplication.vendor_id == Vendor.id).filter(
            Vendor.province == province
        )
    return q


def _scored_rows(
    db: Session,
    f: Any,
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    include_backfilled: bool = False,
) -> list[PlatformApplicationRiskScore]:
    """Current score rows in a window, scoped, with a usable scaled score."""
    q = (
        db.query(PlatformApplicationRiskScore)
        .join(
            PlatformCreditApplication,
            PlatformApplicationRiskScore.application_id == PlatformCreditApplication.id,
        )
        .filter(PlatformApplicationRiskScore.is_current.is_(True))
        .filter(PlatformApplicationRiskScore.risk_segment.isnot(None))
    )
    if not include_backfilled:
        q = q.filter(PlatformApplicationRiskScore.is_backfilled.is_(False))
    if since is not None:
        q = q.filter(PlatformApplicationRiskScore.scored_at >= since)
    if until is not None:
        q = q.filter(PlatformApplicationRiskScore.scored_at < until)
    return _scope(q, f).all()


def _empty_counts() -> dict[str, int]:
    return {key: 0 for key, _label, _range in SEGMENT_ROWS}


def _distribution_pct(counts: dict[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    if total == 0:
        return {k: 0.0 for k in counts}
    return {k: round(100.0 * v / total, 4) for k, v in counts.items()}


# ---------------------------------------------------------------------------
# 1. System stability (PSI)
# ---------------------------------------------------------------------------


def system_stability(
    db: Session, f: Any, *, window_start: datetime, window_end: datetime
) -> dict[str, Any]:
    actual_rows = _scored_rows(db, f, since=window_start, until=window_end)
    baseline_rows = _scored_rows(db, f, until=window_start)

    actual = _empty_counts()
    for row in actual_rows:
        actual[row.risk_segment] = actual.get(row.risk_segment, 0) + 1
    expected = _empty_counts()
    for row in baseline_rows:
        expected[row.risk_segment] = expected.get(row.risk_segment, 0) + 1

    notes: list[str] = []
    if not baseline_rows:
        notes.append(
            "No scored applications exist before the reporting window, so there is "
            "no expected (baseline) distribution to measure drift against. Rows "
            "report actual counts only; PSI is null."
        )
    if not actual_rows:
        notes.append("No applications were scored inside the reporting window.")

    a_pct = _distribution_pct(actual)
    e_pct = _distribution_pct(expected)

    rows: list[dict[str, Any]] = []
    total: Optional[float] = 0.0 if baseline_rows and actual_rows else None
    for idx, (key, label, score_range) in enumerate(SEGMENT_ROWS, start=1):
        if not baseline_rows or not actual_rows:
            rows.append(
                {
                    "index": idx, "segment": key, "risk_level": label,
                    "score": score_range,
                    "actual_count": actual[key], "expected_count": expected[key],
                    "actual_pct": a_pct[key], "expected_pct": e_pct[key],
                    "a_minus_e": None, "a_over_e": None, "ln_a_over_e": None,
                    "psi_index": None, "degenerate": None,
                }
            )
            continue
        cell = RS.psi_row(a_pct[key], e_pct[key])
        if cell["index"] is None:
            total = None  # a degenerate row makes the total undefined (Dave ships -∞)
        elif total is not None:
            total = round(total + cell["index"], 4)
        rows.append(
            {
                "index": idx, "segment": key, "risk_level": label,
                "score": score_range,
                "actual_count": actual[key], "expected_count": expected[key],
                "actual_pct": cell["actual_pct"], "expected_pct": cell["expected_pct"],
                "a_minus_e": cell["a_minus_e"], "a_over_e": cell["a_over_e"],
                "ln_a_over_e": cell["ln_a_over_e"], "psi_index": cell["index"],
                "degenerate": cell["degenerate"],
            }
        )

    return {
        "rows": rows,
        "total_psi": total,
        "rag": RS.psi_rag(total),
        "actual_population": len(actual_rows),
        "expected_population": len(baseline_rows),
        "thresholds": {"stable_below": RS.PSI_STABLE_MAX, "shift_below": RS.PSI_SHIFT_MAX},
        "notes": notes
        + [
            "Index = ((Actual% - Expected%) / 100) * Ln(Actual% / Expected%); the "
            "total is the sum of the per-band indices. A band with 0% actual is "
            "degenerate (-inf) and is reported as such, not hidden.",
            "Expected = the segment distribution of everything scored BEFORE the "
            "reporting window; Actual = the window itself.",
        ],
    }


# ---------------------------------------------------------------------------
# Loan outcome join — shared by tables 2 and 3
# ---------------------------------------------------------------------------


def _loan_outcomes(db: Session, f: Any, as_of: date) -> dict[str, dict[str, Any]]:
    """{application_id: {status, bucket, balance_cents}} for every funded loan."""
    overdue = (
        db.query(
            PlatformLoanScheduleItem.loan_id.label("loan_id"),
            func.min(PlatformLoanScheduleItem.due_date).label("oldest_due"),
        )
        .filter(PlatformLoanScheduleItem.status.in_(("scheduled", "partial", "late")))
        .filter(PlatformLoanScheduleItem.due_date < as_of)
        .group_by(PlatformLoanScheduleItem.loan_id)
        .subquery()
    )
    q = (
        db.query(
            PlatformLoan.application_id,
            PlatformLoan.status,
            PlatformLoan.principal_balance_cents,
            overdue.c.oldest_due,
        )
        .join(
            PlatformCreditApplication,
            PlatformLoan.application_id == PlatformCreditApplication.id,
        )
        .outerjoin(overdue, overdue.c.loan_id == PlatformLoan.id)
    )
    policy = dq.get_policy(db)
    out: dict[str, dict[str, Any]] = {}
    for application_id, status, balance, oldest_due in _scope(q, f).all():
        if status == "charged_off":
            bucket = "written_off"
        elif status in _LIVE:
            dpd = (as_of - oldest_due).days if oldest_due else 0
            bucket = dq.aging_bucket(dpd, policy)
        else:
            bucket = dq.AGING_GOOD
        out[str(application_id)] = {
            "status": status,
            "bucket": bucket,
            "balance_cents": int(balance or 0),
        }
    return out


def _is_bad(outcome: dict[str, Any]) -> bool:
    return outcome["bucket"] in ("written_off", dq.AGING_91_PLUS)


# ---------------------------------------------------------------------------
# 2. Scorecard accuracy
# ---------------------------------------------------------------------------


def scorecard_accuracy(db: Session, f: Any, *, as_of: Optional[date] = None) -> dict[str, Any]:
    as_of = as_of or datetime.now(timezone.utc).date()
    outcomes = _loan_outcomes(db, f, as_of)
    rows_by_segment: dict[str, list[PlatformApplicationRiskScore]] = {
        key: [] for key, _l, _r in SEGMENT_ROWS
    }
    for row in _scored_rows(db, f):
        if str(row.application_id) in outcomes:
            rows_by_segment.setdefault(row.risk_segment, []).append(row)

    table: list[dict[str, Any]] = []
    for idx, (key, label, score_range) in enumerate(SEGMENT_ROWS, start=1):
        members = rows_by_segment.get(key) or []
        accounts = len(members)
        bad = sum(1 for m in members if _is_bad(outcomes[str(m.application_id)]))
        pds = [
            float(m.probability_of_default)
            for m in members
            if m.probability_of_default is not None
        ]
        table.append(
            {
                "index": idx, "segment": key, "risk_level": label, "score": score_range,
                "accounts": accounts,
                "bad": bad,
                "bad_rate_pct": round(100.0 * bad / accounts, 4) if accounts else None,
                "expected_bad_rate_pct": (
                    round(100.0 * sum(pds) / len(pds), 4) if pds else None
                ),
            }
        )
    return {
        "rows": table,
        "as_of": as_of.isoformat(),
        "notes": [
            f"'Bad' = {BAD_DEFINITION}.",
            "Expected bad rate = the mean calibrated probability of default of the "
            "accounts in the band (see pd_calibration on each score row) — a "
            "declared PDO calibration, not a measured historical rate.",
            "Only applications that produced a funded loan are counted; an "
            "application with no loan has no outcome to be right or wrong about.",
        ],
    }


# ---------------------------------------------------------------------------
# 3. Delinquency performance by band
# ---------------------------------------------------------------------------

_DELINQ_COLUMNS: tuple[str, ...] = (
    dq.AGING_GOOD,
    dq.AGING_1_30,
    dq.AGING_31_60,
    dq.AGING_61_90,
    dq.AGING_91_PLUS,
    "written_off",
)


def delinquency_by_band(
    db: Session, f: Any, *, as_of: Optional[date] = None
) -> dict[str, Any]:
    as_of = as_of or datetime.now(timezone.utc).date()
    outcomes = _loan_outcomes(db, f, as_of)
    per_segment: dict[str, dict[str, int]] = {
        key: {c: 0 for c in _DELINQ_COLUMNS} for key, _l, _r in SEGMENT_ROWS
    }
    for row in _scored_rows(db, f):
        outcome = outcomes.get(str(row.application_id))
        if outcome is None:
            continue
        bucket = outcome["bucket"]
        per_segment.setdefault(row.risk_segment, {c: 0 for c in _DELINQ_COLUMNS})
        per_segment[row.risk_segment][bucket] = (
            per_segment[row.risk_segment].get(bucket, 0) + 1
        )

    table: list[dict[str, Any]] = []
    for idx, (key, label, score_range) in enumerate(SEGMENT_ROWS, start=1):
        counts = per_segment.get(key) or {c: 0 for c in _DELINQ_COLUMNS}
        accounts = sum(counts.values())
        table.append(
            {
                "index": idx, "segment": key, "risk_level": label, "score": score_range,
                "accounts": accounts,
                "counts": {c: counts.get(c, 0) for c in _DELINQ_COLUMNS},
                "pct": {
                    c: (round(100.0 * counts.get(c, 0) / accounts, 4) if accounts else None)
                    for c in _DELINQ_COLUMNS
                },
            }
        )
    return {
        "columns": list(_DELINQ_COLUMNS),
        "column_labels": {
            **{
                c: dq.AGING_BUCKET_LABELS.get(c, c)
                for c in _DELINQ_COLUMNS
                if c != "written_off"
            },
            "written_off": "Written off",
        },
        "rows": table,
        "as_of": as_of.isoformat(),
        "notes": [
            "Ageing buckets use the corrected platform vocabulary "
            "(good / 1-30 / 31-60 / 61-90 / 91plus, upper bounds INCLUSIVE, 91+ "
            "open-ended) via delinquency_buckets.aging_bucket — the same mapper "
            "the Risks and servicing surfaces use.",
            "'written_off' follows the loan's charged_off status and supersedes "
            "the ageing bucket.",
            "Both # and % are returned so the unit toggle needs no second call.",
        ],
    }


# ---------------------------------------------------------------------------
# 4. Final score
# ---------------------------------------------------------------------------


def final_score(
    db: Session, f: Any, *, window_start: datetime, window_end: datetime
) -> dict[str, Any]:
    rows = _scored_rows(db, f, since=window_start, until=window_end)
    per_segment: dict[str, dict[str, int]] = {
        key: {"applicants": 0, "approved": 0, "low_side": 0, "high_side": 0}
        for key, _l, _r in SEGMENT_ROWS
    }
    for row in rows:
        cell = per_segment.setdefault(
            row.risk_segment,
            {"applicants": 0, "approved": 0, "low_side": 0, "high_side": 0},
        )
        cell["applicants"] += 1
        final = row.underwriter_decision or row.system_decision
        if final == "approved":
            cell["approved"] += 1
        override = RSVC.classify_override(row.system_decision, row.underwriter_decision)
        if override == RSVC.OVERRIDE_LOW_SIDE:
            cell["low_side"] += 1
        elif override == RSVC.OVERRIDE_HIGH_SIDE:
            cell["high_side"] += 1

    table: list[dict[str, Any]] = []
    for idx, (key, label, score_range) in enumerate(SEGMENT_ROWS, start=1):
        cell = per_segment[key]
        n = cell["applicants"]
        table.append(
            {
                "index": idx, "segment": key, "risk_level": label, "score": score_range,
                "applicants": n,
                "approved": cell["approved"],
                "approved_pct": round(100.0 * cell["approved"] / n, 4) if n else None,
                "low_side_pct": round(100.0 * cell["low_side"] / n, 4) if n else None,
                "high_side_pct": round(100.0 * cell["high_side"] / n, 4) if n else None,
                "low_side": cell["low_side"],
                "high_side": cell["high_side"],
            }
        )
    return {
        "rows": table,
        "population": len(rows),
        "notes": [
            "High-side override = the underwriter APPROVED what the system would "
            "have declined; low-side = the underwriter DECLINED what the system "
            "would have approved. Both require the SYSTEM and UNDERWRITER "
            "decisions to be recorded separately — that is what the dual-decision "
            "fields on the score row are for.",
            "A 'refer' / manual-review recommendation is not an override: it is "
            "the system asking for a human, not disagreeing with one.",
            "Applications decided before the score model existed carry a "
            "backfilled row with one half of the pair unknown; those rows are "
            "excluded from this table (is_backfilled=false filter).",
        ],
    }


# ---------------------------------------------------------------------------
# Loans grouped by risk band (Portfolio report #4 / Risks segmentation)
# ---------------------------------------------------------------------------


def loans_by_risk_band(db: Session, f: Any, *, as_of: Optional[date] = None) -> dict[str, Any]:
    """Funded loans grouped by the risk segment of their originating application."""
    as_of = as_of or datetime.now(timezone.utc).date()
    outcomes = _loan_outcomes(db, f, as_of)
    principal = dict(
        _scope(
            db.query(PlatformLoan.application_id, PlatformLoan.principal_cents).join(
                PlatformCreditApplication,
                PlatformLoan.application_id == PlatformCreditApplication.id,
            ),
            f,
        ).all()
    )
    per_segment: dict[str, dict[str, int]] = {
        key: {"count": 0, "principal_cents": 0, "outstanding_cents": 0}
        for key, _l, _r in SEGMENT_ROWS
    }
    unscored = {"count": 0, "principal_cents": 0, "outstanding_cents": 0}
    scored_ids: set[str] = set()
    for row in _scored_rows(db, f, include_backfilled=True):
        app_id = str(row.application_id)
        outcome = outcomes.get(app_id)
        if outcome is None:
            continue
        scored_ids.add(app_id)
        cell = per_segment.setdefault(
            row.risk_segment, {"count": 0, "principal_cents": 0, "outstanding_cents": 0}
        )
        cell["count"] += 1
        cell["principal_cents"] += int(principal.get(row.application_id) or 0)
        cell["outstanding_cents"] += outcome["balance_cents"]
    for app_id, outcome in outcomes.items():
        if app_id in scored_ids:
            continue
        unscored["count"] += 1
        unscored["outstanding_cents"] += outcome["balance_cents"]

    return {
        "as_of": as_of.isoformat(),
        "bands": [
            {
                "index": idx,
                "segment": key,
                "risk_level": label,
                "score": score_range,
                **per_segment[key],
            }
            for idx, (key, label, score_range) in enumerate(SEGMENT_ROWS, start=1)
        ],
        "unscored": unscored,
        "notes": [
            "Grouped by the risk SEGMENT persisted on the application's current "
            "score row (Dave's five levels on the 0-1000 ladder).",
            "'unscored' counts funded loans whose application has no score row at "
            "all — reported honestly rather than folded into a band.",
            "Backfilled rows ARE included here (a portfolio view must total to the "
            "book); rows whose score could not be reconstructed have no segment and "
            "therefore land in 'unscored'.",
        ],
    }
