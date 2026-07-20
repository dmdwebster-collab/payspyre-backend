"""Golden-case unit tests for the actuals-based daily-simple-interest engine.

Pure functions only — no DB, no fakes needed. Run just this file:

    source .venv/bin/activate && \
        python -m pytest tests/test_interest_engine.py -p no:warnings -q

Convention under test (documented in interest_engine, flagged for Dave):
ACT/365-Fixed, accrual from disbursement, span interest = round-half-even of
outstanding × rate × days / (10_000 × 365), interest evaluated at each event's
EFFECTIVE date, excess interest payments redirect to principal.
"""
from datetime import date

import pytest

from app.services.interest_engine import (
    BalanceView,
    InterestEngineConfig,
    LedgerEvent,
    PaymentAllocation,
    accrue_interest_cents,
    allocate_regular_payment,
    compute_balances,
)


D = date  # brevity

# A loan engineered for clean per-diem numbers:
# 100,000 cents at 36.50% -> per-diem = 100_000 * 0.365 / 365 = 100 cents/day.
PRINCIPAL = 100_000
RATE_BPS = 3650
DISBURSED = D(2026, 1, 1)


def _pmt(eff, principal=0, interest=0, fees=0, add_on=0):
    return LedgerEvent(
        effective_date=eff,
        principal_paid_cents=principal,
        interest_paid_cents=interest,
        fees_paid_cents=fees,
        add_on_paid_cents=add_on,
    )


# ---------------------------------------------------------------------------
# accrue_interest_cents — per-diem + rounding
# ---------------------------------------------------------------------------

def test_per_diem_accrual_exact():
    # 100/day for 30 days.
    assert accrue_interest_cents(PRINCIPAL, RATE_BPS, 30) == 3_000


def test_accrual_zero_for_zero_days_zero_rate_zero_principal():
    assert accrue_interest_cents(PRINCIPAL, RATE_BPS, 0) == 0
    assert accrue_interest_cents(PRINCIPAL, 0, 30) == 0
    assert accrue_interest_cents(0, RATE_BPS, 30) == 0
    assert accrue_interest_cents(PRINCIPAL, RATE_BPS, -5) == 0


def test_accrual_rounds_half_to_even():
    # denominator = 10_000 * 365 = 3_650_000.
    # numerator = 1_825_000 -> exactly 0.5 -> rounds to 0 (even).
    assert accrue_interest_cents(1_825_000, 1, 1) == 0
    # numerator = 5_475_000 -> exactly 1.5 -> rounds to 2 (even).
    assert accrue_interest_cents(5_475_000, 1, 1) == 2
    # Just over the tie rounds up: 1_825_001 / 3_650_000 -> 1.
    assert accrue_interest_cents(1_825_001, 1, 1) == 1


def test_accrual_rounds_once_per_span_not_per_day():
    # 10_000 cents at 12.99%: exact per-diem = 3.5589... cents.
    # 10 days in ONE span: round(35.589) = 36 — NOT round(3.56)*10 = 40.
    assert accrue_interest_cents(10_000, 1299, 10) == 36


def test_accrual_day_count_denominator_configurable():
    cfg = InterestEngineConfig(day_count_denominator=360)
    # 100_000 * 0.36 * 10 / 360 = 1_000.
    assert accrue_interest_cents(100_000, 3600, 10, cfg) == 1_000


# ---------------------------------------------------------------------------
# compute_balances — golden scenarios
# ---------------------------------------------------------------------------

def test_no_events_accrues_from_disbursement_to_as_of():
    view = compute_balances(PRINCIPAL, RATE_BPS, DISBURSED, [], D(2026, 1, 31))
    assert view.outstanding_principal_cents == PRINCIPAL
    assert view.interest_due_cents == 3_000  # 30 days x 100
    assert view.payoff_cents == 103_000


def test_no_disbursement_never_accrues():
    view = compute_balances(PRINCIPAL, RATE_BPS, None, [], D(2027, 1, 1))
    assert view.interest_due_cents == 0
    assert view.payoff_cents == PRINCIPAL


def test_payoff_on_disbursement_day_is_principal_only():
    # Day-boundary: the disbursement day itself has accrued nothing yet at
    # same-day payoff (span is [start, as_of) on actual days).
    view = compute_balances(PRINCIPAL, RATE_BPS, DISBURSED, [], DISBURSED)
    assert view.interest_due_cents == 0
    assert view.payoff_cents == PRINCIPAL


def test_one_day_after_disbursement_accrues_one_per_diem():
    view = compute_balances(PRINCIPAL, RATE_BPS, DISBURSED, [], D(2026, 1, 2))
    assert view.interest_due_cents == 100


