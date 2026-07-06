"""Pure aggregation helpers for the lender reporting suite (admin_analytics).

These functions hold ALL the report *math* — interval bucketing keys, amount /
risk banding, prior-period deltas, ratio + percentage arithmetic — with zero DB
or SQLAlchemy dependency so they can be unit-tested in isolation and reused by
every report endpoint. The endpoints do the querying; these turn rows into the
dashboard-shaped payloads (series + totals + prior-period deltas).

Money is integer cents throughout. Rates/percentages are returned as fractions
(0..1) unless a field name says ``_pct`` (then it is a 0..100 percentage rounded
to 2 dp), matching what the dashboard charts want.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Interval handling — maps the API's interval choice to a Postgres date_trunc
# field and to the strftime-ish label the series keys use. The actual truncation
# runs in SQL (func.date_trunc); this module only owns the *vocabulary* so the
# mapping is testable and shared.
# ---------------------------------------------------------------------------

# interval -> (date_trunc field, human label format hint)
_INTERVAL_TRUNC = {
    "daily": "day",
    "weekly": "week",
    "monthly": "month",
    "quarterly": "quarter",
    "annually": "year",
}


def interval_trunc_field(interval: str) -> str:
    """Postgres ``date_trunc`` field for an API interval. Defaults to month."""
    return _INTERVAL_TRUNC.get((interval or "").lower(), "month")


def valid_interval(interval: str) -> str:
    """Normalize/validate an interval, defaulting to 'monthly'."""
    i = (interval or "").lower()
    return i if i in _INTERVAL_TRUNC else "monthly"


# ---------------------------------------------------------------------------
# Amount bands (loan principal) — Dave's spec bands, in cents.
# ---------------------------------------------------------------------------

# (label, lower_inclusive_cents, upper_exclusive_cents_or_None)
AMOUNT_BANDS: list[tuple[str, int, Optional[int]]] = [
    ("<1k", 0, 100_000),
    ("1-5k", 100_000, 500_000),
    ("5-10k", 500_000, 1_000_000),
    ("10-20k", 1_000_000, 2_000_000),
    ("20-30k", 2_000_000, 3_000_000),
    (">30k", 3_000_000, None),
]


def amount_band(cents: int) -> str:
    """Classify a principal amount (cents) into its spec band label."""
    c = int(cents or 0)
    for label, lo, hi in AMOUNT_BANDS:
        if c >= lo and (hi is None or c < hi):
            return label
    return AMOUNT_BANDS[-1][0]


def empty_amount_bands() -> dict[str, dict[str, int]]:
    """A zeroed {band: {count, principal_cents}} map in spec order."""
    return {label: {"count": 0, "principal_cents": 0} for label, _lo, _hi in AMOUNT_BANDS}


# ---------------------------------------------------------------------------
# Risk banding — the scorecard produces no persisted numeric risk score yet (see
# module note in admin_analytics), so this maps whatever numeric score is
# available (0..1000 style) into the 5 spec ranks. Kept pure + tested so it is
# ready the moment a score column lands.
# ---------------------------------------------------------------------------

RISK_RANKS = ["Poor", "Weak", "Average", "Good", "Excellent"]

# Score thresholds (upper-exclusive) on a 0..1000 scale.
_RISK_THRESHOLDS = [
    ("Poor", 560),
    ("Weak", 650),
    ("Average", 720),
    ("Good", 800),
    ("Excellent", None),
]


def risk_rank(score: Optional[float]) -> Optional[str]:
    """Map a numeric score (0..1000) to a rank. None → None (not tracked)."""
    if score is None:
        return None
    for label, upper in _RISK_THRESHOLDS:
        if upper is None or score < upper:
            return label
    return "Excellent"


def empty_risk_ranks() -> dict[str, dict[str, int]]:
    return {r: {"count": 0, "principal_cents": 0} for r in RISK_RANKS}


# ---------------------------------------------------------------------------
# Ratios, percentages and prior-period deltas.
# ---------------------------------------------------------------------------


def ratio(num: Optional[int], den: Optional[int]) -> Optional[float]:
    """num/den as a 0..1 fraction (4dp), or None when the denominator is 0."""
    num = num or 0
    den = den or 0
    return round(num / den, 4) if den else None


def pct(num: Optional[int], den: Optional[int]) -> Optional[float]:
    """num/den as a 0..100 percentage (2dp), or None when denominator is 0."""
    r = ratio(num, den)
    return round(r * 100, 2) if r is not None else None


@dataclass
class Delta:
    """A current-vs-prior comparison for one metric."""

    current: float
    prior: float
    change: float                     # current - prior (absolute)
    change_pct: Optional[float]       # (current-prior)/prior * 100, None if prior 0
    direction: str                    # "up" | "down" | "flat"

    def as_dict(self) -> dict:
        return {
            "current": self.current,
            "prior": self.prior,
            "change": self.change,
            "change_pct": self.change_pct,
            "direction": self.direction,
        }


def delta(current: Optional[float], prior: Optional[float]) -> dict:
    """Build a prior-period Delta for a single metric.

    ``change_pct`` is None when there is no prior base to grow from (prior == 0),
    which the dashboard renders as "new" rather than a misleading +100%.
    """
    cur = float(current or 0)
    pri = float(prior or 0)
    change = round(cur - pri, 4)
    if pri == 0:
        change_pct = None
    else:
        change_pct = round((cur - pri) / abs(pri) * 100, 2)
    if change > 0:
        direction = "up"
    elif change < 0:
        direction = "down"
    else:
        direction = "flat"
    return Delta(current=round(cur, 4), prior=round(pri, 4),
                 change=change, change_pct=change_pct, direction=direction).as_dict()


# ---------------------------------------------------------------------------
# Delinquency / collections aging buckets.
# ---------------------------------------------------------------------------

# Collections "potential" buckets keyed by days-past-due, spec order.
COLLECTIONS_BUCKETS = ["current_late", "30", "60", "90", "120plus"]

# Risk delinquency-performance buckets (spec: 1-29, 30-59, 60-89, >90).
DELINQUENCY_BUCKETS = ["1-29", "30-59", "60-89", "90plus"]


def days_past_due_bucket(days: int) -> str:
    """Map days-past-due into the risk delinquency-performance bucket label."""
    d = int(days or 0)
    if d <= 0:
        return "current"
    if d < 30:
        return "1-29"
    if d < 60:
        return "30-59"
    if d < 90:
        return "60-89"
    return "90plus"


def collections_bucket(days: int) -> str:
    """Map days-past-due into the collections potential-loss bucket label.

    <=0        current (not late)
    1-29       current_late (late this month, not yet 30)
    30-59      "30"
    60-89      "60"
    90-119     "90"
    120+       "120plus"
    """
    d = int(days or 0)
    if d <= 0:
        return "current"
    if d < 30:
        return "current_late"
    if d < 60:
        return "30"
    if d < 90:
        return "60"
    if d < 120:
        return "90"
    return "120plus"


def bucket_summary(buckets: dict[str, dict[str, int]], total_outstanding_cents: int) -> dict:
    """Attach a share-of-portfolio pct to each aging bucket's {count, amount}."""
    out = {}
    for name, vals in buckets.items():
        amt = int(vals.get("amount_cents", 0))
        out[name] = {
            "count": int(vals.get("count", 0)),
            "amount_cents": amt,
            "pct": pct(amt, total_outstanding_cents),
        }
    return out


# ---------------------------------------------------------------------------
# Profit — a report-level derivation. Interest + fees earned less charge-offs.
# ---------------------------------------------------------------------------


@dataclass
class ProfitPoint:
    disbursed_cents: int = 0
    repaid_cents: int = 0
    interest_cents: int = 0
    fees_cents: int = 0
    charged_off_cents: int = 0

    @property
    def profit_cents(self) -> int:
        """Earned (interest+fees) net of principal written off."""
        return self.interest_cents + self.fees_cents - self.charged_off_cents

    def profit_pct(self) -> Optional[float]:
        """Profit as a % of disbursed principal."""
        return pct(self.profit_cents, self.disbursed_cents)


def series_delta(points: list[tuple[str, float]]) -> dict:
    """Given an ordered [(label, value), ...] series, return the last-vs-prior
    delta for the trendline (the "increase/decrease vs the selected interval").
    """
    if not points:
        return delta(0, 0)
    if len(points) == 1:
        return delta(points[-1][1], 0)
    return delta(points[-1][1], points[-2][1])
