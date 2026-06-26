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


class TestAprRegulatoryWorkedExample:
    """Independently hand-computed against SOR/2001-104 s.3(1): APR = (C/(T*P))*100.

    $10,000 @ 12.00% nominal, 12 monthly payments, $300 fee.
    Amortizing at 1%/mo: total interest = $661.86 (C = $961.86 with the fee),
    average opening principal P = $5,515.445, T = 1.00 yr.
    APR = 96186 / (1.00 * 551544.5) = 0.174396 -> 1744 bps (17.44%).
    """

    def test_regulatory_apr_with_fee(self):
        apr = compute_apr_bps(1_000_000, 1200, 12, "monthly", fees_cents=30_000)
        assert apr == 1744

    def test_breakdown_reconciles(self):
        q = quote_loan(1_000_000, 1200, 12, "monthly", fees_cents=30_000)
        # Principal + Interest + Fees == Total of Payments (Dave's display breakdown).
        assert q.amount_cents + q.interest_cents + q.fees_cents == q.total_of_payments_cents
        assert q.interest_cents == 66_186


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
