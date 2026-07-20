"""DB-free tests for WS-F repayment modes (regular / add_on / special / payoff).

MONEY-PATH coverage, all with in-memory fakes (same idiom as
tests/test_loan_ledger.py — no DB):

  * pure allocators: per-mode semantics + validation rules,
  * PROPERTY TESTS (Hypothesis): every mode conserves money exactly
    (allocation sums to the cash) and preserves the BalanceView invariant —
    applying the allocation as a ledger event moves payoff down by exactly
    the applied cash (principal + interest due + fees + add-on == payoff,
    always),
  * record_payment integration per mode: ledger row, plan handling
    (regular fills installments; add_on/special leave the plan untouched;
    payoff waives + closes), zero-debt closure for ANY mode,
  * borrower Pay Now (loan_payments): mode gating (special never offered),
    add-on offered only when a balance exists, payoff server-quoted +
    non-editable, and payoff-drift fallback at webhook settlement.

Run just this file:

    source .venv/bin/activate && \
        python -m pytest tests/test_repayment_modes.py -p no:warnings -q
"""
from datetime import date, datetime, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services import loan_ledger, loan_payments
from app.services.interest_engine import (
    BalanceView,
    LedgerEvent,
    REPAYMENT_MODES,
    allocate_add_on_payment,
    allocate_payment,
    allocate_payoff_payment,
    allocate_regular_payment,
    allocate_special_payment,
    compute_balances,
)
from app.services.loan_servicing import record_payment

D = date


# --- fakes (test_loan_ledger.py idiom) ----------------------------------------


class _NoResultQuery:
    def filter(self, *a, **k):
        return self

    def first(self):
        return None

    def all(self):
        return []


class _FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    def query(self, *a, **k):
        return _NoResultQuery()

    def flush(self):
        pass

    def commit(self):
        pass

    def refresh(self, obj):
        pass


class _Txn:
    _n = 0

    def __init__(self, txn_type, effective_date, *, seq=None, amount=0,
                 principal=0, interest=0, fees=0, add_on=0, reverses=None,
                 payment_type=None, repayment_mode=None):
        _Txn._n += 1
        self.id = f"txn-{_Txn._n}"
        self.loan_id = "loan-1"
        self.seq = seq if seq is not None else _Txn._n
        self.reference = f"vendor-loan-1-{self.seq}"
        self.txn_type = txn_type
        self.payment_type = payment_type
        self.repayment_mode = repayment_mode
        self.amount_cents = amount
        self.principal_cents = principal
        self.interest_cents = interest
        self.fees_cents = fees
        self.add_on_cents = add_on
        self.effective_date = effective_date
        self.processing_date = effective_date
        self.reverses_transaction_id = reverses
        self.created_by = "test"
        self.comment = None
        self.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _Item:
    def __init__(self, n, principal, interest, *, due=None, status="scheduled"):
        self.installment_number = n
        self.principal_cents = principal
        self.interest_cents = interest
        self.total_cents = principal + interest
        self.status = status
        self.paid_cents = 0
        self.due_date = due or D(2026, 1 + n, 1)


class _Loan:
    def __init__(self, *, principal_cents=100_000, annual_rate_bps=3650,
                 disbursed_on=D(2026, 1, 1), schedule=None, transactions=None):
        self.id = "loan-1"
        self.application_id = None
        self.principal_cents = principal_cents
        self.principal_balance_cents = principal_cents
        self.annual_rate_bps = annual_rate_bps
        self.status = "active"
        self.disbursed_at = (
            datetime(disbursed_on.year, disbursed_on.month, disbursed_on.day,
                     tzinfo=timezone.utc)
            if disbursed_on
            else None
        )
        self.schedule = schedule if schedule is not None else [
            _Item(n, 25_000, 1_500) for n in range(1, 5)
        ]
        self.transactions = transactions if transactions is not None else []


# Per-diem for the default loan: 100_000 * 0.3650 / 365 = 100 cents/day.


def _pay(db, loan, amount, on, mode="regular", **kw):
    return record_payment(
        db, loan, amount,
        datetime(on.year, on.month, on.day, 12, 0, tzinfo=timezone.utc),
        kw.pop("method", "zumrails_collection"),
        repayment_mode=mode, **kw,
    )


