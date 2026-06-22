"""actual/360 day-count option for the amortization engine (Turnkey-legacy parity).

The default 30/360 path must be byte-identical to before; actual/360 must reproduce
Turnkey's interest accrual (reconciled to the cent against the real book) while keeping
the same exact money tie-out invariants.
"""
from datetime import date

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.loan_servicing import generate_amortization_schedule


def _rates(): return st.integers(min_value=0, max_value=5000)


# --- default unchanged ------------------------------------------------------


def test_default_is_30_360_and_unchanged():
    a = generate_amortization_schedule(1_000_000, 1299, 24, date(2026, 7, 1))
    b = generate_amortization_schedule(1_000_000, 1299, 24, date(2026, 7, 1), day_count="30/360")
    assert a == b
    # 30/360 interest does NOT depend on accrual_start_date.
    c = generate_amortization_schedule(
        1_000_000, 1299, 24, date(2026, 7, 1), accrual_start_date=date(2026, 1, 1)
    )
    assert a == c


def test_unknown_day_count_raises():
    import pytest

    with pytest.raises(ValueError, match="day_count"):
        generate_amortization_schedule(1_000_000, 1299, 24, date(2026, 7, 1), day_count="30E/360")


# --- actual/360 reconciliation to Turnkey -----------------------------------


def test_actual_360_first_interest_matches_turnkey_to_the_cent():
    # Loan shape from the real book (no PII): $3,395 @ 5.99%, 36 mo, originated
    # 2021-05-19, first payment 2021-06-19. Turnkey posted $17.51 interest on the
    # opening balance: 3395 * 0.0599 * 31/360 = 17.51.
    rows = generate_amortization_schedule(
        339_500, 599, 36, date(2021, 6, 19),
        day_count="actual/360", accrual_start_date=date(2021, 5, 19),
    )
    assert rows[0].interest_cents == 1751   # 31-day first period
    # Second period (Jun 19 -> Jul 19 = 30 days) on the post-payment balance.
    expected2 = round(rows[0]  # balance after payment 1
                      and (339_500 - rows[0].principal_cents) * 0.0599 * 30 / 360)
    assert rows[1].interest_cents == expected2


def test_actual_360_31_vs_30_day_periods_differ():
    # A 31-day first period accrues more interest than a 30-day one, same loan.
    p31 = generate_amortization_schedule(
        339_500, 599, 36, date(2021, 6, 19), day_count="actual/360",
        accrual_start_date=date(2021, 5, 19),  # 31 days
    )[0].interest_cents
    p30 = generate_amortization_schedule(
        339_500, 599, 36, date(2021, 6, 18), day_count="actual/360",
        accrual_start_date=date(2021, 5, 19),  # 30 days
    )[0].interest_cents
    assert p31 > p30


def test_actual_360_accrues_more_total_interest_than_30_360():
    # Real calendar months average >30 days, so actual/360 accrues slightly more.
    common = dict(principal_cents=500_000, annual_rate_bps=1299, term_months=36)
    base = generate_amortization_schedule(first_due_date=date(2026, 7, 1), **common)
    a360 = generate_amortization_schedule(
        first_due_date=date(2026, 7, 1), day_count="actual/360", **common
    )
    assert sum(r.interest_cents for r in a360) >= sum(r.interest_cents for r in base)


# --- tie-out invariants hold for actual/360 too -----------------------------


@settings(max_examples=300)
@given(
    principal=st.integers(min_value=1, max_value=100_000_000),
    rate=_rates(),
    term=st.integers(min_value=1, max_value=120),
)
def test_actual_360_ties_out(principal, rate, term):
    rows = generate_amortization_schedule(
        principal, rate, term, date(2026, 7, 1), day_count="actual/360"
    )
    assert len(rows) == term
    assert sum(r.principal_cents for r in rows) == principal
    total_interest = sum(r.interest_cents for r in rows)
    assert sum(r.total_cents for r in rows) == principal + total_interest
    for r in rows:
        assert r.principal_cents >= 0
        assert r.interest_cents >= 0
        assert r.total_cents == r.principal_cents + r.interest_cents


def test_actual_360_interest_free_is_even_split():
    rows = generate_amortization_schedule(
        1_200_000, 0, 12, date(2026, 7, 1), day_count="actual/360"
    )
    assert all(r.interest_cents == 0 for r in rows)
    assert sum(r.principal_cents for r in rows) == 1_200_000
    assert all(r.principal_cents == 100_000 for r in rows)
