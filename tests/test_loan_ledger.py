"""DB-free tests for the loan ledger service + record_payment ledger integration.

Covers WS-A's money-path change end-to-end with in-memory fakes (same idiom as
tests/test_loan_servicing.py — no DB):

  * row -> LedgerEvent mapping (payment / adjustment / fee / disbursement /
    reversal semantics),
  * loan_balances / ledger_view (running balances + header invariant),
  * record_payment writing an immutable ledger row with the actuals allocation
    (interest -> principal -> fees) and repointing principal_balance_cents at
    the ledger truth,
  * compute_payoff = actuals payoff with the 4-bucket invariant.

Run just this file:

    source .venv/bin/activate && \
        python -m pytest tests/test_loan_ledger.py -p no:warnings -q
"""
from datetime import date, datetime, timezone

from app.services import loan_ledger
from app.services.loan_servicing import compute_payoff, record_payment


D = date


# --- fakes -------------------------------------------------------------------


class _NoResultQuery:
    def filter(self, *a, **k):
        return self

    def first(self):
        return None


class _FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    def query(self, *a, **k):
        return _NoResultQuery()

    def commit(self):
        pass

    def refresh(self, obj):
        pass


class _Txn:
    """Attribute-compatible stand-in for PlatformLoanTransaction rows."""

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
    def __init__(self, n, principal, interest):
        self.installment_number = n
        self.principal_cents = principal
        self.interest_cents = interest
        self.total_cents = principal + interest
        self.status = "scheduled"
        self.paid_cents = 0


class _Loan:
    def __init__(self, *, principal_cents=100_000, annual_rate_bps=3650,
                 disbursed_on=D(2026, 1, 1), schedule=None, transactions=None):
        self.id = "loan-1"
        self.application_id = None  # no vendor lookup in DB-free tests
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


# --- balances / row mapping --------------------------------------------------


def test_loan_balances_accrues_per_diem_from_disbursement():
    loan = _Loan()
    view = loan_ledger.loan_balances(loan, as_of=D(2026, 1, 31))
    assert view.outstanding_principal_cents == 100_000
    assert view.interest_due_cents == 3_000
    assert view.payoff_cents == 103_000


def test_payment_and_fee_rows_move_the_buckets():
    loan = _Loan(transactions=[
        _Txn("fee", D(2026, 1, 10), fees=100),
        _Txn("fee", D(2026, 1, 15), add_on=4_500),
        _Txn("payment", D(2026, 1, 31), amount=5_100,
             principal=2_000, interest=3_000, fees=100),
    ])
    view = loan_ledger.loan_balances(loan, as_of=D(2026, 1, 31))
    assert view.outstanding_principal_cents == 98_000
    assert view.interest_due_cents == 0
    assert view.fees_due_cents == 0
    assert view.add_on_balance_cents == 4_500
    assert view.payoff_cents == 102_500


def test_adjustment_rows_reduce_like_payments():
    # Vendor accommodation: reduce amount owed without cash (Dave's manual
    # "Adjustment" payment type / txn).
    loan = _Loan(transactions=[
        _Txn("adjustment", D(2026, 1, 1), amount=10_000, principal=10_000),
    ])
    view = loan_ledger.loan_balances(loan, as_of=D(2026, 1, 1))
    assert view.outstanding_principal_cents == 90_000


def test_disbursement_rows_are_informational_never_double_counted():
    # The principal advance is anchored on loan.principal_cents; a disbursement
    # ledger row must not add on top.
    loan = _Loan(transactions=[
        _Txn("disbursement", D(2026, 1, 1), amount=100_000),
    ])
    view = loan_ledger.loan_balances(loan, as_of=D(2026, 1, 31))
    assert view.outstanding_principal_cents == 100_000
    assert view.interest_due_cents == 3_000