def _balances(principal=100_000, interest=3_000, fees=500, add_on=4_500):
    return BalanceView(
        as_of=D(2026, 1, 31),
        outstanding_principal_cents=principal,
        interest_due_cents=interest,
        fees_due_cents=fees,
        add_on_balance_cents=add_on,
    )


# --- allocators: per-mode semantics --------------------------------------------


def test_regular_waterfall_interest_then_principal_then_fees():
    a = allocate_regular_payment(103_600, _balances())
    assert (a.interest_cents, a.principal_cents, a.fees_cents, a.add_on_cents) == (
        3_000, 100_100, 500, 0
    )
    assert a.overpayment_cents == 100  # cash beyond everything regular touches
    assert a.total_cents == 103_600


def test_add_on_pays_the_add_on_bucket_only():
    a = allocate_add_on_payment(4_500, _balances())
    assert (a.interest_cents, a.principal_cents, a.fees_cents) == (0, 0, 0)
    assert a.add_on_cents == 4_500
    a = allocate_add_on_payment(1, _balances())  # partial add-on payment OK
    assert a.add_on_cents == 1


def test_add_on_rejects_amount_beyond_the_add_on_balance():
    with pytest.raises(ValueError, match="exceeds the add-on balance"):
        allocate_add_on_payment(4_501, _balances(add_on=4_500))
    with pytest.raises(ValueError, match="exceeds the add-on balance"):
        allocate_add_on_payment(1, _balances(add_on=0))


def test_special_is_100_percent_principal():
    a = allocate_special_payment(60_000, _balances())
    assert a.principal_cents == 60_000
    assert (a.interest_cents, a.fees_cents, a.add_on_cents) == (0, 0, 0)


def test_special_rejects_amount_beyond_outstanding_principal():
    with pytest.raises(ValueError, match="exceeds outstanding principal"):
        allocate_special_payment(100_001, _balances(principal=100_000))


def test_payoff_requires_the_exact_computed_amount_and_clears_all_buckets():
    b = _balances()  # payoff = 100_000 + 3_000 + 500 + 4_500 = 108_000
    a = allocate_payoff_payment(108_000, b)
    assert (a.principal_cents, a.interest_cents, a.fees_cents, a.add_on_cents) == (
        100_000, 3_000, 500, 4_500
    )
    for wrong in (107_999, 108_001, 1):
        with pytest.raises(ValueError, match="must equal the computed payoff"):
            allocate_payoff_payment(wrong, b)


def test_allocate_payment_dispatch_and_unknown_mode():
    assert allocate_payment("regular", 1_000, _balances()).interest_cents == 1_000
    with pytest.raises(ValueError, match="unknown repayment mode"):
        allocate_payment("bogus", 1_000, _balances())
    with pytest.raises(ValueError):
        allocate_payment("regular", 0, _balances())


# --- PROPERTY TESTS: money conservation + BalanceView invariant ----------------
#
# For EVERY mode and any balance state: (1) the allocation's buckets sum to the
# cash exactly (integer cents, nothing lost or minted); (2) applying it as a
# ledger event preserves the header invariant — the new payoff equals the old
# payoff minus the cash actually absorbed by the buckets.

_bucket = st.integers(min_value=0, max_value=50_000_000)
_cash = st.integers(min_value=1, max_value=200_000_000)


def _apply(balances: BalanceView, allocation) -> BalanceView:
    """Replay the allocation through the REAL engine walk (compute_balances)."""
    return compute_balances(
        principal_cents=balances.outstanding_principal_cents,
        annual_rate_bps=0,  # no accrual: isolate the allocation's effect
        accrual_start=None,
        events=[
            LedgerEvent(
                effective_date=balances.as_of,
                # Seed the non-principal buckets as charges, then pay.
                interest_charged_cents=balances.interest_due_cents,
                fees_charged_cents=balances.fees_due_cents,
                add_on_charged_cents=balances.add_on_balance_cents,
            ),
            LedgerEvent(
                effective_date=balances.as_of,
                principal_paid_cents=allocation.principal_cents,
                interest_paid_cents=allocation.interest_cents,
                fees_paid_cents=allocation.fees_cents,
                add_on_paid_cents=allocation.add_on_cents,
            ),
        ],
        as_of=balances.as_of,
    )


