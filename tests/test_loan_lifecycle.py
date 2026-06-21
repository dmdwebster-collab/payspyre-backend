"""Unit tests for the loan LIFECYCLE state machine + deepened servicing — P9.x.

DELIBERATELY DB-FREE and NETWORK-FREE (the suite shares a remote DB and must not
be run wholesale). We use:

  * lightweight in-memory fakes for the loan / schedule / payment objects and a
    fake Session that records add/commit/refresh,
  * MOCKED SignNow / Zumrails adapters injected straight into the lifecycle
    functions (the ``signnow=`` / ``zumrails=`` / ``recipient_id=`` params), so
    no real adapter, credential lookup, or HTTP call ever happens.

Run JUST this file:

    source .venv/bin/activate && \
        python -m pytest tests/test_loan_lifecycle.py -p no:warnings -q
"""
from datetime import date, datetime, timezone

import pytest

from app.services import loan_lifecycle
from app.services.loan_servicing import (
    AgingResult,
    PayoffQuote,
    compute_payoff,
    generate_statement,
    run_delinquency_aging,
)
from app.services.esign.signnow_adapter import SignNowSendResult
from app.services.payments.zumrails_adapter import (
    TransactionResult,
    TransactionStatus,
)


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


class _FakeSession:
    """Captures add/commit/refresh; supports the tiny query() surface the
    servicing functions use (statement idempotency lookup)."""

    def __init__(self, query_results=None):
        self.added = []
        self.commits = 0
        # query_results: optional mapping for canned .first()/.all() responses.
        self._query_results = query_results or {}

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commits += 1

    def refresh(self, obj, **kwargs):
        # Accept with_for_update= (used by the disbursement row lock); the fake has
        # no real row to lock/reload, so it's a no-op.
        pass

    def query(self, *args, **kwargs):
        return _FakeQuery(self._query_results)


class _FakeQuery:
    def __init__(self, results):
        self._results = results

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._results.get("first")

    def all(self):
        return self._results.get("all", [])


class _Item:
    def __init__(self, n, principal, interest, due_date, status="scheduled", paid=0):
        self.installment_number = n
        self.principal_cents = principal
        self.interest_cents = interest
        self.total_cents = principal + interest
        self.due_date = due_date
        self.status = status
        self.paid_cents = paid
        self.loan_id = "loan-1"


class _Payment:
    def __init__(self, amount_cents, received_at):
        self.amount_cents = amount_cents
        self.received_at = received_at


class _Loan:
    def __init__(
        self,
        *,
        schedule=None,
        payments=None,
        principal_cents=300,
        principal_balance_cents=300,
        status="pending_disbursement",
        agreement_status="not_sent",
        disbursement_status="not_started",
    ):
        self.id = "loan-1"
        self.application_id = "app-1"
        self.principal_cents = principal_cents
        self.principal_balance_cents = principal_balance_cents
        self.status = status
        self.agreement_status = agreement_status
        self.agreement_ref = None
        self.disbursement_status = disbursement_status
        self.disbursement_ref = None
        self.disbursed_at = None
        self.schedule = schedule or []
        self.payments = payments or []


