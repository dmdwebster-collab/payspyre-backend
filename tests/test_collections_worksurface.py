"""Unit tests for the collections work-surface (WS-C full parity).

DELIBERATELY DB-free (fan-out protocol: the suite shares a remote DB and must
not be run wholesale by agents):

  * tier gating, alpha-range matching, promise evaluation, the header
    identity and the queue sort key are PURE — tested directly,
    boundary-by-boundary;
  * ``assign_loan`` / ``unassign_loan`` / ``refresh_promise_statuses`` /
    ``apply_maintenance_fee`` are exercised with in-memory fakes (simple
    objects + a fake Session), same idiom as tests/test_delinquency_buckets.py.

Run JUST this file:

    source .venv/bin/activate && \
        python -m pytest tests/test_collections_worksurface.py -p no:warnings -q
"""
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.models.platform.collections_work import (
    PlatformCollectorAssignment,
    PlatformInsolvencyMaintenanceFee,
    PlatformPromiseToPay,
)
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import PlatformLoanTransaction
from app.services.collections_worksurface import (
    ASSIGN_METHOD_BULK_ALPHA,
    ASSIGN_METHOD_MANUAL,
    COLLECTOR_ASSIGNED_EVENT,
    COLLECTOR_UNASSIGNED_EVENT,
    JUNIOR_ALLOWED_BUCKETS,
    MAINTENANCE_FEE_EVENT,
    PTP_BROKEN,
    PTP_KEPT,
    PTP_OPEN,
    CollectionsError,
    alpha_matches,
    apply_maintenance_fee,
    assign_loan,
    display_bucket,
    evaluate_promise,
    header_view,
    insolvency_sort_key,
    loan_days_past_due,
    loan_has_past_due_unpaid,
    normalize_alpha,
    refresh_promise_statuses,
    tier_allows_bucket,
    unassign_loan,
)

TODAY = date(2026, 7, 20)


# ---------------------------------------------------------------------------
# Tier gating (Dave's junior/senior split)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bucket,junior_ok",
    [
        ("current", True),
        ("current_month_late", True),
        ("pot_30", True),
        ("pot_60", False),
        ("pot_90", False),
        ("default", False),
        ("insolvency", False),
        ("written_off", False),
    ],
)
def test_tier_gate_junior(bucket, junior_ok):
    assert tier_allows_bucket("junior", bucket) is junior_ok
    # Seniors work everything.
    assert tier_allows_bucket("senior", bucket) is True


def test_tier_gate_unknown_tier_raises():
    with pytest.raises(CollectionsError):
        tier_allows_bucket("intern", "pot_30")


def test_junior_allowed_buckets_is_the_shallow_ladder():
    assert JUNIOR_ALLOWED_BUCKETS == ("current", "current_month_late", "pot_30")


# ---------------------------------------------------------------------------
# Alpha-range matching (Dave's per-vendor-and-alpha queues)
# ---------------------------------------------------------------------------


def test_alpha_matches_basics():
    assert alpha_matches("Mertz", "A", "M") is True
    assert alpha_matches("mertz", "A", "M") is True  # case-insensitive
    assert alpha_matches("Nertz", "A", "M") is False
    assert alpha_matches("Olson", "N", "Z") is True
    assert alpha_matches("Anders", "A", "A") is True  # inclusive bounds
    assert alpha_matches("Zed", "A", "Z") is True


def test_alpha_matches_edge_names():
    assert alpha_matches(None, "A", "Z") is False
    assert alpha_matches("", "A", "Z") is False
    assert alpha_matches("  ", "A", "Z") is False
    assert alpha_matches("6-digit", "A", "Z") is False  # non-letter start


def test_alpha_bounds_validation():
    assert normalize_alpha(" m ") == "M"
    with pytest.raises(CollectionsError):
        normalize_alpha("AB")
    with pytest.raises(CollectionsError):
        normalize_alpha("")
    with pytest.raises(CollectionsError):
        normalize_alpha("1")
    with pytest.raises(CollectionsError):
        alpha_matches("Mertz", "Z", "A")  # inverted range


# ---------------------------------------------------------------------------
# Promise-to-pay evaluation (against actual payments)
# ---------------------------------------------------------------------------


class _Payment:
    def __init__(self, amount_cents, received):
        self.amount_cents = amount_cents
        self.received_at = received


WINDOW_START = date(2026, 7, 1)
PROMISED = date(2026, 7, 15)


def _eval(payments, today, amount=10_000):
    return evaluate_promise(amount, PROMISED, WINDOW_START, payments, today)