@settings(max_examples=400)
@given(principal=_bucket, interest=_bucket, fees=_bucket, add_on=_bucket, cash=_cash)
def test_property_every_mode_conserves_money_and_the_invariant(
    principal, interest, fees, add_on, cash
):
    balances = BalanceView(
        as_of=D(2026, 1, 31),
        outstanding_principal_cents=principal,
        interest_due_cents=interest,
        fees_due_cents=fees,
        add_on_balance_cents=add_on,
    )
    for mode in REPAYMENT_MODES:
        amount = balances.payoff_cents if mode == "payoff" else cash
        try:
            allocation = allocate_payment(mode, amount, balances)
        except ValueError:
            # Mode-rule rejection (add-on/special caps, payoff=0) is a valid
            # outcome — the point is it NEVER mis-allocates instead.
            continue

        # (1) Money conservation: the row ties out to the cash exactly.
        assert allocation.total_cents == amount
        assert allocation.interest_cents >= 0
        assert allocation.principal_cents >= 0
        assert allocation.fees_cents >= 0
        assert allocation.add_on_cents >= 0

        # (2) Bucket-cap safety: no bucket is paid beyond what it holds
        # (principal absorbs any overpayment residue by design).
        assert allocation.interest_cents <= balances.interest_due_cents
        assert allocation.fees_cents <= balances.fees_due_cents
        assert allocation.add_on_cents <= balances.add_on_balance_cents

        # (3) The header invariant survives: payoff after == sum of buckets
        # after, and equals payoff before minus what the buckets absorbed.
        after = _apply(balances, allocation)
        assert after.payoff_cents == (
            after.outstanding_principal_cents
            + after.interest_due_cents
            + after.fees_due_cents
            + after.add_on_balance_cents
        )
        absorbed = amount - allocation.overpayment_cents
        assert after.payoff_cents == balances.payoff_cents - absorbed

        # (4) Payoff clears the debt completely.
        if mode == "payoff":
            assert after.payoff_cents == 0


@settings(max_examples=200)
@given(principal=_bucket, interest=_bucket, fees=_bucket, add_on=_bucket)
def test_property_payoff_amount_is_exactly_the_four_buckets(
    principal, interest, fees, add_on
):
    balances = BalanceView(
        as_of=D(2026, 1, 31),
        outstanding_principal_cents=principal,
        interest_due_cents=interest,
        fees_due_cents=fees,
        add_on_balance_cents=add_on,
    )
    if balances.payoff_cents <= 0:
        return
    a = allocate_payoff_payment(balances.payoff_cents, balances)
    assert a.principal_cents == principal
    assert a.interest_cents == interest
    assert a.fees_cents == fees
    assert a.add_on_cents == add_on
    assert a.overpayment_cents == 0


# --- record_payment integration (MONEY-PATH) ------------------------------------


def test_record_payment_add_on_mode_targets_the_add_on_bucket_only():
    # NSF-style fee: 4_500 sits in the non-accruing add-on bucket.
    loan = _Loan(transactions=[_Txn("fee", D(2026, 1, 10), add_on=4_500)])
    db = _FakeSession()
    _pay(db, loan, 4_500, D(2026, 1, 31), mode="add_on")

    txn = loan.transactions[-1]
    assert txn.repayment_mode == "add_on"
    assert txn.add_on_cents == 4_500
    assert txn.interest_cents == 0 and txn.principal_cents == 0 and txn.fees_cents == 0
    # Principal balance untouched; plan untouched.
    assert loan.principal_balance_cents == 100_000
    assert all(s.status == "scheduled" and s.paid_cents == 0 for s in loan.schedule)
    view = loan_ledger.loan_balances(loan, as_of=D(2026, 1, 31))
    assert view.add_on_balance_cents == 0
    assert view.outstanding_principal_cents == 100_000


def test_record_payment_add_on_rejects_overshoot_without_touching_state():
    loan = _Loan(transactions=[_Txn("fee", D(2026, 1, 10), add_on=1_000)])
    db = _FakeSession()
    with pytest.raises(ValueError, match="exceeds the add-on balance"):
        _pay(db, loan, 1_001, D(2026, 1, 31), mode="add_on")
    assert len(loan.transactions) == 1  # no ledger row appended
    assert db.added == []               # nothing staged on the session
    assert loan.principal_balance_cents == 100_000


