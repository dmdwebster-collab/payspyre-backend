"""Self-contained mutation target for the loan money-math (mutmut, L1 hardening).

Deliberately OUTSIDE tests/ so tests/conftest.py (autouse DB/app-init fixtures)
never applies — mutmut runs this in its isolated mutants/ copy with zero DB or
FastAPI setup. Pure, deterministic assertions that pin the amortization schedule
and payment application tightly enough to KILL mutations of loan_servicing.py.

Run standalone:  pytest mutation/test_loan_math.py -q
Mutation score:  mutmut run   (configured in setup.cfg to mutate only loan_servicing)
"""
from datetime import date, datetime, timezone

import pytest

from app.services.loan_servicing import generate_amortization_schedule, record_payment

FIRST_DUE = date(2026, 7, 1)


# --- amortization schedule (pure) ------------------------------------------


def test_length_and_numbering():
    rows = generate_amortization_schedule(1_000_000, 1299, 24, FIRST_DUE)
    assert len(rows) == 24
    assert [r.installment_number for r in rows] == list(range(1, 25))


def test_principal_sums_exactly():
    rows = generate_amortization_schedule(1_234_567, 1799, 36, FIRST_DUE)
    assert sum(r.principal_cents for r in rows) == 1_234_567


def test_total_ties_out_to_principal_plus_interest():
    principal = 2_000_000
    rows = generate_amortization_schedule(principal, 1799, 48, FIRST_DUE)
    interest = sum(r.interest_cents for r in rows)
    assert sum(r.total_cents for r in rows) == principal + interest
    for r in rows:
        assert r.total_cents == r.principal_cents + r.interest_cents


def test_components_never_negative():
    rows = generate_amortization_schedule(999_983, 2999, 60, FIRST_DUE)
    for r in rows:
        assert r.principal_cents >= 0
        assert r.interest_cents >= 0
        assert r.total_cents >= 0


def test_equal_payments_except_final_remainder():
    rows = generate_amortization_schedule(1_000_000, 1299, 24, FIRST_DUE)
    firsts = {r.total_cents for r in rows[:-1]}
    assert len(firsts) == 1  # every non-final payment is identical


def test_interest_free_is_even_split_zero_interest():
    rows = generate_amortization_schedule(1_200_000, 0, 12, FIRST_DUE)
    assert all(r.interest_cents == 0 for r in rows)
    assert sum(r.principal_cents for r in rows) == 1_200_000
    assert all(r.principal_cents == 100_000 for r in rows)  # 1.2M / 12


def test_first_interest_is_full_month_on_opening_balance():
    principal, rate_bps = 1_000_000, 1200  # 12% annual -> 1%/mo
    rows = generate_amortization_schedule(principal, rate_bps, 12, FIRST_DUE)
    # First month's interest = balance * monthly rate = 1_000_000 * 0.01 = 10_000.
    assert rows[0].interest_cents == 10_000


def test_balance_amortizes_to_zero():
    rows = generate_amortization_schedule(750_000, 999, 18, FIRST_DUE)
    bal = 750_000
    for r in rows:
        bal -= r.principal_cents
    assert bal == 0


def test_due_dates_step_monthly():
    rows = generate_amortization_schedule(500_000, 1299, 3, FIRST_DUE)
    assert rows[0].due_date == date(2026, 7, 1)
    assert rows[1].due_date == date(2026, 8, 1)
    assert rows[2].due_date == date(2026, 9, 1)


def test_due_dates_cross_the_year_boundary():
    # Jul 2026 + 6 months -> Jan 2027 (exercises add_months' year rollover, which a
    # same-year-only check can't distinguish from a wrong sign).
    rows = generate_amortization_schedule(900_000, 1299, 9, FIRST_DUE)
    assert rows[5].due_date == date(2026, 12, 1)
    assert rows[6].due_date == date(2027, 1, 1)
    assert rows[8].due_date == date(2027, 3, 1)


def test_month_end_clamps_when_target_month_is_shorter():
    # Jan 31 + 1 month must clamp to Feb 28 (2027 is not a leap year), not overflow.
    rows = generate_amortization_schedule(300_000, 1299, 2, date(2027, 1, 31))
    assert rows[0].due_date == date(2027, 1, 31)
    assert rows[1].due_date == date(2027, 2, 28)


# --- input validation (exact message — kills ValueError(None) mutants) ------


@pytest.mark.parametrize("bad_principal", [0, -1, -999])
def test_zero_or_negative_principal_raises(bad_principal):
    with pytest.raises(ValueError, match="principal_cents must be positive"):
        generate_amortization_schedule(bad_principal, 1299, 12, FIRST_DUE)