def test_promise_open_before_date_without_cover():
    result = _eval([_Payment(4_000, date(2026, 7, 10))], today=date(2026, 7, 12))
    assert result.status == PTP_OPEN
    assert result.covered_cents == 4_000


def test_promise_kept_when_covered_even_before_date():
    result = _eval(
        [_Payment(6_000, date(2026, 7, 5)), _Payment(4_000, date(2026, 7, 10))],
        today=date(2026, 7, 11),
    )
    assert result.status == PTP_KEPT
    assert result.covered_cents == 10_000


def test_promise_kept_on_exact_promised_date_payment():
    result = _eval([_Payment(10_000, PROMISED)], today=PROMISED)
    assert result.status == PTP_KEPT


def test_promise_still_open_on_promised_date_itself():
    # today == promised_date: the borrower still has the day.
    result = _eval([], today=PROMISED)
    assert result.status == PTP_OPEN


def test_promise_broken_day_after_without_cover():
    result = _eval([_Payment(9_999, date(2026, 7, 14))], today=PROMISED + timedelta(days=1))
    assert result.status == PTP_BROKEN
    assert result.covered_cents == 9_999


def test_promise_window_excludes_payments_before_creation_and_after_date():
    payments = [
        _Payment(10_000, WINDOW_START - timedelta(days=1)),  # before the promise
        _Payment(10_000, PROMISED + timedelta(days=1)),  # after the date
    ]
    result = _eval(payments, today=PROMISED + timedelta(days=2))
    assert result.status == PTP_BROKEN
    assert result.covered_cents == 0


def test_promise_accepts_datetime_received_at():
    payments = [_Payment(10_000, datetime(2026, 7, 10, 15, 30, tzinfo=timezone.utc))]
    result = _eval(payments, today=date(2026, 7, 11))
    assert result.status == PTP_KEPT


def test_promise_grace_days_extend_the_deadline():
    result = evaluate_promise(
        10_000, PROMISED, WINDOW_START, [], date(2026, 7, 16), grace_days=2
    )
    assert result.status == PTP_OPEN  # 7/16 is inside the 2-day grace
    result = evaluate_promise(
        10_000, PROMISED, WINDOW_START, [], date(2026, 7, 18), grace_days=2
    )
    assert result.status == PTP_BROKEN


# ---------------------------------------------------------------------------
# Queue sort key (Dave: principal first, then DPD, descending)
# ---------------------------------------------------------------------------


def test_insolvency_sort_key_orders_principal_then_dpd_desc():
    rows = [
        ("small_old", insolvency_sort_key(1_000, 900)),
        ("big_new", insolvency_sort_key(500_000, 10)),
        ("big_old", insolvency_sort_key(500_000, 400)),
    ]
    ordered = [name for name, _ in sorted(rows, key=lambda r: r[1])]
    assert ordered == ["big_old", "big_new", "small_old"]


# ---------------------------------------------------------------------------
# Fakes (test_delinquency_buckets idiom)
# ---------------------------------------------------------------------------


class _Item:
    def __init__(self, due, total=10_000, paid=0, status="scheduled"):
        self.due_date = due
        self.total_cents = total
        self.paid_cents = paid
        self.status = status


class _FakeLoan:
    """Attribute-compatible stand-in for PlatformLoan (test_loan_ledger
    idiom — plain object so fake schedule/payment rows can live in its
    relationship lists without ORM instrumentation)."""

    def __init__(
        self,
        schedule=(),
        status="active",
        insolvency=None,
        current_bucket="current",
        transactions=(),
        payments=(),
    ):
        self.id = uuid4()
        self.application_id = None  # no vendor lookup in DB-free tests
        self.principal_cents = 100_000
        self.principal_balance_cents = 100_000
        self.annual_rate_bps = 0
        self.term_months = 12
        self.status = status
        self.currency = "CAD"
        self.current_bucket = current_bucket
        self.insolvency_status = insolvency
        self.insolvency_marked_at = None
        self.disbursed_at = None
        self.schedule = list(schedule)
        self.payments = list(payments)
        self.transactions = list(transactions)


def _loan(**kwargs):
    return _FakeLoan(**kwargs)


class _ListQuery:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class _FakeSession:
    """Query stub keyed by model class; records adds/flushes/commits."""

    def __init__(self, rows_by_model=None):
        self.rows_by_model = rows_by_model or {}
        self.added = []
        self.flushes = 0
        self.commits = 0

    def query(self, model, *rest):
        return _ListQuery(self.rows_by_model.get(model, []))

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        self.flushes += 1

    def commit(self):
        self.commits += 1