def test_record_payment_special_mode_is_pure_principal_and_leaves_the_plan_alone():
    loan = _Loan()
    db = _FakeSession()
    _pay(db, loan, 40_000, D(2026, 1, 31), mode="special", created_by="admin-1")

    txn = loan.transactions[0]
    assert txn.repayment_mode == "special"
    assert txn.principal_cents == 40_000
    assert txn.interest_cents == 0 and txn.fees_cents == 0 and txn.add_on_cents == 0
    assert loan.principal_balance_cents == 60_000
    # Dave's borrower-protection rule: the as-agreed installments are untouched.
    assert all(s.status == "scheduled" and s.paid_cents == 0 for s in loan.schedule)
    # Interest accrued to date is STILL due (special bypasses it, not forgives it).
    view = loan_ledger.loan_balances(loan, as_of=D(2026, 1, 31))
    assert view.interest_due_cents == 3_000


def test_record_payment_special_rejects_amount_beyond_principal():
    loan = _Loan()
    with pytest.raises(ValueError, match="exceeds outstanding principal"):
        _pay(_FakeSession(), loan, 100_001, D(2026, 1, 31), mode="special")


def test_record_payment_payoff_closes_the_loan_and_waives_open_installments():
    # Day 30: payoff = 100_000 principal + 3_000 accrued = 103_000.
    loan = _Loan()
    loan.schedule[0].status = "late"  # an overdue installment is waived too
    db = _FakeSession()
    _pay(db, loan, 103_000, D(2026, 1, 31), mode="payoff", created_by="admin-1")

    txn = loan.transactions[0]
    assert txn.repayment_mode == "payoff"
    assert txn.principal_cents == 100_000
    assert txn.interest_cents == 3_000
    assert loan.status == "paid_off"
    assert loan.principal_balance_cents == 0
    assert all(s.status == "waived" for s in loan.schedule)
    view = loan_ledger.loan_balances(loan, as_of=D(2026, 1, 31))
    assert view.payoff_cents == 0


def test_record_payment_payoff_rejects_any_other_amount():
    loan = _Loan()
    for wrong in (102_999, 103_001):
        with pytest.raises(ValueError, match="must equal the computed payoff"):
            _pay(_FakeSession(), loan, wrong, D(2026, 1, 31), mode="payoff")
    assert loan.transactions == [] and loan.status == "active"


def test_record_payment_payoff_includes_fees_and_add_on_buckets():
    loan = _Loan(transactions=[
        _Txn("fee", D(2026, 1, 10), fees=500),
        _Txn("fee", D(2026, 1, 15), add_on=4_500),
    ])
    # payoff at day 30 = 100_000 + 3_000 + 500 + 4_500 = 108_000
    _pay(_FakeSession(), loan, 108_000, D(2026, 1, 31), mode="payoff")
    txn = loan.transactions[-1]
    assert (txn.principal_cents, txn.interest_cents, txn.fees_cents, txn.add_on_cents) == (
        100_000, 3_000, 500, 4_500
    )
    assert loan.status == "paid_off"


def test_record_payment_regular_payment_that_zeroes_debt_also_closes_the_loan():
    # Dave: ANY payment bringing total debt to zero closes the loan — a regular
    # payment of exactly payoff behaves like a payoff.
    loan = _Loan()
    _pay(_FakeSession(), loan, 103_000, D(2026, 1, 31), mode="regular")
    assert loan.status == "paid_off"
    assert all(s.status in ("paid", "waived") for s in loan.schedule)
    assert loan_ledger.loan_balances(loan, as_of=D(2026, 1, 31)).payoff_cents == 0


def test_record_payment_regular_still_fills_the_plan_and_stays_open():
    loan = _Loan()
    _pay(_FakeSession(), loan, 26_500, D(2026, 1, 31), mode="regular")
    assert loan.schedule[0].status == "paid"
    assert loan.schedule[1].status == "scheduled"
    assert loan.status == "active"


def test_record_payment_unknown_mode_rejected():
    with pytest.raises(ValueError, match="unknown repayment mode"):
        _pay(_FakeSession(), _Loan(), 1_000, D(2026, 1, 31), mode="bogus")