@pytest.mark.parametrize("bad_term", [0, -1])
def test_zero_or_negative_term_raises(bad_term):
    with pytest.raises(ValueError, match="term_months must be positive"):
        generate_amortization_schedule(1_000_000, 1299, bad_term, FIRST_DUE)


def test_negative_rate_raises_with_message():
    with pytest.raises(ValueError, match="annual_rate_bps must be non-negative"):
        generate_amortization_schedule(1_000_000, -1, 12, FIRST_DUE)


def test_zero_rate_is_allowed():
    # Boundary: 0 bps is valid (interest-free), only NEGATIVE is rejected.
    rows = generate_amortization_schedule(1_000_000, 0, 12, FIRST_DUE)
    assert len(rows) == 12


def test_minimal_loan_one_cent_one_month_is_valid():
    # Boundary just above the validation floor: principal=1 and term=1 must be
    # accepted (the limit is <= 0, not <= 1).
    rows = generate_amortization_schedule(1, 1299, 1, FIRST_DUE)
    assert len(rows) == 1
    assert rows[0].principal_cents == 1
    assert rows[0].installment_number == 1


# --- payment application (in-memory fakes; no DB) --------------------------


class _FakeSession:
    def add(self, obj):
        pass

    def commit(self):
        pass

    def refresh(self, obj, **kwargs):
        pass


class _Item:
    def __init__(self, n, principal, interest):
        self.installment_number = n
        self.principal_cents = principal
        self.interest_cents = interest
        self.total_cents = principal + interest
        self.status = "scheduled"
        self.paid_cents = 0


class _Loan:
    def __init__(self, schedule, principal_balance_cents):
        self.id = "loan-mut"
        self.schedule = schedule
        self.principal_balance_cents = principal_balance_cents
        self.status = "active"


def _loan():
    schedule = [_Item(n, 250, 10) for n in range(1, 5)]  # 4 × (250 principal + 10 int)
    return _Loan(schedule, 1000)


def _now():
    return datetime(2026, 7, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad_amount", [0, -1, -500])
def test_payment_amount_must_be_positive(bad_amount):
    with pytest.raises(ValueError, match="amount_cents must be positive"):
        record_payment(_FakeSession(), _loan(), bad_amount, _now(), "test")


def test_exact_installment_payment_marks_it_paid_and_reduces_balance():
    loan = _loan()
    record_payment(_FakeSession(), loan, 260, _now(), "test")  # one full installment
    assert loan.schedule[0].status == "paid"
    assert loan.schedule[0].paid_cents == 260
    assert loan.principal_balance_cents == 750  # 1000 - 250 principal
    assert loan.schedule[1].status == "scheduled"  # next installment untouched
    assert loan.status == "active"  # not paid off yet


def test_partial_payment_is_exact_and_reduces_principal_by_principal_portion():
    loan = _loan()
    record_payment(_FakeSession(), loan, 100, _now(), "test")  # partial on installment 1
    item = loan.schedule[0]
    assert item.status == "partial"
    assert item.paid_cents == 100
    # 100 < 250 principal, so the whole 100 is principal -> balance 1000 - 100 = 900.
    assert loan.principal_balance_cents == 900
    assert loan.status == "active"


def test_payment_spills_across_installments_in_due_order():
    loan = _loan()
    record_payment(_FakeSession(), loan, 400, _now(), "test")  # 260 fills #1, 140 -> #2
    assert loan.schedule[0].status == "paid"
    assert loan.schedule[0].paid_cents == 260
    assert loan.schedule[1].status == "partial"
    assert loan.schedule[1].paid_cents == 140
    assert loan.schedule[2].status == "scheduled"
    # principal reduced: 250 (all of #1) + 140 (140<250 principal of #2) = 390.
    assert loan.principal_balance_cents == 610


def test_paying_all_installments_pays_off_loan_to_zero():
    loan = _loan()
    for _ in range(4):
        record_payment(_FakeSession(), loan, 260, _now(), "test")
    assert loan.principal_balance_cents == 0
    assert all(s.status == "paid" for s in loan.schedule)
    assert loan.status == "paid_off"


def test_overpayment_beyond_total_leaves_loan_paid_off_not_negative():
    loan = _loan()
    record_payment(_FakeSession(), loan, 5000, _now(), "test")  # > 1040 total due
    assert loan.status == "paid_off"
    assert loan.principal_balance_cents == 0
    assert all(s.status == "paid" for s in loan.schedule)
    for s in loan.schedule:
        assert s.paid_cents <= s.total_cents  # never overfills an installment