# ---------------------------------------------------------------------------
# DPD / display-bucket helpers
# ---------------------------------------------------------------------------


def test_loan_dpd_and_past_due_helpers():
    loan = _loan(
        schedule=[
            _Item(TODAY - timedelta(days=40)),
            _Item(TODAY - timedelta(days=10)),
            _Item(TODAY + timedelta(days=20)),  # future — never counts
        ]
    )
    assert loan_has_past_due_unpaid(loan, TODAY) is True
    assert loan_days_past_due(loan, TODAY) == 40


def test_dpd_ignores_waived_suspended_and_paid_amounts():
    loan = _loan(
        schedule=[
            _Item(TODAY - timedelta(days=90), status="waived"),
            _Item(TODAY - timedelta(days=60), status="suspended"),
            _Item(TODAY - timedelta(days=30), paid=10_000),  # fully paid
        ]
    )
    assert loan_has_past_due_unpaid(loan, TODAY) is False
    assert loan_days_past_due(loan, TODAY) == 0


def test_display_bucket_overrides():
    # Insolvency overrides everything.
    loan = _loan(insolvency="bankruptcy", current_bucket="pot_60")
    assert display_bucket(loan, TODAY) == "insolvency"
    # Charged-off → written_off.
    loan = _loan(status="charged_off", current_bucket="pot_90")
    assert display_bucket(loan, TODAY) == "written_off"
    # Full catch-up cures to current immediately.
    loan = _loan(current_bucket="pot_30")  # no past-due unpaid installments
    assert display_bucket(loan, TODAY) == "current"
    # Otherwise the last snapshot bucket sticks.
    loan = _loan(
        schedule=[_Item(TODAY - timedelta(days=45))], current_bucket="pot_30"
    )
    assert display_bucket(loan, TODAY) == "pot_30"


# ---------------------------------------------------------------------------
# assign / unassign (fakes)
# ---------------------------------------------------------------------------


def _delinquent_loan(dpd=45, current_bucket="pot_30", **kw):
    return _loan(
        schedule=[_Item(TODAY - timedelta(days=dpd))],
        current_bucket=current_bucket,
        **kw,
    )


def test_assign_loan_happy_path_records_row_and_event():
    db = _FakeSession()
    loan = _delinquent_loan()
    collector = uuid4()
    assignment = assign_loan(
        db,
        loan,
        collector,
        "senior",
        method=ASSIGN_METHOD_MANUAL,
        actor_id="staff-1",
        today=TODAY,
    )
    assert assignment.collector_user_id == collector
    assert assignment.tier == "senior"
    assert assignment.active is True
    added_types = [type(x) for x in db.added]
    assert PlatformCollectorAssignment in added_types
    events = [x for x in db.added if isinstance(x, PlatformEvent)]
    assert [e.event_type for e in events] == [COLLECTOR_ASSIGNED_EVENT]
    assert events[0].payload["bucket"] == "pot_30"
    assert db.flushes == 1


def test_assign_loan_junior_blocked_on_deep_bucket():
    db = _FakeSession()
    loan = _delinquent_loan(dpd=75, current_bucket="pot_60")
    with pytest.raises(CollectionsError, match="junior"):
        assign_loan(
            db,
            loan,
            uuid4(),
            "junior",
            method=ASSIGN_METHOD_MANUAL,
            actor_id="staff-1",
            today=TODAY,
        )
    assert db.added == []  # nothing written


def test_assign_loan_junior_allowed_on_shallow_bucket():
    db = _FakeSession()
    loan = _delinquent_loan(dpd=10, current_bucket="current_month_late")
    assignment = assign_loan(
        db,
        loan,
        uuid4(),
        "junior",
        method=ASSIGN_METHOD_BULK_ALPHA,
        actor_id="staff-1",
        today=TODAY,
    )
    assert assignment.method == ASSIGN_METHOD_BULK_ALPHA


def test_assign_loan_blocks_double_assignment_unless_reassign():
    loan = _delinquent_loan()
    existing = PlatformCollectorAssignment(
        id=uuid4(),
        loan_id=loan.id,
        collector_user_id=uuid4(),
        tier="senior",
        method="manual",
        active=True,
        assigned_by="staff-0",
    )
    db = _FakeSession({PlatformCollectorAssignment: [existing]})
    with pytest.raises(CollectionsError, match="active collector assignment"):
        assign_loan(
            db, loan, uuid4(), "senior",
            method=ASSIGN_METHOD_MANUAL, actor_id="staff-1", today=TODAY,
        )
    # With reassign, the old row is deactivated (history kept) and replaced.
    new_collector = uuid4()
    assignment = assign_loan(
        db, loan, new_collector, "senior",
        method=ASSIGN_METHOD_MANUAL, actor_id="staff-1",
        reassign=True, today=TODAY,
    )
    assert existing.active is False
    assert existing.unassigned_by == "staff-1"
    assert assignment.collector_user_id == new_collector
    event = [x for x in db.added if isinstance(x, PlatformEvent)][-1]
    assert event.payload["replaced_assignment_id"] == str(existing.id)