def test_record_payment_event_payload_carries_the_mode():
    loan = _Loan(transactions=[_Txn("fee", D(2026, 1, 10), add_on=500)])
    db = _FakeSession()
    _pay(db, loan, 500, D(2026, 1, 31), mode="add_on")
    events = [o for o in db.added if getattr(o, "event_type", None) == "loan_payment_recorded"]
    assert len(events) == 1
    assert events[0].payload["after"]["repayment_mode"] == "add_on"


# --- borrower Pay Now (loan_payments) -------------------------------------------


class _ScheduleQuery:
    """Fake query for outstanding_cents: returns the loan's OPEN items the way
    the real status filter would."""

    def __init__(self, items):
        self._items = items

    def filter(self, *a, **k):
        return self

    def all(self):
        return [i for i in self._items if i.status in loan_payments._OPEN_ITEM_STATUSES]

    def first(self):
        return None


class _PayNowSession(_FakeSession):
    def __init__(self, loan):
        super().__init__()
        self._loan = loan

    def query(self, model, *a, **k):
        name = getattr(model, "__name__", str(model))
        if "ScheduleItem" in name:
            return _ScheduleQuery(self._loan.schedule)
        return _NoResultQuery()

    def execute(self, *a, **k):
        class _R:
            def first(self):
                return None
        return _R()


class _FakeCollectionResult:
    def __init__(self, status):
        from app.services.payments.zumrails_adapter import TransactionStatus

        self.transaction_id = "zum-txn-1"
        self.status = TransactionStatus(status)


class _FakeZumrails:
    def __init__(self, status="pending"):
        self._status = status
        self.calls = []

    def create_collection(self, **kw):
        self.calls.append(kw)
        return _FakeCollectionResult(self._status)


def test_payment_options_hides_add_on_until_a_balance_exists():
    loan = _Loan()
    opts = loan_payments.payment_options(_PayNowSession(loan), loan)
    assert opts["modes"] == ["regular", "payoff"]
    assert opts["add_on_balance_cents"] == 0

    loan_with_addon = _Loan(transactions=[_Txn("fee", D(2026, 1, 10), add_on=4_500)])
    opts = loan_payments.payment_options(_PayNowSession(loan_with_addon), loan_with_addon)
    assert opts["modes"] == ["regular", "add_on", "payoff"]
    assert opts["add_on_balance_cents"] == 4_500
    assert opts["payoff_cents"] > 0


def test_borrower_cannot_initiate_a_special_payment():
    loan = _Loan()
    with pytest.raises(loan_payments.PaymentValidationError, match="not available"):
        loan_payments.initiate_payment(
            _PayNowSession(loan), loan, 1_000, mode="special",
            payer_id="p-1", zumrails=_FakeZumrails(),
        )


def test_borrower_add_on_requires_a_balance_and_respects_the_cap():
    loan = _Loan()  # no add-on balance
    with pytest.raises(loan_payments.PaymentValidationError, match="no add-on balance"):
        loan_payments.initiate_payment(
            _PayNowSession(loan), loan, 1_000, mode="add_on",
            payer_id="p-1", zumrails=_FakeZumrails(),
        )
    loan2 = _Loan(transactions=[_Txn("fee", D(2026, 1, 10), add_on=2_000)])
    with pytest.raises(loan_payments.PaymentValidationError, match="exceeds the add-on"):
        loan_payments.initiate_payment(
            _PayNowSession(loan2), loan2, 2_001, mode="add_on",
            payer_id="p-1", zumrails=_FakeZumrails(),
        )
    zum = _FakeZumrails()
    out = loan_payments.initiate_payment(
        _PayNowSession(loan2), loan2, 2_000, mode="add_on",
        payer_id="p-1", zumrails=zum,
    )
    assert out["repayment_mode"] == "add_on"
    assert zum.calls[0]["amount_cents"] == 2_000


def test_borrower_payoff_is_server_quoted_and_non_editable():
    loan = _Loan()
    session = _PayNowSession(loan)
    quote = loan_ledger.loan_balances(loan, as_of=date.today()).payoff_cents

    # A client-supplied amount that doesn't match the quote is rejected…
    with pytest.raises(loan_payments.PaymentValidationError, match="server-computed"):
        loan_payments.initiate_payment(
            session, loan, quote + 1, mode="payoff",
            payer_id="p-1", zumrails=_FakeZumrails(),
        )
    # …omitting the amount charges the server quote.
    zum = _FakeZumrails()
    out = loan_payments.initiate_payment(
        session, loan, None, mode="payoff", payer_id="p-1", zumrails=zum,
    )
    assert out["amount_cents"] == quote
    assert out["repayment_mode"] == "payoff"
    assert zum.calls[0]["amount_cents"] == quote