def _dt(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Mock adapters
# ---------------------------------------------------------------------------


class _MockSignNow:
    def __init__(self, document_id="snd_123"):
        self.document_id = document_id
        self.calls = []

    def send_for_signature(self, **kwargs):
        self.calls.append(kwargs)
        return SignNowSendResult(document_id=self.document_id, signing_url="https://x")


class _MockZumrails:
    def __init__(self, status=TransactionStatus.IN_PROGRESS, txn_id="zr_tx_1"):
        self._status = status
        self._txn_id = txn_id
        self.calls = []

    def create_disbursement(self, **kwargs):
        self.calls.append(kwargs)
        return TransactionResult(
            transaction_id=self._txn_id,
            status=self._status,
            raw_status=self._status.value,
            amount_cents=kwargs["amount_cents"],
            currency="CAD",
            direction="disbursement",
            client_transaction_id=kwargs.get("client_transaction_id"),
        )


# ===========================================================================
# send_agreement
# ===========================================================================


def test_send_agreement_sends_and_stores_ref():
    loan = _Loan()
    db = _FakeSession(query_results={"first": None})  # no signnow settings row
    adapter = _MockSignNow(document_id="snd_abc")

    out = loan_lifecycle.send_agreement(db, loan, signnow=adapter)

    assert out.agreement_status == "sent"
    assert out.agreement_ref == "snd_abc"
    assert len(adapter.calls) == 1
    assert db.commits == 1


def test_send_agreement_idempotent_when_already_sent():
    loan = _Loan(agreement_status="sent")
    loan.agreement_ref = "snd_existing"
    db = _FakeSession()
    adapter = _MockSignNow(document_id="snd_new")

    out = loan_lifecycle.send_agreement(db, loan, signnow=adapter)

    # No second invite, ref unchanged.
    assert adapter.calls == []
    assert out.agreement_ref == "snd_existing"
    assert db.commits == 0


def test_send_agreement_noop_when_provider_disabled():
    loan = _Loan()
    db = _FakeSession()
    # No adapter injected and _build_signnow_adapter returns None (no row).
    out = loan_lifecycle.send_agreement(db, loan)

    assert out.agreement_status == "not_sent"
    assert out.agreement_ref is None
    assert db.commits == 0


# ===========================================================================
# on_agreement_signed → triggers disbursement
# ===========================================================================


def test_signed_marks_signed_and_triggers_disbursement():
    loan = _Loan(agreement_status="sent")
    db = _FakeSession()
    zr = _MockZumrails(status=TransactionStatus.IN_PROGRESS, txn_id="zr_99")

    out = loan_lifecycle.on_agreement_signed(
        db, loan, recipient_id="recipient-1", zumrails=zr
    )

    assert out.agreement_status == "signed"
    assert out.disbursement_status == "in_progress"
    assert out.disbursement_ref == "zr_99"
    assert len(zr.calls) == 1
    # Disbursement amount is the full principal; client ref is the loan id.
    assert zr.calls[0]["amount_cents"] == loan.principal_cents
    assert zr.calls[0]["client_transaction_id"] == str(loan.id)


def test_signed_synchronous_completed_activates_immediately():
    loan = _Loan(agreement_status="sent")
    db = _FakeSession()
    zr = _MockZumrails(status=TransactionStatus.COMPLETED, txn_id="zr_sync")

    out = loan_lifecycle.on_agreement_signed(
        db, loan, recipient_id="recipient-1", zumrails=zr
    )

    assert out.disbursement_status == "completed"
    assert out.status == "active"
    assert out.disbursed_at is not None


def test_signed_is_idempotent_does_not_double_disburse():
    loan = _Loan(agreement_status="signed", disbursement_status="in_progress")
    loan.disbursement_ref = "zr_already"
    db = _FakeSession()
    zr = _MockZumrails()

    out = loan_lifecycle.on_agreement_signed(
        db, loan, recipient_id="recipient-1", zumrails=zr
    )

    # Already in_progress -> no second push.
    assert zr.calls == []
    assert out.disbursement_ref == "zr_already"


def test_signed_without_recipient_noops_disbursement():
    loan = _Loan(agreement_status="sent")
    db = _FakeSession(query_results={"first": None})
    zr = _MockZumrails()

    # No recipient injected and _recipient_for_loan resolves to None.
    out = loan_lifecycle.on_agreement_signed(db, loan, zumrails=zr)

    assert out.agreement_status == "signed"  # signed still recorded
    assert out.disbursement_status == "not_started"  # disbursement deferred
    assert zr.calls == []


def test_signed_provider_disabled_records_signed_but_defers_disbursement():
    loan = _Loan(agreement_status="sent")
    db = _FakeSession(query_results={"first": None})  # no zumrails row -> None

    out = loan_lifecycle.on_agreement_signed(db, loan, recipient_id="r-1")

    assert out.agreement_status == "signed"
    assert out.disbursement_status == "not_started"


# ===========================================================================
# on_disbursement_complete
# ===========================================================================


def test_disbursement_complete_activates_loan():
    loan = _Loan(
        agreement_status="signed",
        disbursement_status="in_progress",
        status="pending_disbursement",
    )
    loan.disbursement_ref = "zr_1"
    db = _FakeSession()

    out = loan_lifecycle.on_disbursement_complete(db, loan, ref="zr_1")

    assert out.status == "active"
    assert out.disbursement_status == "completed"
    assert out.disbursed_at is not None
    assert db.commits == 1


def test_disbursement_complete_is_idempotent():
    loan = _Loan(
        agreement_status="signed",
        disbursement_status="completed",
        status="active",
    )
    loan.disbursed_at = _dt(2026, 6, 1)
    db = _FakeSession()

    out = loan_lifecycle.on_disbursement_complete(db, loan, ref="zr_1")

    assert out.status == "active"
    # No re-commit on idempotent replay.
    assert db.commits == 0
    assert out.disbursed_at == _dt(2026, 6, 1)


def test_disbursement_failed_marks_failed_not_active():
    loan = _Loan(agreement_status="signed", disbursement_status="in_progress")
    db = _FakeSession()

    out = loan_lifecycle.on_disbursement_failed(db, loan, ref="zr_x")

    assert out.disbursement_status == "failed"
    assert out.status == "pending_disbursement"


def test_agreement_declined_blocks_signing():
    loan = _Loan(agreement_status="sent")
    db = _FakeSession()

    out = loan_lifecycle.on_agreement_declined(db, loan)
    assert out.agreement_status == "declined"

    # A later "signed" webhook for a declined loan does not disburse.
    zr = _MockZumrails()
    out2 = loan_lifecycle.on_agreement_signed(
        db, loan, recipient_id="r-1", zumrails=zr
    )
    assert out2.agreement_status == "declined"
    assert zr.calls == []


# ===========================================================================
# book_loan idempotency (mocked create + query)
# ===========================================================================


def test_book_loan_idempotent_returns_existing(monkeypatch):
    existing = _Loan(status="pending_disbursement")
    db = _FakeSession(query_results={"first": existing})

    called = {"create": 0}

    def _fake_create(*a, **k):
        called["create"] += 1
        return _Loan()

    monkeypatch.setattr(
        loan_lifecycle.loan_servicing,
        "create_loan_from_application",
        _fake_create,
    )

    app = type("App", (), {"id": "app-1"})()
    out = loan_lifecycle.book_loan(db, app)

    assert out is existing
    assert called["create"] == 0  # no second booking


def test_book_loan_creates_when_absent(monkeypatch):
    db = _FakeSession(query_results={"first": None})
    created = _Loan()

    def _fake_create(*a, **k):
        return created

    monkeypatch.setattr(
        loan_lifecycle.loan_servicing,
        "create_loan_from_application",
        _fake_create,
    )

    app = type("App", (), {"id": "app-1"})()
    out = loan_lifecycle.book_loan(db, app)
    assert out is created


# ===========================================================================
# Delinquency aging (in-memory)
# ===========================================================================


class _AgingSession:
    """Fake Session whose query() returns canned item/loan lists per model."""

    def __init__(self, schedule_items, loans):
        self._items = schedule_items
        self._loans = loans
        self.commits = 0

    def commit(self):
        self.commits += 1

    def query(self, *cols):
        # Distinguish "schedule items" vs "schedule.loan_id" vs "loans" by the
        # first column passed. We import the real models for identity checks.
        from app.models.platform.loan import (
            PlatformLoan,
            PlatformLoanScheduleItem,
        )

        col = cols[0]
        if col is PlatformLoanScheduleItem:
            return _AgingItemQuery(self._items, project_loan_id=False)
        if col is PlatformLoanScheduleItem.loan_id:
            return _AgingItemQuery(self._items, project_loan_id=True)
        if col is PlatformLoan:
            return _AgingLoanQuery(self._loans)
        raise AssertionError("unexpected query column")


class _AgingItemQuery:
    def __init__(self, items, project_loan_id):
        self._items = items
        self._project = project_loan_id
        self._status_in = None
        self._status_eq = None
        self._due_before = None

    def filter(self, *clauses):
        # We cannot introspect SQLAlchemy clauses cheaply here; the aging code
        # calls .filter(due<as_of, status.in_(...)) for overdue items and
        # .filter(status=='late') for the late-loan rollup. We approximate by
        # re-deriving from the items at .all() time using flags set by the
        # caller-known query shape. Simplest: store nothing, compute in .all().
        self._filtered = True
        return self

    def all(self):
        # Heuristic split: the overdue query is the one used to flag items; the
        # late rollup query asks for already-late loan ids. We expose both via
        # the items' current status, matching how the real SQL would behave.
        if self._project:
            # late-loan rollup: loan_ids of items currently 'late'
            return [(i.loan_id,) for i in self._items if i.status == "late"]
        # overdue items: scheduled/partial past due (the test sets due dates so
        # all provided items qualify when this query runs first).
        return [i for i in self._items if i.status in ("scheduled", "partial")]


class _AgingLoanQuery:
    def __init__(self, loans):
        self._loans = loans

    def filter(self, *clauses):
        return self

    def all(self):
        # Only active loans become delinquent.
        return [ln for ln in self._loans if ln.status == "active"]


def test_run_delinquency_aging_flips_late_and_delinquent():
    overdue = _Item(1, 100, 10, due_date=date(2026, 5, 1), status="scheduled")
    overdue2 = _Item(2, 100, 10, due_date=date(2026, 6, 1), status="partial", paid=20)
    overdue2.loan_id = "loan-1"
    loan = _Loan(status="active")
    db = _AgingSession([overdue, overdue2], [loan])

    result = run_delinquency_aging(db, as_of=date(2026, 6, 19))

    assert isinstance(result, AgingResult)
    assert overdue.status == "late"
    assert overdue2.status == "late"
    assert result.installments_flagged_late == 2
    assert loan.status == "delinquent"
    assert result.loans_marked_delinquent == ["loan-1"]
    assert db.commits == 1


def test_run_delinquency_aging_skips_non_active_loans():
    overdue = _Item(1, 100, 10, due_date=date(2026, 5, 1), status="scheduled")
    paid_off_loan = _Loan(status="paid_off")
    db = _AgingSession([overdue], [paid_off_loan])

    result = run_delinquency_aging(db, as_of=date(2026, 6, 19))

    assert overdue.status == "late"  # item still flagged
    assert paid_off_loan.status == "paid_off"  # loan status untouched
    assert result.loans_marked_delinquent == []


# ===========================================================================
# Payoff (pure)
# ===========================================================================


def test_compute_payoff_principal_plus_accrued_interest():
    # 3 installments due monthly; as_of after installments 1 & 2 are due.
    sched = [
        _Item(1, 100, 10, due_date=date(2026, 4, 1), status="paid", paid=110),
        _Item(2, 100, 10, due_date=date(2026, 5, 1), status="scheduled", paid=0),
        _Item(3, 100, 10, due_date=date(2026, 6, 1), status="scheduled", paid=0),
    ]
    # Installment 1 fully paid -> principal balance reduced by 100 -> 200 left.
    loan = _Loan(schedule=sched, principal_cents=300, principal_balance_cents=200)

    # as_of covers installments 1 & 2 (due <= as_of), not 3.
    quote = compute_payoff(None, loan, as_of=date(2026, 5, 15))

    assert isinstance(quote, PayoffQuote)
    assert quote.principal_cents == 200
    # Accrued interest = unpaid interest of installments 1 & 2.
    # Inst 1 fully paid (interest covered). Inst 2 unpaid -> 10 accrued.
    assert quote.accrued_interest_cents == 10
    assert quote.payoff_cents == 210


def test_compute_payoff_excludes_future_interest():
    sched = [
        _Item(1, 100, 10, due_date=date(2026, 4, 1), status="scheduled", paid=0),
        _Item(2, 100, 10, due_date=date(2026, 7, 1), status="scheduled", paid=0),
    ]
    loan = _Loan(schedule=sched, principal_cents=200, principal_balance_cents=200)

    quote = compute_payoff(None, loan, as_of=date(2026, 5, 1))

    # Only installment 1 has accrued (due before as_of); installment 2 is future.
    assert quote.accrued_interest_cents == 10
    assert quote.payoff_cents == 210


def test_compute_payoff_partial_payment_fills_principal_before_interest():
    # Installment due & partially paid: 50 paid, principal 100 -> all 50 to
    # principal, interest 10 still fully unpaid.
    sched = [
        _Item(1, 100, 10, due_date=date(2026, 4, 1), status="partial", paid=50),
    ]
    loan = _Loan(schedule=sched, principal_cents=100, principal_balance_cents=50)

    quote = compute_payoff(None, loan, as_of=date(2026, 5, 1))

    assert quote.principal_cents == 50
    assert quote.accrued_interest_cents == 10
    assert quote.payoff_cents == 60


# ===========================================================================
# Statement generation (in-memory)
# ===========================================================================


def test_generate_statement_splits_principal_and_interest_in_window():
    # Schedule: 3 x (100 principal + 10 interest) = 110 each.
    sched = [
        _Item(1, 100, 10, due_date=date(2026, 4, 1)),
        _Item(2, 100, 10, due_date=date(2026, 5, 1)),
        _Item(3, 100, 10, due_date=date(2026, 6, 1)),
    ]
    # One payment of 110 inside the window (fully covers installment 1).
    payments = [_Payment(110, _dt(2026, 4, 15))]
    # After that payment principal_balance is 200 (300 - 100).
    loan = _Loan(
        schedule=sched,
        payments=payments,
        principal_cents=300,
        principal_balance_cents=200,
    )
    db = _FakeSession(query_results={"first": None})  # no existing statement

    stmt = generate_statement(db, loan, (date(2026, 4, 1), date(2026, 4, 30)))

    assert stmt.principal_paid_cents == 100
    assert stmt.interest_paid_cents == 10
    # No payments after the window -> closing == current balance.
    assert stmt.closing_balance_cents == 200
    # Opening = closing + principal paid in window.
    assert stmt.opening_balance_cents == 300
    assert db.commits == 1


def test_generate_statement_idempotent_returns_existing():
    existing = object()
    loan = _Loan()
    db = _FakeSession(query_results={"first": existing})

    out = generate_statement(db, loan, (date(2026, 4, 1), date(2026, 4, 30)))
    assert out is existing
    assert db.commits == 0


def test_generate_statement_rejects_inverted_period():
    loan = _Loan()
    db = _FakeSession(query_results={"first": None})
    with pytest.raises(ValueError):
        generate_statement(db, loan, (date(2026, 4, 30), date(2026, 4, 1)))
