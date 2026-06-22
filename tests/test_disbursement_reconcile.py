"""Unit tests for disbursement reconciliation — ``reconcile_stuck_disbursements``.

DELIBERATELY DB-FREE and NETWORK-FREE (the suite shares a remote DB and must not
be run wholesale). We use an in-memory fake Session that serves a canned list of
"stuck" loans for the ``query(...).filter(...).order_by(...).limit(...).all()``
chain, and a MOCKED Zumrails adapter injected via ``zumrails=`` so no real
adapter, credential lookup, or HTTP call ever happens. The reconcile path is
asserted to delegate to the real ``on_disbursement_complete`` /
``on_disbursement_failed`` lifecycle transitions (no duplicated state logic).

Run JUST this file:

    source .venv/bin/activate && \
        python -m pytest tests/test_disbursement_reconcile.py -p no:warnings -q
"""
from datetime import datetime, timedelta, timezone

from app.services import loan_lifecycle
from app.services.payments.zumrails_adapter import (
    TransactionResult,
    TransactionStatus,
    TransientZumrailsError,
)


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


class _ReconcileSession:
    """Fake Session that returns a fixed list of stuck loans for the reconcile
    query chain and records commits. The query is ``in_progress + stale``; we
    don't re-evaluate the SQL filter here (the caller passes already-stuck
    loans), we just replay the chain shape: filter/order_by/limit/all."""

    def __init__(self, stuck):
        self._stuck = stuck
        self.commits = 0

    def add(self, obj):
        pass

    def commit(self):
        self.commits += 1

    def refresh(self, obj, **kwargs):
        pass

    def query(self, *args, **kwargs):
        return _ReconcileQuery(self._stuck)


class _ReconcileQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        # Only reached when _build_zumrails_adapter looks up the (absent)
        # integration_settings row in the provider-disabled test → None.
        return None


class _Loan:
    def __init__(
        self,
        *,
        loan_id="loan-1",
        status="pending_disbursement",
        disbursement_status="in_progress",
        disbursement_ref="zr_tx_1",
    ):
        self.id = loan_id
        self.application_id = "app-1"
        self.principal_cents = 300
        self.principal_balance_cents = 300
        self.status = status
        self.agreement_status = "signed"
        self.agreement_ref = "snd_1"
        self.disbursement_status = disbursement_status
        self.disbursement_ref = disbursement_ref
        self.disbursed_at = None


class _MockZumrails:
    """Returns a programmed status for each ``get_transaction_status`` call,
    keyed by transaction id; records the ids it was polled with."""

    def __init__(self, status_by_ref):
        self._status_by_ref = status_by_ref
        self.polled = []

    def get_transaction_status(self, transaction_id):
        self.polled.append(transaction_id)
        status = self._status_by_ref[transaction_id]
        return TransactionResult(
            transaction_id=transaction_id,
            status=status,
            raw_status=status.value,
            amount_cents=300,
            currency="CAD",
            direction="disbursement",
            client_transaction_id="loan-1",
        )


class _RaisingZumrails:
    def __init__(self, exc):
        self._exc = exc
        self.polled = []

    def get_transaction_status(self, transaction_id):
        self.polled.append(transaction_id)
        raise self._exc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reconcile_completes_loan_when_vendor_now_completed():
    loan = _Loan(disbursement_ref="zr_done")
    db = _ReconcileSession([loan])
    zr = _MockZumrails({"zr_done": TransactionStatus.COMPLETED})

    result = loan_lifecycle.reconcile_stuck_disbursements(db, zumrails=zr)

    # Advanced via on_disbursement_complete (real transition).
    assert loan.status == "active"
    assert loan.disbursement_status == "completed"
    assert loan.disbursed_at is not None
    assert result.examined == 1
    assert result.loans_completed == ["loan-1"]
    assert zr.polled == ["zr_done"]


def test_reconcile_fails_loan_when_vendor_failed():
    loan = _Loan(disbursement_ref="zr_bad")
    db = _ReconcileSession([loan])
    zr = _MockZumrails({"zr_bad": TransactionStatus.FAILED})

    result = loan_lifecycle.reconcile_stuck_disbursements(db, zumrails=zr)

    # Advanced via on_disbursement_failed (real transition) — never activates.
    assert loan.disbursement_status == "failed"
    assert loan.status == "pending_disbursement"
    assert result.loans_failed == ["loan-1"]


def test_reconcile_cancelled_marks_failed():
    loan = _Loan(disbursement_ref="zr_cancel")
    db = _ReconcileSession([loan])
    zr = _MockZumrails({"zr_cancel": TransactionStatus.CANCELLED})

    result = loan_lifecycle.reconcile_stuck_disbursements(db, zumrails=zr)

    assert loan.disbursement_status == "failed"
    assert result.loans_failed == ["loan-1"]