def test_borrower_regular_amount_still_required_and_capped():
    loan = _Loan()
    session = _PayNowSession(loan)
    with pytest.raises(loan_payments.PaymentValidationError, match="must be positive"):
        loan_payments.initiate_payment(
            session, loan, None, mode="regular", payer_id="p-1", zumrails=_FakeZumrails(),
        )
    owed = loan_payments.outstanding_cents(session, loan)
    with pytest.raises(loan_payments.PaymentValidationError, match="exceeds outstanding"):
        loan_payments.initiate_payment(
            session, loan, owed + 1, mode="regular", payer_id="p-1", zumrails=_FakeZumrails(),
        )


def test_initiation_event_carries_the_mode():
    loan = _Loan()
    session = _PayNowSession(loan)
    loan_payments.initiate_payment(
        session, loan, 5_000, mode="regular", payer_id="p-1", zumrails=_FakeZumrails(),
    )
    events = [o for o in session.added
              if getattr(o, "event_type", None) == loan_payments.INITIATED_EVENT]
    assert len(events) == 1
    assert events[0].payload["repayment_mode"] == "regular"


# --- webhook settlement: payoff drift fallback ----------------------------------


class _SettleSession(_PayNowSession):
    """_PayNowSession + the webhook lookups (initiation event, loan row)."""

    def __init__(self, loan, init_payload):
        super().__init__(loan)
        self._init_payload = init_payload

    def query(self, model, *a, **k):
        name = getattr(model, "__name__", str(model))
        if name == "PlatformLoan":
            loan = self._loan

            class _Q:
                def filter(self, *a, **k):
                    return self

                def first(self):
                    return loan

            return _Q()
        return super().query(model, *a, **k)

    def execute(self, *a, **k):
        payload = self._init_payload

        class _R:
            def first(self):
                return (payload,)
        return _R()


def test_settlement_applies_payoff_mode_when_the_quote_still_matches():
    loan = _Loan()
    quote = loan_ledger.loan_balances(loan, as_of=date.today()).payoff_cents
    session = _SettleSession(loan, {
        "loan_id": str(loan.id), "amount_cents": quote,
        "repayment_mode": "payoff", "transaction_id": "zum-txn-9",
    })
    assert loan_payments.on_collection_complete(session, "zum-txn-9") is True
    assert loan.transactions[-1].repayment_mode == "payoff"
    assert loan.status == "paid_off"


def test_settlement_falls_back_to_regular_when_the_payoff_drifted():
    loan = _Loan()
    quote = loan_ledger.loan_balances(loan, as_of=date.today()).payoff_cents
    stale = quote - 300  # e.g. quoted 3 per-diem days ago
    session = _SettleSession(loan, {
        "loan_id": str(loan.id), "amount_cents": stale,
        "repayment_mode": "payoff", "transaction_id": "zum-txn-9",
    })
    assert loan_payments.on_collection_complete(session, "zum-txn-9") is True
    txn = loan.transactions[-1]
    assert txn.repayment_mode == "regular"  # money never lost, never mis-forced
    completed = [o for o in session.added
                 if getattr(o, "event_type", None) == loan_payments.COMPLETED_EVENT]
    assert completed[0].payload["payoff_mismatch"] == {
        "quoted_payoff_cents": stale,
        "payoff_cents_at_settlement": quote,
    }
    # The residue (drift) survives on the ledger for staff true-up — the
    # regular fallback never pretends the debt was cleared. (Loan status here
    # follows the pre-existing plan-complete rule; the LEDGER is money truth.)
    assert loan_ledger.loan_balances(loan, as_of=date.today()).payoff_cents == 300


def test_settlement_without_a_mode_defaults_to_regular():
    # Pre-WS-F initiation events carry no repayment_mode.
    loan = _Loan()
    session = _SettleSession(loan, {
        "loan_id": str(loan.id), "amount_cents": 5_000,
        "transaction_id": "zum-txn-9",
    })
    assert loan_payments.on_collection_complete(session, "zum-txn-9") is True
    assert loan.transactions[-1].repayment_mode == "regular"
