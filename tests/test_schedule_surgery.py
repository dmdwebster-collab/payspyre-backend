"""DB-free tests for WS-F scheduled-transaction surgery.

Dave (03__WP_Servicing): "this is actually a very, very important section."

Covers the service primitives (app/services/schedule_surgery.py) with
in-memory fakes:

  * suspend / unsuspend an individual installment (state rules, restore logic,
    MANDATORY comment, platform_events audit with actor),
  * add / cancel a custom scheduled transaction (mode validation, status flip
    instead of hard delete, audit),
  * the automation-skip guarantees: the delinquency-aging and dunning jobs
    must SKIP suspended installments — pinned against the modules' status
    tuples and exercised through run_delinquency_aging's flow,
  * migration 050 chain pin (merge-train convention).

Run just this file:

    source .venv/bin/activate && \
        python -m pytest tests/test_schedule_surgery.py -p no:warnings -q
"""
import importlib.util
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from app.services import dunning, loan_payments, schedule_surgery
from app.services.loan_servicing import _AGEABLE_ITEM_STATUSES

D = date


# --- fakes -------------------------------------------------------------------


class _Item:
    def __init__(self, n, *, status="scheduled", paid_cents=0, due=None):
        self.id = uuid4()
        self.loan_id = "loan-1"
        self.installment_number = n
        self.due_date = due or D(2026, 1 + n, 1)
        self.principal_cents = 25_000
        self.interest_cents = 1_500
        self.total_cents = 26_500
        self.status = status
        self.paid_cents = paid_cents


class _Custom:
    pass  # rows are created by the service; this exists only for isinstance-free fakes


class _Loan:
    def __init__(self, schedule=None):
        self.id = uuid4()
        self.application_id = None
        self.status = "active"
        self.schedule = schedule if schedule is not None else [_Item(n) for n in (1, 2, 3)]


class _ItemQuery:
    """Fake query honoring the (id, loan_id) ownership filter the service uses."""

    def __init__(self, items, model_name):
        self._items = items
        self._model_name = model_name
        self._wanted_ids = None

    def filter(self, *criteria):
        # Extract literal values bound in `Model.id == x` / `Model.loan_id == y`.
        for c in criteria:
            try:
                col = c.left.name
                val = c.right.value
            except AttributeError:
                continue
            if col == "id":
                self._wanted_ids = val
        return self

    def first(self):
        for item in self._items:
            if self._wanted_ids is None or item.id == self._wanted_ids:
                return item
        return None


class _FakeSession:
    def __init__(self, loan):
        self.loan = loan
        self.added = []
        self.custom_rows = []

    def add(self, obj):
        self.added.append(obj)
        if type(obj).__name__ == "PlatformLoanCustomTransaction":
            self.custom_rows.append(obj)

    def flush(self):
        pass

    def commit(self):
        pass

    def query(self, model, *a, **k):
        name = getattr(model, "__name__", str(model))
        if name == "PlatformLoanScheduleItem":
            return _ItemQuery(self.loan.schedule, name)
        if name == "PlatformLoanCustomTransaction":
            return _ItemQuery(self.custom_rows, name)
        return _ItemQuery([], name)


def _events(db, event_type=None):
    evs = [o for o in db.added if type(o).__name__ == "PlatformEvent"]
    if event_type:
        evs = [e for e in evs if e.event_type == event_type]
    return evs


# --- suspend / unsuspend --------------------------------------------------------


def test_suspend_flips_status_and_audits_with_actor_and_comment():
    loan = _Loan()
    db = _FakeSession(loan)
    item = loan.schedule[1]
    out = schedule_surgery.suspend_installment(
        db, loan, item.id, comment="Borrower requested; process on 07-15", actor="staff-7"
    )
    assert out is item and item.status == "suspended"
    (ev,) = _events(db, schedule_surgery.ITEM_SUSPENDED_EVENT)
    assert ev.actor == "staff-7"
    assert ev.payload["comment"] == "Borrower requested; process on 07-15"
    assert ev.payload["installment_number"] == 2
    assert ev.payload["prior_status"] == "scheduled"
    assert ev.payload["loan_id"] == str(loan.id)


@pytest.mark.parametrize("status", ["scheduled", "partial", "late"])
def test_suspend_allowed_from_open_statuses(status):
    loan = _Loan([_Item(1, status=status)])
    db = _FakeSession(loan)
    schedule_surgery.suspend_installment(
        db, loan, loan.schedule[0].id, comment="c", actor="a"
    )
    assert loan.schedule[0].status == "suspended"


