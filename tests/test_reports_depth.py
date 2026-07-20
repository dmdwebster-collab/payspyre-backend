"""DB-free unit tests for the WS-H reports-depth math + scheduler orchestration.

Everything here runs without a DB: the pure helpers in
``app.services.metrics.reports_depth`` and the report-builder column projection
in ``app.services.report_exports``, plus the scheduler orchestration exercised
against fakes. Kept out of the shared test-DB path deliberately.
"""
from datetime import date

import pytest

from app.services.metrics import reports_depth as RD


# ---------------------------------------------------------------------------
# Profit split
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fs,expected",
    [
        ("payspyre_capital", 0),
        ("partner_lender", 0),
        ("hybrid", 0),
        ("clinic_self", 10_000),
        (None, 0),
        ("nonsense", 0),
    ],
)
def test_vendor_share_defaults(fs, expected):
    assert RD.vendor_share_bps(fs) == expected


def test_vendor_share_override_wins():
    ov = {"clinic_self": 8500, "hybrid": 5000}
    assert RD.vendor_share_bps("clinic_self", ov) == 8500
    assert RD.vendor_share_bps("hybrid", ov) == 5000
    assert RD.vendor_share_bps("payspyre_capital", ov) == 0  # unchanged default


def test_parse_share_overrides_tolerant():
    assert RD.parse_share_overrides(None) == {}
    assert RD.parse_share_overrides("") == {}
    assert RD.parse_share_overrides("not json") == {}
    assert RD.parse_share_overrides("[1,2,3]") == {}  # not a dict
    # unknown key dropped; bad value dropped; good clamped
    parsed = RD.parse_share_overrides(
        '{"clinic_self": 12000, "hybrid": "x", "bogus": 5000, "partner_lender": -3}'
    )
    assert parsed == {"clinic_self": 10_000, "partner_lender": 0}


def test_split_revenue_sums_exactly_and_floors_to_vendor():
    # 100.00 at 85% → vendor 85.00 exactly
    assert RD.split_revenue(10_000, 8500) == (8500, 1500)
    # rounding remainder stays with PaySpyre
    v, p = RD.split_revenue(101, 5000)  # 50.5 → floor 50
    assert (v, p) == (50, 51)
    assert v + p == 101
    # all-PaySpyre and all-vendor edges
    assert RD.split_revenue(999, 0) == (0, 999)
    assert RD.split_revenue(999, 10_000) == (999, 0)
    # negative gross (net-reversal period) still sums
    v2, p2 = RD.split_revenue(-100, 5000)
    assert v2 + p2 == -100


def test_split_revenue_clamps_bps():
    assert RD.split_revenue(1000, 99_999) == (1000, 0)
    assert RD.split_revenue(1000, -5) == (0, 1000)


# ---------------------------------------------------------------------------
# Bucket series + roll series
# ---------------------------------------------------------------------------


def test_bucket_series_zero_fills_all_buckets_ordered():
    rows = [
        (date(2026, 6, 1), "current", 5, 500_000, 0),
        (date(2026, 6, 1), "pot_30", 2, 200_000, 50_000),
        (date(2026, 5, 1), "current", 4, 400_000, 0),
    ]
    series = RD.bucket_series(rows)
    assert [s["month"] for s in series] == ["2026-05", "2026-06"]
    june = series[1]["buckets"]
    # every bucket present
    assert set(june) == set(RD.ALL_BUCKETS)
    assert june["pot_30"] == {"count": 2, "outstanding_cents": 200_000, "past_due_cents": 50_000}
    assert june["written_off"] == {"count": 0, "outstanding_cents": 0, "past_due_cents": 0}


def test_roll_series_only_adjacent_months():
    per_month = {
        date(2026, 5, 1): {"a": "current_month_late", "b": "current_month_late"},
        date(2026, 6, 1): {"a": "pot_30", "b": "current"},
        # gap: skip July, so Aug is NOT adjacent to June
        date(2026, 8, 1): {"a": "pot_60"},
    }
    rolls = RD.roll_series(per_month)
    # only the May→June transition is adjacent
    assert len(rolls) == 1
    r = rolls[0]
    assert r["from_month"] == "2026-05"
    assert r["to_month"] == "2026-06"
    cml_to_30 = next(
        t for t in r["transitions"] if t["from_bucket"] == "current_month_late"
    )
    # 2 loans CML in May, 1 rolled to pot_30 in June → 50%
    assert cml_to_30["base_count"] == 2
    assert cml_to_30["rolled_count"] == 1
    assert cml_to_30["roll_rate_bps"] == 5000