def test_reversal_row_compensates_the_original():
    original = _Txn("payment", D(2026, 1, 31), amount=5_000,
                    principal=2_000, interest=3_000)
    reversal = _Txn("reversal", D(2026, 2, 10), amount=5_000,
                    principal=2_000, interest=3_000, reverses=original.id)
    loan = _Loan(transactions=[original, reversal])
    view = loan_ledger.loan_balances(loan, as_of=D(2026, 2, 10))
    # Principal restored; interest paid is put back on top of the accrual.
    assert view.outstanding_principal_cents == 100_000
    assert view.interest_due_cents == 3_980  # (3000-3000) + 10d x 98 + 3000 back


def test_dangling_reversal_is_a_noop():
    loan = _Loan(transactions=[
        _Txn("reversal", D(2026, 1, 15), amount=5_000, principal=5_000,
             reverses="missing-id"),
    ])
    view = loan_ledger.loan_balances(loan, as_of=D(2026, 1, 15))
    assert view.outstanding_principal_cents == 100_000


def test_rows_replay_in_effective_date_then_seq_order():
    later = _Txn("payment", D(2026, 2, 1), seq=2, amount=1_000, principal=1_000)
    earlier = _Txn("payment", D(2026, 1, 15), seq=1, amount=1_000, principal=1_000)
    loan = _Loan(transactions=[later, earlier])  # deliberately out of order
    rows = loan_ledger.sorted_transactions(loan)
    assert [r.seq for r in rows] == [1, 2]


def test_next_seq_and_reference_format():
    loan = _Loan()
    assert loan_ledger.next_seq(loan) == 1
    loan.transactions.append(_Txn("payment", D(2026, 1, 15), seq=7))
    assert loan_ledger.next_seq(loan) == 8
    assert loan_ledger.build_reference("v-1", "loan-1", 8) == "v-1-loan-1-8"
    assert loan_ledger.build_reference(None, "loan-1", 1) == "none-loan-1-1"


def test_payment_type_mapping():
    assert loan_ledger.payment_type_for_method("cash") == "cash"
    assert loan_ledger.payment_type_for_method("personal cheque") == "check"
    assert loan_ledger.payment_type_for_method("credit_card") == "credit_card"
    assert loan_ledger.payment_type_for_method("adjustment") == "adjustment"
    assert loan_ledger.payment_type_for_method("zumrails_collection") == "eft"
    assert loan_ledger.payment_type_for_method(None) == "eft"


# --- ledger_view (read endpoint payload) --------------------------------------


def test_ledger_view_running_balances_and_header_invariant():
    loan = _Loan(transactions=[
        _Txn("fee", D(2026, 1, 15), add_on=4_500),
        _Txn("payment", D(2026, 1, 31), amount=5_000, principal=2_000, interest=3_000,
             payment_type="eft", repayment_mode="regular"),
    ])
    out = loan_ledger.ledger_view(loan, as_of=D(2026, 2, 10))

    assert out["loan_id"] == "loan-1"
    assert len(out["transactions"]) == 2

    fee_row, pay_row = out["transactions"]
    assert fee_row["txn_type"] == "fee"
    assert fee_row["running_balances"]["add_on_balance_cents"] == 4_500
    # Running interest is accrued to the ROW's effective date (14 days x 100).
    assert fee_row["running_balances"]["interest_due_cents"] == 1_400

    assert pay_row["running_balances"]["outstanding_principal_cents"] == 98_000
    assert pay_row["running_balances"]["interest_due_cents"] == 0

    balances = out["balances"]
    # Header invariant: the four buckets sum exactly to payoff.
    assert balances["payoff_cents"] == (
        balances["outstanding_principal_cents"]
        + balances["interest_due_cents"]
        + balances["fees_due_cents"]
        + balances["add_on_balance_cents"]
    )
    # 10 more days at 98/day after the payment.
    assert balances["interest_due_cents"] == 980
    assert balances["payoff_cents"] == 98_000 + 980 + 4_500


# --- record_payment ledger integration (MONEY-PATH) ---------------------------


def _pay(db, loan, amount, on, **kw):
    return record_payment(
        db, loan, amount,
        datetime(on.year, on.month, on.day, 12, 0, tzinfo=timezone.utc),
        kw.pop("method", "zumrails_collection"), **kw,
    )