@pytest.mark.parametrize("status", ["paid", "waived", "suspended"])
def test_suspend_rejected_from_settled_or_already_suspended(status):
    loan = _Loan([_Item(1, status=status)])
    db = _FakeSession(loan)
    with pytest.raises(schedule_surgery.ScheduleSurgeryError, match="cannot be suspended"):
        schedule_surgery.suspend_installment(
            db, loan, loan.schedule[0].id, comment="c", actor="a"
        )
    assert loan.schedule[0].status == status
    assert _events(db) == []


def test_suspend_requires_a_comment():
    loan = _Loan()
    db = _FakeSession(loan)
    for bad in ("", "   ", None):
        with pytest.raises(schedule_surgery.ScheduleSurgeryError, match="comment is required"):
            schedule_surgery.suspend_installment(
                db, loan, loan.schedule[0].id, comment=bad, actor="a"
            )
    assert loan.schedule[0].status == "scheduled"


def test_suspend_unknown_or_foreign_item_404s_cleanly():
    loan = _Loan()
    db = _FakeSession(loan)
    with pytest.raises(schedule_surgery.ScheduleSurgeryError, match="not found"):
        schedule_surgery.suspend_installment(db, loan, uuid4(), comment="c", actor="a")


def test_unsuspend_restores_scheduled_or_partial_and_audits():
    fresh = _Item(1, status="suspended", paid_cents=0)
    partial = _Item(2, status="suspended", paid_cents=5_000)
    loan = _Loan([fresh, partial])
    db = _FakeSession(loan)

    schedule_surgery.unsuspend_installment(db, loan, fresh.id, comment="resume", actor="staff-7")
    schedule_surgery.unsuspend_installment(db, loan, partial.id, comment="resume", actor="staff-7")

    assert fresh.status == "scheduled"
    assert partial.status == "partial"  # cash on the item survives the round-trip
    evs = _events(db, schedule_surgery.ITEM_UNSUSPENDED_EVENT)
    assert [e.payload["restored_status"] for e in evs] == ["scheduled", "partial"]


def test_unsuspend_rejects_non_suspended_items_and_requires_comment():
    loan = _Loan()
    db = _FakeSession(loan)
    with pytest.raises(schedule_surgery.ScheduleSurgeryError, match="is not suspended"):
        schedule_surgery.unsuspend_installment(
            db, loan, loan.schedule[0].id, comment="c", actor="a"
        )
    loan.schedule[0].status = "suspended"
    with pytest.raises(schedule_surgery.ScheduleSurgeryError, match="comment is required"):
        schedule_surgery.unsuspend_installment(
            db, loan, loan.schedule[0].id, comment=" ", actor="a"
        )


# --- custom transactions ----------------------------------------------------------


def test_add_custom_transaction_creates_row_and_audits():
    loan = _Loan()
    db = _FakeSession(loan)
    row = schedule_surgery.add_custom_transaction(
        db, loan,
        scheduled_date=D(2026, 7, 15),
        amount_cents=50_000,
        repayment_mode="regular",
        comment="Borrower request, authorization obtained",
        actor="staff-7",
    )
    assert row.loan_id == loan.id
    assert row.scheduled_date == D(2026, 7, 15)
    assert row.amount_cents == 50_000
    assert row.status == "scheduled"
    assert row.comment == "Borrower request, authorization obtained"
    assert row.created_by == "staff-7"
    (ev,) = _events(db, schedule_surgery.CUSTOM_TXN_ADDED_EVENT)
    assert ev.payload["amount_cents"] == 50_000
    assert ev.payload["repayment_mode"] == "regular"
    # The amortization plan itself was never touched (Dave's protection rule).
    assert all(s.status == "scheduled" for s in loan.schedule)


def test_add_custom_transaction_validates_mode_amount_and_comment():
    loan = _Loan()
    db = _FakeSession(loan)
    with pytest.raises(schedule_surgery.ScheduleSurgeryError, match="unknown repayment mode"):
        schedule_surgery.add_custom_transaction(
            db, loan, scheduled_date=D(2026, 7, 15), amount_cents=1,
            repayment_mode="bogus", comment="c", actor="a",
        )
    with pytest.raises(schedule_surgery.ScheduleSurgeryError, match="must be positive"):
        schedule_surgery.add_custom_transaction(
            db, loan, scheduled_date=D(2026, 7, 15), amount_cents=0,
            repayment_mode="regular", comment="c", actor="a",
        )
    with pytest.raises(schedule_surgery.ScheduleSurgeryError, match="comment is required"):
        schedule_surgery.add_custom_transaction(
            db, loan, scheduled_date=D(2026, 7, 15), amount_cents=1,
            repayment_mode="regular", comment="", actor="a",
        )
    assert db.custom_rows == []


