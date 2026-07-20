"""Unit tests for the Loan Servicing (LMS) spine — P9.0.

These tests are DELIBERATELY DB-free:

  * The amortization math (``generate_amortization_schedule``) is a pure
    function — tested directly with assertions on exact cent tie-out.
  * ``record_payment`` is exercised with lightweight in-memory fakes (simple
    objects + a fake Session) so we validate the application logic without
    touching the shared remote database.

Run JUST this file (the suite shares a remote DB and must not be run wholesale):

    source .venv/bin/activate && \
        python -m pytest tests/test_loan_servicing.py -p no:warnings -q
"""
from datetime import date, datetime, timezone

import pytest

from app.models.platform.event import PlatformEvent
from app.services.loan_servicing import (
    LOAN_PAYMENT_RECORDED_EVENT,
    ScheduleRow,
    generate_amortization_schedule,
    record_payment,
    _add_months,
)


FIRST_DUE = date(2026, 7, 1)


# ---------------------------------------------------------------------------
# Pure amortization math
# ---------------------------------------------------------------------------

def test_schedule_has_correct_length_and_numbering():
    rows = generate_amortization_schedule(1_000_000, 1299, 24, FIRST_DUE)
    assert len(rows) == 24
    assert [r.installment_number for r in rows] == list(range(1, 25))


def test_principal_sums_exactly_to_input_principal():
    principal = 1_500_000
    rows = generate_amortization_schedule(principal, 1299, 36, FIRST_DUE)
    assert sum(r.principal_cents for r in rows) == principal


def test_total_ties_out_to_principal_plus_interest():
    principal = 2_345_678
    rows = generate_amortization_schedule(principal, 1799, 48, FIRST_DUE)
    total_interest = sum(r.interest_cents for r in rows)
    total_paid = sum(r.total_cents for r in rows)
    # No fractional cents escape: total paid == principal + total interest.
    assert total_paid == principal + total_interest
    # And each row's total is its own principal+interest.
    for r in rows:
        assert r.total_cents == r.principal_cents + r.interest_cents


def test_equal_payments_except_final_remainder_row():
    # All installments except the last should share the same total (equal pay).
    rows = generate_amortization_schedule(1_000_000, 1299, 24, FIRST_DUE)
    non_final_totals = {r.total_cents for r in rows[:-1]}
    # Equal-payment amortization: every non-final installment is identical.
    assert len(non_final_totals) == 1
    # The final installment absorbs the rounding remainder; it should be close
    # to the others (within a couple cents) but is allowed to differ.
    regular = next(iter(non_final_totals))
    assert abs(rows[-1].total_cents - regular) <= 5


def test_interest_free_loan_zero_interest_and_even_split():
    principal = 1_000_000
    rows = generate_amortization_schedule(principal, 0, 12, FIRST_DUE)
    assert all(r.interest_cents == 0 for r in rows)
    assert sum(r.principal_cents for r in rows) == principal
    assert sum(r.total_cents for r in rows) == principal
    # 1,000,000 / 12 doesn't divide evenly -> remainder lands on the last row.
    assert rows[0].principal_cents == 83_333
    assert rows[-1].principal_cents == 1_000_000 - 83_333 * 11


def test_interest_only_first_row_interest_is_full_month_on_balance():
    principal = 1_000_000
    rate_bps = 1200  # 12% APR -> 1% per month
    rows = generate_amortization_schedule(principal, rate_bps, 12, FIRST_DUE)
    # First installment interest = 1% of full principal = 10,000 cents.
    assert rows[0].interest_cents == 10_000


def test_balance_amortizes_to_zero():
    principal = 777_777
    rows = generate_amortization_schedule(principal, 999, 18, FIRST_DUE)
    # Replay the principal reductions: running principal must equal input.
    assert sum(r.principal_cents for r in rows) == principal
    # Final row clears the balance — its principal is whatever remained.
    assert rows[-1].principal_cents > 0


def test_due_dates_step_monthly_from_first_due():
    rows = generate_amortization_schedule(500_000, 1299, 3, FIRST_DUE)
    assert rows[0].due_date == date(2026, 7, 1)
    assert rows[1].due_date == date(2026, 8, 1)
    assert rows[2].due_date == date(2026, 9, 1)


def test_add_months_clamps_end_of_month():
    # Jan 31 + 1 month -> Feb 28 (2026 is not a leap year).
    assert _add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    # Year rollover.
    assert _add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        generate_amortization_schedule(0, 1299, 24, FIRST_DUE)
    with pytest.raises(ValueError):
        generate_amortization_schedule(1000, 1299, 0, FIRST_DUE)
    with pytest.raises(ValueError):
        generate_amortization_schedule(1000, -1, 24, FIRST_DUE)


# ---------------------------------------------------------------------------
# In-memory fakes for record_payment (no DB)
# ---------------------------------------------------------------------------

class _NoResultQuery:
    """Query stub whose filtered .first() finds nothing (no prior payment)."""

    def filter(self, *a, **k):
        return self

    def first(self):
        return None


class _FakeSession:
    """Captures add/commit/refresh without persisting anything."""

    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    def query(self, *args, **kwargs):
        return _NoResultQuery()

    def commit(self):
        pass

    def refresh(self, obj):
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
    def __init__(self, schedule, principal_balance_cents, principal_cents=300):
        self.id = "loan-1"
        self.application_id = "app-1"
        self.schedule = schedule
        self.principal_cents = principal_cents
        self.principal_balance_cents = principal_balance_cents
        self.status = "active"
        # WS-A actuals-ledger attributes: no disbursement date -> no per-diem
        # accrual, so these tests exercise the schedule/plan mechanics with the
        # ledger allocating all cash to principal (interest due is 0).
        self.annual_rate_bps = 0
        self.disbursed_at = None
        self.transactions = []