def test_record_payment_writes_actuals_allocated_ledger_row():
    loan = _Loan()
    db = _FakeSession()
    _pay(db, loan, 5_000, D(2026, 1, 31))  # 30 days -> 3_000 interest accrued

    assert len(loan.transactions) == 1
    txn = loan.transactions[0]
    assert txn.txn_type == "payment"
    assert txn.repayment_mode == "regular"
    assert txn.payment_type == "eft"
    assert txn.amount_cents == 5_000
    # Regular waterfall: accrued interest first, then principal.
    assert txn.interest_cents == 3_000
    assert txn.principal_cents == 2_000
    assert txn.fees_cents == 0 and txn.add_on_cents == 0
    # Dual dates: effective = processing = the received date (no backdating yet).
    assert txn.effective_date == D(2026, 1, 31)
    assert txn.processing_date == D(2026, 1, 31)
    assert txn.reference == "none-loan-1-1"
    assert txn.created_by == "system"
    # The loan's balance column now follows the LEDGER (money truth).
    assert loan.principal_balance_cents == 98_000
    # The ledger row is also in the session's pending adds.
    assert txn in db.added


def test_record_payment_late_payment_pays_more_interest_less_principal():
    on_time = _Loan()
    _pay(_FakeSession(), on_time, 5_000, D(2026, 1, 31))   # day 30
    late = _Loan()
    _pay(_FakeSession(), late, 5_000, D(2026, 2, 10))      # day 40 (10 late)

    assert on_time.transactions[0].interest_cents == 3_000
    assert late.transactions[0].interest_cents == 4_000
    # Same cash, less principal repaid when late.
    assert late.principal_balance_cents == 99_000
    assert on_time.principal_balance_cents == 98_000


def test_record_payment_early_payment_pays_less_interest():
    loan = _Loan()
    _pay(_FakeSession(), loan, 5_000, D(2026, 1, 11))  # day 10
    assert loan.transactions[0].interest_cents == 1_000
    assert loan.principal_balance_cents == 96_000


def test_record_payment_second_payment_accrues_on_reduced_principal():
    loan = _Loan()
    db = _FakeSession()
    _pay(db, loan, 5_000, D(2026, 1, 31))    # -> outstanding 98_000
    _pay(db, loan, 10_980, D(2026, 2, 10))   # 10 days x 98 = 980 interest

    second = loan.transactions[1]
    assert second.seq == 2
    assert second.interest_cents == 980
    assert second.principal_cents == 10_000
    assert loan.principal_balance_cents == 88_000


def test_record_payment_created_by_and_comment_land_on_the_row():
    loan = _Loan()
    _pay(_FakeSession(), loan, 1_000, D(2026, 1, 5),
         method="cash", created_by="admin-9", comment="paid at clinic")
    txn = loan.transactions[0]
    assert txn.created_by == "admin-9"
    assert txn.comment == "paid at clinic"
    assert txn.payment_type == "cash"


def test_record_payment_keeps_schedule_status_flipping_intact():
    # The plan book still flips installment statuses oldest-first even though
    # the money allocation is now ledger-driven.
    loan = _Loan()
    _pay(_FakeSession(), loan, 26_500, D(2026, 1, 31))  # = installment total
    assert loan.schedule[0].status == "paid"
    assert loan.schedule[0].paid_cents == 26_500
    assert loan.schedule[1].status == "scheduled"


def test_compute_payoff_uses_the_actuals_ledger():
    loan = _Loan()
    db = _FakeSession()
    _pay(db, loan, 5_000, D(2026, 1, 31))

    quote = compute_payoff(db, loan, as_of=D(2026, 2, 10))
    assert quote.principal_cents == 98_000
    assert quote.accrued_interest_cents == 980       # 10 days x 98, actuals
    assert quote.fees_due_cents == 0
    assert quote.add_on_balance_cents == 0
    assert quote.payoff_cents == 98_980              # 0% future interest
    # Invariant: buckets sum to payoff.
    assert quote.payoff_cents == (
        quote.principal_cents + quote.accrued_interest_cents
        + quote.fees_due_cents + quote.add_on_balance_cents
    )