def test_on_time_payment_splits_interest_then_principal():
    # Payment on day 30: 3_000 accrued. A 5_000 payment allocated 3_000
    # interest / 2_000 principal leaves outstanding 98_000; ten more days at
    # the NEW per-diem (98/day) -> 980 due.
    events = [_pmt(D(2026, 1, 31), principal=2_000, interest=3_000)]
    view = compute_balances(PRINCIPAL, RATE_BPS, DISBURSED, events, D(2026, 2, 10))
    assert view.outstanding_principal_cents == 98_000
    assert view.interest_due_cents == 980
    assert view.payoff_cents == 98_980


def test_late_payment_accrues_more_per_diem_days():
    # Same cash, arriving on day 40 instead of day 30: 4_000 accrued by then.
    on_time = compute_balances(
        PRINCIPAL, RATE_BPS, DISBURSED,
        [_pmt(D(2026, 1, 31), principal=2_000, interest=3_000)],
        D(2026, 2, 10),
    )
    late = compute_balances(
        PRINCIPAL, RATE_BPS, DISBURSED,
        [_pmt(D(2026, 2, 10), principal=1_000, interest=4_000)],  # same 5_000 cash
        D(2026, 2, 10),
    )
    # The late payer repaid LESS principal for the same cash.
    assert late.outstanding_principal_cents == 99_000
    assert on_time.outstanding_principal_cents == 98_000
    assert late.payoff_cents > on_time.payoff_cents


def test_early_payment_accrues_fewer_per_diem_days():
    # Cash on day 10: only 1_000 accrued -> 4_000 of the 5_000 hits principal.
    view = compute_balances(
        PRINCIPAL, RATE_BPS, DISBURSED,
        [_pmt(D(2026, 1, 11), principal=4_000, interest=1_000)],
        D(2026, 1, 11),
    )
    assert view.outstanding_principal_cents == 96_000
    assert view.interest_due_cents == 0
    assert view.payoff_cents == 96_000  # payoff on payment day: zero future interest


def test_multi_payment_walk_accrues_between_actual_dates():
    # Three payments; each span accrues on the then-outstanding principal.
    events = [
        _pmt(D(2026, 1, 31), principal=2_000, interest=3_000),   # 30d x 100
        _pmt(D(2026, 3, 2), principal=2_060, interest=2_940),    # 30d x 98
        _pmt(D(2026, 4, 1), principal=3_121, interest=2_879),    # 30d x 95.94->2878.5->2878? see below
    ]
    # Recompute exactly: after pmt2 outstanding = 95_940; 30 days at
    # 95_940*3650*30/3_650_000 = 95_940*30/1000 = 2_878.2 -> 2_878.
    # The event above deliberately overpays interest by 1 (2_879): the excess
    # cent must redirect to principal, never vanish (money conservation).
    view = compute_balances(PRINCIPAL, RATE_BPS, DISBURSED, events, D(2026, 4, 1))
    assert view.interest_due_cents == 0
    # outstanding = 95_940 - 3_121 - 1 (excess interest cent) = 92_818.
    assert view.outstanding_principal_cents == 92_818
    assert view.payoff_cents == 92_818


def test_same_day_payments_apply_in_sequence_without_double_accrual():
    events = [
        _pmt(D(2026, 1, 31), principal=1_000, interest=3_000),
        _pmt(D(2026, 1, 31), principal=2_000, interest=0),
    ]
    view = compute_balances(PRINCIPAL, RATE_BPS, DISBURSED, events, D(2026, 1, 31))
    assert view.interest_due_cents == 0
    assert view.outstanding_principal_cents == 97_000


def test_events_after_as_of_are_ignored():
    events = [_pmt(D(2026, 6, 1), principal=50_000)]
    view = compute_balances(PRINCIPAL, RATE_BPS, DISBURSED, events, D(2026, 1, 31))
    assert view.outstanding_principal_cents == PRINCIPAL
    assert view.interest_due_cents == 3_000


def test_excess_interest_payment_redirects_to_principal():
    # Backfilled schedule allocations can carry more "interest" than actually
    # accrued: 3_000 accrued by day 30, event claims 5_000 interest.
    events = [_pmt(D(2026, 1, 31), principal=0, interest=5_000)]
    view = compute_balances(PRINCIPAL, RATE_BPS, DISBURSED, events, D(2026, 1, 31))
    assert view.interest_due_cents == 0
    # 2_000 excess reduced principal instead of vanishing.
    assert view.outstanding_principal_cents == 98_000