def _build_loan():
    # 3 installments of 100 principal + 10 interest = 110 each.
    items = [_Item(1, 100, 10), _Item(2, 100, 10), _Item(3, 100, 10)]
    return _Loan(items, principal_balance_cents=300)


def _now():
    return datetime(2026, 7, 1, tzinfo=timezone.utc)


def test_full_payment_marks_oldest_installment_paid():
    loan = _build_loan()
    db = _FakeSession()
    record_payment(db, loan, 110, _now(), "zumrails", external_ref="zr_txn_1")

    assert loan.schedule[0].status == "paid"
    assert loan.schedule[0].paid_cents == 110
    assert loan.schedule[1].status == "scheduled"
    # WS-A actuals ledger: with no accrued interest (never disbursed), the FULL
    # cash amount is principal — the money truth no longer follows the
    # schedule's per-installment principal/interest split (300 - 110 = 190).
    assert loan.principal_balance_cents == 190
    # The payment row was recorded with its external ref.
    payments = [o for o in db.added if getattr(o, "external_ref", "n/a") != "n/a"]
    assert payments and payments[0].external_ref == "zr_txn_1"
    # And an immutable ledger row was appended with the actuals allocation.
    assert len(loan.transactions) == 1
    txn = loan.transactions[0]
    assert txn.txn_type == "payment"
    assert txn.repayment_mode == "regular"
    assert txn.amount_cents == 110
    assert txn.interest_cents == 0 and txn.principal_cents == 110
    assert txn.reference == "none-loan-1-1"  # no vendor resolvable via fakes


def _events(db):
    return [o for o in db.added if isinstance(o, PlatformEvent)]


def test_record_payment_emits_audit_event_with_amount_and_ref():
    loan = _build_loan()
    db = _FakeSession()
    record_payment(db, loan, 110, _now(), "zumrails", external_ref="zr_txn_1")

    events = _events(db)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == LOAN_PAYMENT_RECORDED_EVENT
    assert event.actor == "system"
    # No PII: ids + amounts only, application_id sourced straight off the loan.
    assert event.application_id == "app-1"
    assert event.payload["loan_id"] == "loan-1"
    assert event.payload["application_id"] == "app-1"
    assert event.payload["after"]["amount_cents"] == 110
    assert event.payload["after"]["external_ref"] == "zr_txn_1"
    assert event.payload["after"]["method"] == "zumrails"


def test_record_payment_audit_event_omitted_on_idempotent_replay():
    # A replayed receipt returns the original payment and applies nothing — so it
    # must NOT emit a second money-IN audit row.
    existing_payment = object()

    class _DedupQuery:
        def filter(self, *a, **k):
            return self

        def first(self):
            return existing_payment

    class _DedupSession(_FakeSession):
        def query(self, *a, **k):
            return _DedupQuery()

    db = _DedupSession()
    loan = _build_loan()
    record_payment(db, loan, 110, _now(), "zumrails", external_ref="dup-1")
    assert _events(db) == []


def test_partial_payment_marks_partial():
    loan = _build_loan()
    db = _FakeSession()
    record_payment(db, loan, 50, _now(), "manual")

    assert loan.schedule[0].status == "partial"
    assert loan.schedule[0].paid_cents == 50
    # Actuals ledger: no interest due -> all 50 cash is principal.
    assert loan.principal_balance_cents == 250


def test_payment_spills_into_next_installment_oldest_first():
    loan = _build_loan()
    db = _FakeSession()
    # 165 = full first installment (110) + 55 into the second.
    record_payment(db, loan, 165, _now(), "zumrails")

    assert loan.schedule[0].status == "paid"
    assert loan.schedule[0].paid_cents == 110
    assert loan.schedule[1].status == "partial"
    assert loan.schedule[1].paid_cents == 55
    assert loan.schedule[2].status == "scheduled"


def test_paying_everything_marks_loan_paid_off():
    loan = _build_loan()
    db = _FakeSession()
    record_payment(db, loan, 330, _now(), "zumrails")  # 3 x 110

    assert all(s.status == "paid" for s in loan.schedule)
    assert loan.status == "paid_off"
    assert loan.principal_balance_cents == 0


def test_sequential_partials_accumulate():
    loan = _build_loan()
    db = _FakeSession()
    record_payment(db, loan, 40, _now(), "manual")
    record_payment(db, loan, 70, _now(), "manual")

    # 40 + 70 = 110 fully covers installment 1.
    assert loan.schedule[0].status == "paid"
    assert loan.schedule[0].paid_cents == 110
    # Actuals ledger: no interest due -> all 110 cash is principal (300 - 110).
    assert loan.principal_balance_cents == 190
    # Ledger seq increments per payment; both rows on the same loan.
    assert [t.seq for t in loan.transactions] == [1, 2]


def test_non_positive_payment_raises():
    loan = _build_loan()
    db = _FakeSession()
    with pytest.raises(ValueError):
        record_payment(db, loan, 0, _now(), "manual")


def test_record_payment_idempotent_on_duplicate_external_ref():
    # A replayed receipt (same external_ref) must return the ORIGINAL payment and
    # apply nothing — no double-count of cash to principal.
    existing_payment = object()

    class _DedupQuery:
        def filter(self, *a, **k):
            return self

        def first(self):
            return existing_payment

    class _DedupSession(_FakeSession):
        def query(self, *a, **k):
            return _DedupQuery()

    db = _DedupSession()
    loan = _build_loan()
    before_balance = loan.principal_balance_cents
    out = record_payment(db, loan, 110, _now(), "zumrails", external_ref="dup-1")
    assert out is existing_payment        # returned the original receipt
    assert db.added == []                 # inserted no new payment row
    assert loan.principal_balance_cents == before_balance  # applied nothing