def test_reconcile_leaves_still_pending_untouched():
    loan = _Loan(disbursement_ref="zr_pending")
    db = _ReconcileSession([loan])
    zr = _MockZumrails({"zr_pending": TransactionStatus.PENDING})

    result = loan_lifecycle.reconcile_stuck_disbursements(db, zumrails=zr)

    assert loan.disbursement_status == "in_progress"
    assert loan.status == "pending_disbursement"
    assert result.still_pending == 1
    assert result.loans_completed == []
    assert result.loans_failed == []


def test_reconcile_unknown_status_left_in_progress():
    loan = _Loan(disbursement_ref="zr_unknown")
    db = _ReconcileSession([loan])
    zr = _MockZumrails({"zr_unknown": TransactionStatus.UNKNOWN})

    result = loan_lifecycle.reconcile_stuck_disbursements(db, zumrails=zr)

    assert loan.disbursement_status == "in_progress"
    assert result.still_pending == 1


def test_reconcile_noop_when_provider_disabled():
    loan = _Loan()
    db = _ReconcileSession([loan])

    # No adapter injected and _build_zumrails_adapter returns None (no settings).
    result = loan_lifecycle.reconcile_stuck_disbursements(db)

    assert result.provider_disabled is True
    assert result.examined == 0
    assert loan.disbursement_status == "in_progress"  # untouched


def test_reconcile_poll_error_leaves_loan_for_next_sweep():
    loan = _Loan(disbursement_ref="zr_err")
    db = _ReconcileSession([loan])
    zr = _RaisingZumrails(TransientZumrailsError("boom"))

    result = loan_lifecycle.reconcile_stuck_disbursements(db, zumrails=zr)

    # No terminal state guessed from a failed poll.
    assert loan.disbursement_status == "in_progress"
    assert result.errors == 1
    assert result.examined == 1


def test_reconcile_skips_loan_with_no_ref():
    loan = _Loan(disbursement_ref=None)
    db = _ReconcileSession([loan])
    zr = _MockZumrails({})

    result = loan_lifecycle.reconcile_stuck_disbursements(db, zumrails=zr)

    assert result.skipped_no_ref == 1
    assert zr.polled == []  # never polled — nothing to poll
    assert loan.disbursement_status == "in_progress"


def test_reconcile_mixed_batch_buckets_each_loan_once():
    done = _Loan(loan_id="l-done", disbursement_ref="r-done")
    failed = _Loan(loan_id="l-failed", disbursement_ref="r-failed")
    pending = _Loan(loan_id="l-pending", disbursement_ref="r-pending")
    db = _ReconcileSession([done, failed, pending])
    zr = _MockZumrails(
        {
            "r-done": TransactionStatus.COMPLETED,
            "r-failed": TransactionStatus.FAILED,
            "r-pending": TransactionStatus.IN_PROGRESS,
        }
    )

    result = loan_lifecycle.reconcile_stuck_disbursements(db, zumrails=zr)

    assert result.examined == 3
    assert result.loans_completed == ["l-done"]
    assert result.loans_failed == ["l-failed"]
    assert result.still_pending == 1
    assert done.status == "active"
    assert failed.disbursement_status == "failed"
    assert pending.disbursement_status == "in_progress"


def test_reconcile_idempotent_on_already_completed_loan():
    # A loan that a webhook already completed but that still matched the query
    # (e.g. race): on_disbursement_complete short-circuits, no double-activate.
    loan = _Loan(
        loan_id="l-x",
        status="active",
        disbursement_status="completed",
        disbursement_ref="r-x",
    )
    loan.disbursed_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    db = _ReconcileSession([loan])
    zr = _MockZumrails({"r-x": TransactionStatus.COMPLETED})

    result = loan_lifecycle.reconcile_stuck_disbursements(db, zumrails=zr)

    assert loan.status == "active"
    assert loan.disbursed_at == datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert result.loans_completed == ["l-x"]


def test_reconcile_passes_threshold_cutoff(monkeypatch):
    # The query is built from now - stuck_for; assert the function honors a
    # custom now/stuck_for without exploding (cutoff math path).
    loan = _Loan(disbursement_ref="zr_t")
    db = _ReconcileSession([loan])
    zr = _MockZumrails({"zr_t": TransactionStatus.COMPLETED})
    fixed_now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)

    result = loan_lifecycle.reconcile_stuck_disbursements(
        db, zumrails=zr, now=fixed_now, stuck_for=timedelta(hours=1)
    )

    assert result.loans_completed == ["loan-1"]