def test_fee_and_add_on_buckets_and_header_invariant():
    events = [
        LedgerEvent(effective_date=D(2026, 1, 10), fees_charged_cents=100),
        LedgerEvent(effective_date=D(2026, 1, 15), add_on_charged_cents=4_500),  # NSF
        _pmt(D(2026, 1, 31), principal=1_900, interest=3_000, fees=100),
    ]
    view = compute_balances(PRINCIPAL, RATE_BPS, DISBURSED, events, D(2026, 1, 31))
    assert view.fees_due_cents == 0
    assert view.add_on_balance_cents == 4_500  # add-on untouched by regular cash
    assert view.outstanding_principal_cents == 98_100
    # Dave's header math: principal + interest due + fees due + add-on == payoff.
    assert view.payoff_cents == (
        view.outstanding_principal_cents
        + view.interest_due_cents
        + view.fees_due_cents
        + view.add_on_balance_cents
    )
    assert view.payoff_cents == 98_100 + 0 + 0 + 4_500


def test_add_on_balance_never_accrues_interest():
    # Only the principal accrues: a giant add-on balance changes nothing.
    events = [LedgerEvent(effective_date=DISBURSED, add_on_charged_cents=1_000_000)]
    view = compute_balances(PRINCIPAL, RATE_BPS, DISBURSED, events, D(2026, 1, 31))
    assert view.interest_due_cents == 3_000  # same as without the add-on


def test_overpayment_clamps_outstanding_at_zero():
    events = [_pmt(D(2026, 1, 31), principal=200_000, interest=3_000)]
    view = compute_balances(PRINCIPAL, RATE_BPS, DISBURSED, events, D(2026, 3, 1))
    assert view.outstanding_principal_cents == 0
    assert view.interest_due_cents == 0  # nothing outstanding -> nothing accrues


def test_reversal_style_charges_restore_balances():
    # A payment applied on day 30, compensated (charged back) on day 40:
    # principal returns and accrual resumes on the restored balance.
    events = [
        _pmt(D(2026, 1, 31), principal=2_000, interest=3_000),
        LedgerEvent(
            effective_date=D(2026, 2, 10),
            principal_charged_cents=2_000,
            interest_charged_cents=3_000,
        ),
    ]
    view = compute_balances(PRINCIPAL, RATE_BPS, DISBURSED, events, D(2026, 2, 10))
    assert view.outstanding_principal_cents == PRINCIPAL
    # 30 days on 100_000 (3_000) - paid 3_000 + 10 days on 98_000 (980)
    # + 3_000 charged back = 3_980 + 3_000... walk: due at day40 = 980, then
    # +3_000 charge = 3_980.
    assert view.interest_due_cents == 3_980


# ---------------------------------------------------------------------------
# allocate_regular_payment — Turnkey "Regular" waterfall
# ---------------------------------------------------------------------------

def _balances(principal=98_000, interest=3_000, fees=100, add_on=4_500):
    return BalanceView(
        as_of=D(2026, 1, 31),
        outstanding_principal_cents=principal,
        interest_due_cents=interest,
        fees_due_cents=fees,
        add_on_balance_cents=add_on,
    )


def test_regular_allocation_interest_then_principal_then_fees():
    alloc = allocate_regular_payment(5_000, _balances())
    assert alloc == PaymentAllocation(
        interest_cents=3_000, principal_cents=2_000, fees_cents=0,
        add_on_cents=0, overpayment_cents=0,
    )
    assert alloc.total_cents == 5_000


def test_regular_allocation_reaches_fees_after_principal():
    alloc = allocate_regular_payment(101_050, _balances())
    assert alloc.interest_cents == 3_000
    assert alloc.principal_cents == 98_000
    assert alloc.fees_cents == 50  # partial fee coverage
    assert alloc.add_on_cents == 0  # regular mode NEVER touches add-on
    assert alloc.overpayment_cents == 0


def test_regular_allocation_small_payment_is_all_interest():
    alloc = allocate_regular_payment(1_200, _balances())
    assert alloc.interest_cents == 1_200
    assert alloc.principal_cents == 0


def test_regular_allocation_overpayment_recorded_and_row_ties_out():
    # Everything owed outside add-on = 3_000 + 98_000 + 100 = 101_100.
    alloc = allocate_regular_payment(102_000, _balances())
    assert alloc.overpayment_cents == 900
    assert alloc.fees_cents == 100
    # The row still sums to the cash received (overpayment kept in principal).
    assert alloc.total_cents == 102_000
    assert alloc.principal_cents == 98_000 + 900


def test_regular_allocation_rejects_non_positive():
    with pytest.raises(ValueError):
        allocate_regular_payment(0, _balances())
    with pytest.raises(ValueError):
        allocate_regular_payment(-5, _balances())
