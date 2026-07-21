"""Pure helpers for the WS-H reports-depth build (Turnkey parity, video 05).

Everything in this module is DB-free and clock-free so it can be unit-tested
in isolation (the ``analytics_reports`` idiom): the endpoints/services do the
querying, these own the *math and vocabulary* —

  * profit-split policy (gross → vendor share / PaySpyre share) derived from
    the product's ``funding_source``;
  * month-end bucket series shaping + debt-roll series (on top of
    ``delinquency_buckets.compute_roll_rates``);
  * geographic keys (province / city / FSA);
  * scheduled-report cadence arithmetic (previous period key + date window,
    due-ness);
  * underwriter override-side vocabulary (high-side / low-side);
  * report-builder column projection (header subset → index projection).

Money is integer cents; shares are integer basis points (10_000 = 100%).
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Iterable, Mapping, Optional, Sequence

from app.services.delinquency_buckets import ALL_BUCKETS, compute_roll_rates

# ---------------------------------------------------------------------------
# Profit split — Dave's gross / vendor / PaySpyre dropdown (video 05 §4).
#
# There is NO explicit commission table in the schema yet. The commission model
# lives in the product data as ``funding_source``:
#
#   payspyre_capital  PaySpyre funds the loan            → revenue is PaySpyre's
#   partner_lender    a partner funds it                 → platform books it as
#                                                          PaySpyre-side revenue
#                                                          (partner settlement is
#                                                          out of scope here)
#   hybrid            mixed book                         → no split configured →
#                                                          PaySpyre until Dave
#                                                          sets an override
#   clinic_self       the clinic funds its own paper     → revenue is the
#                                                          vendor's; PaySpyre's
#                                                          cut (servicing fee)
#                                                          is 0 until configured
#
# DOCUMENTED ASSUMPTION (flagged for Dave in the PR): the defaults below are a
# structural derivation, not a negotiated split. Real percentages go in the
# ``PROFIT_SPLIT_VENDOR_SHARE_BPS`` settings knob (JSON map, e.g.
# ``{"clinic_self": 8500, "hybrid": 5000}``) — no code change needed.
# ---------------------------------------------------------------------------

FUNDING_SOURCES = ("payspyre_capital", "partner_lender", "hybrid", "clinic_self")

DEFAULT_VENDOR_SHARE_BPS: dict[str, int] = {
    "payspyre_capital": 0,
    "partner_lender": 0,
    "hybrid": 0,
    "clinic_self": 10_000,
}


def parse_share_overrides(raw: Optional[str]) -> dict[str, int]:
    """Parse the settings-knob JSON map of funding_source → vendor-share bps.

    Tolerant by design (a bad env var must never 500 the report): non-JSON,
    non-dict, unknown funding sources and non-numeric values are ignored;
    numeric values are clamped to 0..10_000.
    """
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in data.items():
        if key not in FUNDING_SOURCES:
            continue
        try:
            bps = int(value)
        except (TypeError, ValueError):
            continue
        out[key] = max(0, min(10_000, bps))
    return out


def vendor_share_bps(
    funding_source: Optional[str], overrides: Optional[Mapping[str, int]] = None
) -> int:
    """Vendor's revenue share (bps) for a product funding source.

    Unknown/None funding source (e.g. an application-less migrated loan) →
    0 bps (all PaySpyre) — conservative and visible, never silently vendor-side.
    """
    if funding_source in (overrides or {}):
        return int((overrides or {})[funding_source])  # type: ignore[index]
    return DEFAULT_VENDOR_SHARE_BPS.get(funding_source or "", 0)


def split_revenue(gross_cents: int, share_bps: int) -> tuple[int, int]:
    """(vendor_cents, payspyre_cents). Integer floor to the vendor; the
    remainder (including all rounding) stays with PaySpyre so the two always
    sum exactly to gross. Negative gross (a net-reversal period) splits with
    the same arithmetic."""
    share_bps = max(0, min(10_000, int(share_bps)))
    vendor = (int(gross_cents) * share_bps) // 10_000
    return vendor, int(gross_cents) - vendor


# ---------------------------------------------------------------------------
# Bucket time series + debt roll.
# ---------------------------------------------------------------------------


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _next_month(d: date) -> date:
    first = d.replace(day=1)
    if first.month == 12:
        return date(first.year + 1, 1, 1)
    return date(first.year, first.month + 1, 1)


def bucket_series(
    rows: Iterable[tuple[date, str, int, int, int]],
) -> list[dict]:
    """Shape (snapshot_month, bucket, count, outstanding_cents, past_due_cents)
    rows into an ordered, zero-filled per-month series.

    Every month present in ``rows`` gets ALL buckets (missing → zeros) so the
    chart never has holes; months are ascending.
    """
    by_month: dict[date, dict[str, dict[str, int]]] = {}
    for month, bucket, count, outstanding, past_due in rows:
        cell = by_month.setdefault(month, {})
        cell[bucket] = {
            "count": int(count or 0),
            "outstanding_cents": int(outstanding or 0),
            "past_due_cents": int(past_due or 0),
        }
    zero = {"count": 0, "outstanding_cents": 0, "past_due_cents": 0}
    out = []
    for month in sorted(by_month):
        buckets = {b: dict(by_month[month].get(b, zero)) for b in ALL_BUCKETS}
        out.append({"month": month_key(month), "buckets": buckets})
    return out


def roll_series(per_month: Mapping[date, Mapping[str, str]]) -> list[dict]:
    """Debt-roll transitions for every ADJACENT month pair in the snapshots.

    ``per_month`` maps snapshot month (first-of-month date) → {loan_id: bucket}.
    Non-adjacent months (a gap in the snapshot history) produce no roll row —
    a two-month jump is not a "roll" and would overstate the rate.
    Delegates the pair math to ``delinquency_buckets.compute_roll_rates``
    (Dave's CML→POT30 definition, generalized down the ladder).
    """
    months = sorted(per_month)
    out = []
    for prior_m, current_m in zip(months, months[1:]):
        if _next_month(prior_m) != current_m.replace(day=1):
            continue
        out.append(
            {
                "from_month": month_key(prior_m),
                "to_month": month_key(current_m),
                "transitions": compute_roll_rates(
                    dict(per_month[prior_m]), dict(per_month[current_m])
                ),
            }
        )
    return out


# Buckets that count as "delinquent" for outcome/geo reporting (everything on
# the ladder past current, plus the terminal states).
BAD_BUCKETS = tuple(b for b in ALL_BUCKETS if b != "current")


# ---------------------------------------------------------------------------
# Geography — province / city / FSA keys from application address data.
# ---------------------------------------------------------------------------

_FSA_RE = re.compile(r"^[A-Z][0-9][A-Z]")


def fsa(postal_code: Optional[str]) -> Optional[str]:
    """Forward Sortation Area (first 3 chars of a Canadian postal code), or
    None when the value doesn't look like one."""
    if not postal_code:
        return None
    cleaned = postal_code.strip().upper().replace(" ", "")
    if len(cleaned) < 3:
        return None
    head = cleaned[:3]
    return head if _FSA_RE.match(head) else None


def norm_place(value: Optional[str]) -> Optional[str]:
    """Normalize a free-text city/province for grouping (trim + title-case
    city-style strings; upper-case 2-letter province codes)."""
    if not value or not value.strip():
        return None
    v = value.strip()
    if len(v) == 2:
        return v.upper()
    return v.title()


# ---------------------------------------------------------------------------
# Scheduled-report cadences.
#
# The runner is a daily external cron (parked, PR #178 pattern). A schedule is
# due when the most recently COMPLETED period differs from the last one it ran
# for — so re-runs are idempotent and a missed day self-heals on the next run.
# ---------------------------------------------------------------------------

CADENCES = ("daily", "weekly", "monthly")


def previous_period(cadence: str, today: date) -> tuple[str, date, date]:
    """(period_key, date_from, date_to) of the most recently completed period.

    daily   → yesterday                    key ``YYYY-MM-DD``
    weekly  → previous ISO week (Mon–Sun)  key ``YYYY-Www``
    monthly → previous calendar month      key ``YYYY-MM``
    """
    if cadence == "daily":
        d = today - timedelta(days=1)
        return d.isoformat(), d, d
    if cadence == "weekly":
        this_monday = today - timedelta(days=today.weekday())
        start = this_monday - timedelta(days=7)
        end = start + timedelta(days=6)
        iso = start.isocalendar()
        return f"{iso[0]:04d}-W{iso[1]:02d}", start, end
    if cadence == "monthly":
        first_this = today.replace(day=1)
        end = first_this - timedelta(days=1)
        start = end.replace(day=1)
        return month_key(start), start, end
    raise ValueError(f"Unknown cadence {cadence!r}")


def is_due(cadence: str, today: date, last_period_key: Optional[str]) -> bool:
    """True when the schedule has not yet run for the most recently completed
    period. A schedule that has never run (None) is always due."""
    key, _f, _t = previous_period(cadence, today)
    return key != last_period_key


# ---------------------------------------------------------------------------
# Underwriter overrides vocabulary (video 05 §6 — TL's High-side / Low-side).
# ---------------------------------------------------------------------------


def override_side(outcome: Optional[str]) -> str:
    """Classify an override decision's direction.

    ASSUMPTION (documented in the PR): an override that lands ``approved``
    means the human went *below* the machine's bar → ``low_side``; one that
    lands ``declined`` means the human tightened above it → ``high_side``.
    The platform stores only the final human outcome, not the counterfactual
    machine score, so direction is derived from the outcome alone.
    """
    if outcome == "approved":
        return "low_side"
    if outcome == "declined":
        return "high_side"
    return "other"


# ---------------------------------------------------------------------------
# Report-builder column projection.
# ---------------------------------------------------------------------------


def column_indices(headers: Sequence[str], wanted: Sequence[str]) -> list[int]:
    """Indices of ``wanted`` headers inside ``headers``, in wanted-order.

    Raises ValueError on an unknown header, a duplicate selection, or an empty
    selection — the report-builder API turns that into a 422.
    """
    if not wanted:
        raise ValueError("column selection is empty")
    index = {h: i for i, h in enumerate(headers)}
    seen: set[str] = set()
    out: list[int] = []
    for h in wanted:
        if h not in index:
            raise ValueError(f"unknown column {h!r}")
        if h in seen:
            raise ValueError(f"duplicate column {h!r}")
        seen.add(h)
        out.append(index[h])
    return out


def project_row(row: Sequence, indices: Sequence[int]) -> list:
    return [row[i] for i in indices]