def test_unassign_loan_deactivates_and_audits():
    loan = _delinquent_loan()
    existing = PlatformCollectorAssignment(
        id=uuid4(),
        loan_id=loan.id,
        collector_user_id=uuid4(),
        tier="junior",
        method="manual",
        active=True,
        assigned_by="staff-0",
    )
    db = _FakeSession({PlatformCollectorAssignment: [existing]})
    row = unassign_loan(db, loan, actor_id="staff-2", comment="vacation handover")
    assert row is existing
    assert existing.active is False
    events = [x for x in db.added if isinstance(x, PlatformEvent)]
    assert events[0].event_type == COLLECTOR_UNASSIGNED_EVENT
    assert events[0].payload["comment"] == "vacation handover"


def test_unassign_without_active_assignment_raises():
    db = _FakeSession()
    with pytest.raises(CollectionsError, match="no active"):
        unassign_loan(db, _delinquent_loan(), actor_id="staff-2")


# ---------------------------------------------------------------------------
# refresh_promise_statuses (fakes)
# ---------------------------------------------------------------------------


def _promise(loan, amount=10_000, promised=PROMISED, status=PTP_OPEN, created=None):
    return PlatformPromiseToPay(
        id=uuid4(),
        loan_id=loan.id,
        amount_cents=amount,
        promised_date=promised,
        comment="will pay",
        no_late_fees=False,
        status=status,
        created_by="staff-1",
        created_at=datetime.combine(
            created or WINDOW_START, datetime.min.time(), tzinfo=timezone.utc
        ),
    )


def test_refresh_marks_kept_from_ledger_payments():
    loan = _loan(payments=[_Payment(10_000, date(2026, 7, 10))])
    promise = _promise(loan)
    changed = refresh_promise_statuses(_FakeSession(), loan, [promise], today=date(2026, 7, 11))
    assert changed == [promise]
    assert promise.status == PTP_KEPT
    assert promise.covered_cents == 10_000
    assert promise.evaluated_at is not None


def test_refresh_marks_broken_after_date():
    loan = _loan(payments=[_Payment(1_000, date(2026, 7, 10))])
    promise = _promise(loan)
    changed = refresh_promise_statuses(
        _FakeSession(), loan, [promise], today=PROMISED + timedelta(days=1)
    )
    assert promise.status == PTP_BROKEN
    assert promise.covered_cents == 1_000
    assert changed == [promise]


def test_refresh_leaves_open_and_never_touches_terminal():
    loan = _loan(payments=[])
    open_promise = _promise(loan)
    kept = _promise(loan, status=PTP_KEPT)
    broken = _promise(loan, status=PTP_BROKEN)
    changed = refresh_promise_statuses(
        _FakeSession(), loan, [open_promise, kept, broken], today=date(2026, 7, 10)
    )
    assert changed == []
    assert open_promise.status == PTP_OPEN
    assert kept.status == PTP_KEPT
    assert broken.status == PTP_BROKEN


# ---------------------------------------------------------------------------
# Header math (identity over the actuals ledger)
# ---------------------------------------------------------------------------


def _txn(loan, seq, **kw):
    defaults = dict(
        id=uuid4(),
        loan_id=loan.id,
        seq=seq,
        reference=f"none-{loan.id}-{seq}",
        txn_type="payment",
        amount_cents=0,
        principal_cents=0,
        interest_cents=0,
        fees_cents=0,
        add_on_cents=0,
        effective_date=TODAY,
        processing_date=TODAY,
        created_by="test",
    )
    defaults.update(kw)
    return PlatformLoanTransaction(**defaults)