def test_cancel_custom_transaction_is_a_status_flip_never_a_delete():
    loan = _Loan()
    db = _FakeSession(loan)
    row = schedule_surgery.add_custom_transaction(
        db, loan, scheduled_date=D(2026, 7, 15), amount_cents=1_000,
        repayment_mode="regular", comment="add", actor="staff-7",
    )
    row.id = uuid4()  # the DB would assign this on flush
    out = schedule_surgery.cancel_custom_transaction(
        db, loan, row.id, comment="borrower paid at clinic instead", actor="staff-8"
    )
    assert out is row
    assert row.status == "cancelled"
    assert row.cancelled_by == "staff-8"
    assert row.cancelled_at is not None
    assert row in db.custom_rows  # still present — auditable, not deleted
    (ev,) = _events(db, schedule_surgery.CUSTOM_TXN_CANCELLED_EVENT)
    assert ev.payload["comment"] == "borrower paid at clinic instead"

    # A cancelled (or processed) row cannot be cancelled again.
    with pytest.raises(schedule_surgery.ScheduleSurgeryError, match="only a scheduled"):
        schedule_surgery.cancel_custom_transaction(
            db, loan, row.id, comment="again", actor="staff-8"
        )


# --- the automation-skip guarantees ------------------------------------------------
#
# The entire point of suspension: the delinquency/dunning automation must skip
# a suspended installment. These pin the status tuples each job filters on.


def test_dunning_scan_skips_suspended_installments():
    assert "suspended" not in dunning._OPEN_ITEM_STATUSES


def test_delinquency_aging_skips_suspended_installments():
    assert "suspended" not in _AGEABLE_ITEM_STATUSES
    # And the tuple still covers exactly the open, chaseable plan states.
    assert set(_AGEABLE_ITEM_STATUSES) == {"scheduled", "partial"}


def test_borrower_outstanding_still_includes_suspended_debt():
    # Suspension parks the automation — it does NOT forgive the money. The
    # borrower's outstanding figure (Pay Now cap) keeps counting it.
    assert "suspended" in loan_payments._OPEN_ITEM_STATUSES


def test_run_delinquency_aging_never_flips_a_suspended_item():
    """Exercise the aging pass through a fake session: only scheduled/partial
    overdue items are handed to it (per _AGEABLE_ITEM_STATUSES); a suspended
    item is filtered out before it can be flipped late."""
    from app.services.loan_servicing import run_delinquency_aging

    suspended = _Item(1, status="suspended", due=D(2026, 1, 1))
    open_item = _Item(2, status="scheduled", due=D(2026, 1, 1))

    class _AgingSession:
        def __init__(self):
            self.committed = False

        def query(self, *cols):
            items = [i for i in (suspended, open_item)
                     if i.status in _AGEABLE_ITEM_STATUSES and i.due_date < D(2026, 6, 1)]

            class _Q:
                def filter(self, *a, **k):
                    return self

                def all(self):
                    # loan_id projection query (second call) returns tuples.
                    if cols and getattr(cols[0], "name", "") == "loan_id":
                        return [(i.loan_id,) for i in items if i.status == "late"]
                    return items

                def first(self):
                    return None

            return _Q()

        def commit(self):
            self.committed = True

    result = run_delinquency_aging(_AgingSession(), as_of=D(2026, 6, 1))
    assert open_item.status == "late"
    assert suspended.status == "suspended"  # NEVER flipped
    assert result.installments_flagged_late == 1


# --- migration chain pin (merge-train convention) -----------------------------------


def test_migration_chain():
    """Pin 050_repayment_modes onto 049_loan_ledger — the merge train re-chains
    down_revisions, and a silent fork would split the alembic head."""
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "050_repayment_modes.py"
    spec = importlib.util.spec_from_file_location("migration_050", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "050_repayment_modes"
    assert mod.down_revision == "049_loan_ledger"


def test_app_imports_with_surgery_wired():
    """Smoke: the app (with the new model, service and endpoints) imports and
    exposes the WS-F routes."""
    from app.main import app

    paths = {r.path for r in app.routes}
    assert "/api/v1/admin/loans/{loan_id}/schedule/{item_id}/suspend" in paths
    assert "/api/v1/admin/loans/{loan_id}/schedule/{item_id}/unsuspend" in paths
    assert "/api/v1/admin/loans/{loan_id}/schedule/custom" in paths
    assert "/api/v1/admin/loans/{loan_id}/schedule/custom/{custom_id}/cancel" in paths
    assert "/api/v1/admin/loans/{loan_id}/schedule/changes" in paths
    assert "/api/applicant/v1/loans/{loan_id}/payment-options" in paths