# ---------------------------------------------------------------------------
# Geography
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "postal,expected",
    [
        ("V1Y 3J2", "V1Y"),
        ("v1y3j2", "V1Y"),
        ("M5V", "M5V"),
        ("12345", None),   # US-style
        ("AB", None),      # too short
        (None, None),
        ("", None),
    ],
)
def test_fsa(postal, expected):
    assert RD.fsa(postal) == expected


def test_norm_place():
    assert RD.norm_place("bc") == "BC"
    assert RD.norm_place("  kelowna ") == "Kelowna"
    assert RD.norm_place(None) is None
    assert RD.norm_place("   ") is None


# ---------------------------------------------------------------------------
# Cadence math
# ---------------------------------------------------------------------------


def test_previous_period_daily():
    key, frm, to = RD.previous_period("daily", date(2026, 7, 20))
    assert key == "2026-07-19"
    assert frm == to == date(2026, 7, 19)


def test_previous_period_weekly():
    # 2026-07-20 is a Monday → previous ISO week is Mon 2026-07-13..Sun 07-19
    key, frm, to = RD.previous_period("weekly", date(2026, 7, 20))
    assert frm == date(2026, 7, 13)
    assert to == date(2026, 7, 19)
    assert key.startswith("2026-W")


def test_previous_period_monthly():
    key, frm, to = RD.previous_period("monthly", date(2026, 7, 20))
    assert key == "2026-06"
    assert frm == date(2026, 6, 1)
    assert to == date(2026, 6, 30)


def test_previous_period_monthly_year_boundary():
    key, frm, to = RD.previous_period("monthly", date(2026, 1, 15))
    assert key == "2025-12"
    assert frm == date(2025, 12, 1)
    assert to == date(2025, 12, 31)


def test_is_due():
    today = date(2026, 7, 20)
    key, _f, _t = RD.previous_period("monthly", today)
    assert RD.is_due("monthly", today, None) is True     # never run
    assert RD.is_due("monthly", today, "2025-01") is True  # stale
    assert RD.is_due("monthly", today, key) is False       # already ran


def test_previous_period_unknown_cadence():
    with pytest.raises(ValueError):
        RD.previous_period("yearly", date(2026, 7, 20))


# ---------------------------------------------------------------------------
# Override side
# ---------------------------------------------------------------------------


def test_override_side():
    assert RD.override_side("approved") == "low_side"
    assert RD.override_side("declined") == "high_side"
    assert RD.override_side(None) == "other"
    assert RD.override_side("cancelled") == "other"


# ---------------------------------------------------------------------------
# Report-builder column projection
# ---------------------------------------------------------------------------


def test_column_indices_and_projection():
    headers = ["Vendor", "Provider", "Loan ID", "Amount"]
    idx = RD.column_indices(headers, ["Loan ID", "Vendor"])
    assert idx == [2, 0]
    assert RD.project_row(["v", "p", "l", "a"], idx) == ["l", "v"]


def test_column_indices_errors():
    headers = ["A", "B", "C"]
    with pytest.raises(ValueError):
        RD.column_indices(headers, [])            # empty
    with pytest.raises(ValueError):
        RD.column_indices(headers, ["A", "A"])    # duplicate
    with pytest.raises(ValueError):
        RD.column_indices(headers, ["A", "Z"])    # unknown


# ---------------------------------------------------------------------------
# report_exports builder catalog (pure — no DB touched)
# ---------------------------------------------------------------------------


def test_dataset_catalog_matches_report_keys():
    from app.services import report_exports as RE

    catalog = RE.dataset_catalog()
    assert {c["key"] for c in catalog} == set(RE.REPORT_KEYS)
    for entry in catalog:
        assert entry["columns"]  # non-empty
        assert RE.dataset_columns(entry["key"]) == entry["columns"]


def test_dataset_columns_unknown():
    from app.services import report_exports as RE

    with pytest.raises(KeyError):
        RE.dataset_columns("nope")


def test_vendor_scope_dedupes_and_combines():
    from uuid import uuid4

    from app.services import report_exports as RE

    a, b = uuid4(), uuid4()
    assert RE._vendor_scope(None, None) is None
    assert RE._vendor_scope(a, None) == (a,)
    assert set(RE._vendor_scope(a, [a, b])) == {a, b}
    assert len(RE._vendor_scope(a, [a, b])) == 2  # a not duplicated