def test_header_identity_sums_to_payout():
    loan = _loan(schedule=[_Item(TODAY - timedelta(days=40))], current_bucket="pot_30")
    loan.annual_rate_bps = 1200  # 12% — interest accrues daily from disbursement
    loan.disbursed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    loan.transactions = [
        _txn(loan, 1, txn_type="fee", amount_cents=4_500, add_on_cents=4_500),
        # Accrued interest at 2026-03-01 (59 days @ 12% on 100k) ≈ 1,940c —
        # keep interest_paid below it so the engine doesn't re-route the
        # surplus to principal.
        _txn(
            loan,
            2,
            txn_type="payment",
            amount_cents=16_000,
            principal_cents=15_000,
            interest_cents=1_000,
            effective_date=date(2026, 3, 1),
        ),
    ]
    header = header_view(loan, TODAY, today=TODAY)
    assert (
        header["outstanding_principal_cents"]
        + header["interest_due_cents"]
        + header["fees_due_cents"]
        + header["add_on_balance_cents"]
        == header["total_payout_cents"]
    )
    assert header["add_on_balance_cents"] == 4_500
    assert header["outstanding_principal_cents"] == 85_000
    assert header["interest_due_cents"] > 0  # daily accrual is live
    assert header["days_past_due"] == 40
    assert header["bucket"] == "pot_30"


def test_header_zero_rate_no_ledger_is_pure_principal():
    loan = _loan()
    header = header_view(loan, TODAY, today=TODAY)
    assert header["total_payout_cents"] == 100_000
    assert header["interest_due_cents"] == 0
    assert header["fees_due_cents"] == 0
    assert header["add_on_balance_cents"] == 0
    assert header["bucket"] == "current"


# ---------------------------------------------------------------------------
# Insolvency maintenance fee (idempotent, add-on bucket)
# ---------------------------------------------------------------------------


def test_maintenance_fee_charges_add_on_bucket_once():
    loan = _loan(insolvency="bankruptcy", current_bucket="insolvency")
    db = _FakeSession()
    txn = apply_maintenance_fee(
        db, loan, 2_500, fee_month=date(2026, 7, 1), actor_id="admin-1", today=TODAY
    )
    assert txn is not None
    assert txn.txn_type == "fee"
    assert txn.add_on_cents == 2_500
    assert txn.fees_cents == 0  # triggered fees live under ADD-ON, never loan fees
    assert txn.principal_cents == 0 and txn.interest_cents == 0
    assert txn in loan.transactions  # in-session ledger view stays consistent
    markers = [x for x in db.added if isinstance(x, PlatformInsolvencyMaintenanceFee)]
    assert len(markers) == 1
    assert markers[0].fee_month == date(2026, 7, 1)
    assert markers[0].transaction_id == txn.id
    events = [x for x in db.added if isinstance(x, PlatformEvent)]
    assert [e.event_type for e in events] == [MAINTENANCE_FEE_EVENT]
    # The header now reflects the fee through the actuals engine.
    header = header_view(loan, TODAY, today=TODAY)
    assert header["add_on_balance_cents"] == 2_500


def test_maintenance_fee_is_idempotent_per_month():
    loan = _loan(insolvency="consumer_proposal", current_bucket="insolvency")
    marker = PlatformInsolvencyMaintenanceFee(
        id=uuid4(),
        loan_id=loan.id,
        fee_month=date(2026, 7, 1),
        amount_cents=2_500,
        transaction_id=uuid4(),
        created_by="admin-1",
    )
    db = _FakeSession({PlatformInsolvencyMaintenanceFee: [marker]})
    txn = apply_maintenance_fee(
        db, loan, 2_500, fee_month=date(2026, 7, 15), actor_id="admin-1", today=TODAY
    )
    assert txn is None  # July already charged (any date in the month normalizes)
    assert db.added == []


def test_maintenance_fee_requires_insolvency_and_positive_amount():
    db = _FakeSession()
    with pytest.raises(CollectionsError, match="insolvency"):
        apply_maintenance_fee(
            db, _loan(), 2_500, fee_month=date(2026, 7, 1), actor_id="a", today=TODAY
        )
    with pytest.raises(CollectionsError, match="positive"):
        apply_maintenance_fee(
            db,
            _loan(insolvency="bankruptcy"),
            0,
            fee_month=date(2026, 7, 1),
            actor_id="a",
            today=TODAY,
        )
    assert db.added == []


def test_maintenance_fee_sequences_after_existing_ledger_rows():
    loan = _loan(insolvency="credit_counseling", current_bucket="insolvency")
    loan.transactions = [
        _txn(loan, 1, txn_type="fee", amount_cents=4_500, add_on_cents=4_500)
    ]
    db = _FakeSession()
    txn = apply_maintenance_fee(
        db, loan, 2_500, fee_month=date(2026, 7, 1), actor_id="admin-1", today=TODAY
    )
    assert txn.seq == 2
    assert txn.reference.endswith("-2")
