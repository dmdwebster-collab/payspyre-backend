"""Pure-function tests for the actuarial APR calculation (DB-free).

Run with:
  PYTHONPATH=$(pwd) .venv/bin/python -m pytest tests/test_loan_apr.py -p no:warnings -q
"""
from app.services.loan_quote import compute_apr_bps, quote_loan


class TestAprNoFeesEqualsContractRate:
    """fees=0 -> APR == annual_rate_bps exactly (within <=1 bps rounding)."""

    def test_monthly_no_fees(self):
        apr = compute_apr_bps(1_000_000, 1290, 12, "monthly", fees_cents=0)
        assert abs(apr - 1290) <= 1

    def test_bi_weekly_no_fees(self):
        apr = compute_apr_bps(1_000_000, 1290, 12, "bi_weekly", fees_cents=0)
        assert abs(apr - 1290) <= 1

    def test_monthly_no_fees_other_rate(self):
        apr = compute_apr_bps(2_500_000, 1999, 36, "monthly", fees_cents=0)
        assert abs(apr - 1999) <= 1

    def test_bi_weekly_no_fees_other_rate(self):
        apr = compute_apr_bps(2_500_000, 1999, 36, "bi_weekly", fees_cents=0)
        assert abs(apr - 1999) <= 1


class TestAprWithFees:
    def test_fees_push_apr_above_contract_rate(self):
        apr = compute_apr_bps(1_000_000, 1290, 12, "monthly", fees_cents=20_000)
        assert apr > 1290

    def test_apr_monotonic_in_fees(self):
        base = compute_apr_bps(1_000_000, 1290, 12, "monthly", fees_cents=0)
        small = compute_apr_bps(1_000_000, 1290, 12, "monthly", fees_cents=10_000)
        large = compute_apr_bps(1_000_000, 1290, 12, "monthly", fees_cents=50_000)
        assert base < small < large

    def test_apr_monotonic_in_fees_bi_weekly(self):
        small = compute_apr_bps(1_000_000, 1290, 24, "bi_weekly", fees_cents=10_000)
        large = compute_apr_bps(1_000_000, 1290, 24, "bi_weekly", fees_cents=60_000)
        assert small < large


class TestAprZeroRate:
    def test_zero_rate_with_fees_has_positive_apr(self):
        # a 0% loan that still charges an upfront fee has a real cost of borrowing
        apr = compute_apr_bps(1_000_000, 0, 12, "monthly", fees_cents=30_000)
        assert apr > 0

    def test_zero_rate_no_fees_is_zero_apr(self):
        apr = compute_apr_bps(1_000_000, 0, 12, "monthly", fees_cents=0)
        assert apr == 0


class TestAprSanity:
    def test_known_case_10k_1290bps_12mo(self):
        # $10,000, 12.90% nominal, 12 monthly payments, no fees -> APR ~= 1290 bps
        apr = compute_apr_bps(1_000_000, 1290, 12, "monthly", fees_cents=0)
        assert abs(apr - 1290) <= 1

    def test_quote_loan_sets_apr(self):
        q = quote_loan(1_000_000, 1290, 12, "monthly")
        assert q.apr_bps == 1290
        q_fee = quote_loan(1_000_000, 1290, 12, "monthly", fees_cents=25_000)
        assert q_fee.apr_bps > 1290
