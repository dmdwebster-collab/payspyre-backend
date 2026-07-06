"""DB-free unit tests for the lender-reporting aggregation math.

These exercise ``app.services.metrics.analytics_reports`` in isolation — no DB,
no SQLAlchemy — so they run anywhere and keep the ``test`` gate green regardless
of the shared test-DB. The report *endpoints* are integration-tested against the
live DB in test_admin_analytics.py; here we pin the pure arithmetic that the
endpoints delegate to.
"""
import pytest

from app.services.metrics import analytics_reports as R


# ---- intervals ------------------------------------------------------------

@pytest.mark.parametrize("interval,field", [
    ("daily", "day"), ("weekly", "week"), ("monthly", "month"),
    ("quarterly", "quarter"), ("annually", "year"),
    ("DAILY", "day"), ("bogus", "month"), ("", "month"), (None, "month"),
])
def test_interval_trunc_field(interval, field):
    assert R.interval_trunc_field(interval) == field


@pytest.mark.parametrize("interval,expected", [
    ("weekly", "weekly"), ("QUARTERLY", "quarterly"),
    ("bogus", "monthly"), ("", "monthly"), (None, "monthly"),
])
def test_valid_interval(interval, expected):
    assert R.valid_interval(interval) == expected


# ---- amount bands ---------------------------------------------------------

@pytest.mark.parametrize("cents,band", [
    (0, "<1k"), (99_999, "<1k"),
    (100_000, "1-5k"), (499_999, "1-5k"),
    (500_000, "5-10k"), (999_999, "5-10k"),
    (1_000_000, "10-20k"), (1_999_999, "10-20k"),
    (2_000_000, "20-30k"), (2_999_999, "20-30k"),
    (3_000_000, ">30k"), (10_000_000, ">30k"),
])
def test_amount_band_boundaries(cents, band):
    assert R.amount_band(cents) == band


def test_empty_amount_bands_shape():
    bands = R.empty_amount_bands()
    assert list(bands.keys()) == ["<1k", "1-5k", "5-10k", "10-20k", "20-30k", ">30k"]
    assert all(v == {"count": 0, "principal_cents": 0} for v in bands.values())


# ---- risk ranks -----------------------------------------------------------

@pytest.mark.parametrize("score,rank", [
    (None, None), (0, "Poor"), (559, "Poor"),
    (560, "Weak"), (649, "Weak"),
    (650, "Average"), (719, "Average"),
    (720, "Good"), (799, "Good"),
    (800, "Excellent"), (1000, "Excellent"),
])
def test_risk_rank(score, rank):
    assert R.risk_rank(score) == rank


def test_empty_risk_ranks_shape():
    ranks = R.empty_risk_ranks()
    assert list(ranks.keys()) == ["Poor", "Weak", "Average", "Good", "Excellent"]


# ---- aging buckets --------------------------------------------------------

@pytest.mark.parametrize("dpd,bucket", [
    (0, "current"), (-5, "current"),
    (1, "current_late"), (29, "current_late"),
    (30, "30"), (59, "30"),
    (60, "60"), (89, "60"),
    (90, "90"), (119, "90"),
    (120, "120plus"), (500, "120plus"),
])
def test_collections_bucket(dpd, bucket):
    assert R.collections_bucket(dpd) == bucket


@pytest.mark.parametrize("dpd,bucket", [
    (0, "current"), (1, "1-29"), (29, "1-29"),
    (30, "30-59"), (59, "30-59"),
    (60, "60-89"), (89, "60-89"),
    (90, "90plus"), (365, "90plus"),
])
def test_days_past_due_bucket(dpd, bucket):
    assert R.days_past_due_bucket(dpd) == bucket


def test_bucket_summary_adds_pct():
    buckets = {"30": {"count": 2, "amount_cents": 250}, "60": {"count": 1, "amount_cents": 750}}
    out = R.bucket_summary(buckets, total_outstanding_cents=1000)
    assert out["30"] == {"count": 2, "amount_cents": 250, "pct": 25.0}
    assert out["60"] == {"count": 1, "amount_cents": 750, "pct": 75.0}


def test_bucket_summary_zero_outstanding_gives_null_pct():
    out = R.bucket_summary({"30": {"count": 1, "amount_cents": 100}}, 0)
    assert out["30"]["pct"] is None


# ---- ratios / percentages -------------------------------------------------

def test_ratio_and_pct():
    assert R.ratio(1, 4) == 0.25
    assert R.pct(1, 4) == 25.0
    assert R.ratio(3, 0) is None
    assert R.pct(3, 0) is None
    assert R.ratio(0, 10) == 0.0


# ---- deltas ---------------------------------------------------------------

def test_delta_increase():
    d = R.delta(120, 100)
    assert d["change"] == 20 and d["change_pct"] == 20.0 and d["direction"] == "up"


def test_delta_decrease():
    d = R.delta(80, 100)
    assert d["change"] == -20 and d["change_pct"] == -20.0 and d["direction"] == "down"


def test_delta_flat():
    d = R.delta(100, 100)
    assert d["change"] == 0 and d["direction"] == "flat"


def test_delta_no_prior_base_null_pct():
    # Growing from zero: percentage is meaningless -> None ("new"), not +100%.
    d = R.delta(50, 0)
    assert d["change"] == 50 and d["change_pct"] is None and d["direction"] == "up"


def test_delta_handles_none_inputs():
    d = R.delta(None, None)
    assert d["current"] == 0 and d["prior"] == 0 and d["direction"] == "flat"


# ---- series delta ---------------------------------------------------------

def test_series_delta_last_vs_prior():
    d = R.series_delta([("jan", 10.0), ("feb", 15.0)])
    assert d["current"] == 15.0 and d["prior"] == 10.0 and d["direction"] == "up"


def test_series_delta_single_point_uses_zero_base():
    d = R.series_delta([("jan", 10.0)])
    assert d["current"] == 10.0 and d["prior"] == 0.0 and d["change_pct"] is None


def test_series_delta_empty():
    d = R.series_delta([])
    assert d["current"] == 0 and d["prior"] == 0


# ---- profit derivation ----------------------------------------------------

def test_profit_point_math():
    p = R.ProfitPoint(disbursed_cents=100_000, interest_cents=12_000,
                      fees_cents=0, charged_off_cents=2_000)
    assert p.profit_cents == 10_000
    assert p.profit_pct() == 10.0


def test_profit_point_zero_disbursed_null_pct():
    p = R.ProfitPoint(disbursed_cents=0, interest_cents=5_000)
    assert p.profit_pct() is None
