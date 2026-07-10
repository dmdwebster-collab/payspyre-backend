"""Unit tests for Dave's month-end delinquency bucket state machine (WS-H).

DELIBERATELY DB-free (fan-out protocol: the suite shares a remote DB and must
not be run wholesale by agents):

  * the derivation (``bucket_for``), the as-of replay, the cure display rule,
    the roll-rate math and the month arithmetic are PURE functions — tested
    directly, boundary-by-boundary;
  * ``run_bucket_snapshot`` / ``set_insolvency`` are exercised with in-memory
    fakes (simple objects + a fake Session), same idiom as
    tests/test_loan_servicing.py.

Run JUST this file:

    source .venv/bin/activate && \
        python -m pytest tests/test_delinquency_buckets.py -p no:warnings -q
"""
import importlib.util
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.platform.event import PlatformEvent
from app.models.platform.loan import PlatformLoan, PlatformLoanDelinquencySnapshot
from app.services.delinquency_buckets import (
    BUCKET_CURRENT,
    BUCKET_CURRENT_MONTH_LATE,
    BUCKET_DEFAULT,
    BUCKET_INSOLVENCY,
    BUCKET_POT_30,
    BUCKET_POT_60,
    BUCKET_POT_90,
    BUCKET_WRITTEN_OFF,
    LOAN_INSOLVENCY_CLEARED_EVENT,
    LOAN_INSOLVENCY_MARKED_EVENT,
    InstallmentState,
    LoanBucketInputs,
    bucket_for,
    compute_roll_rates,
    default_snapshot_month,
    effective_bucket,
    installment_states_as_of,
    month_end_of,
    run_bucket_snapshot,
    set_insolvency,
)

MONTH_END = date(2026, 7, 31)


def _inputs(installments, loan_status="active", insolvency=None):
    return LoanBucketInputs(
        loan_status=loan_status,
        insolvency_status=insolvency,
        installments=tuple(installments),
    )


def _unpaid(dpd: int, total=10_000, paid=0, status="scheduled"):
    """One unpaid installment exactly ``dpd`` days past due at MONTH_END."""
    return InstallmentState(
        due_date=MONTH_END - timedelta(days=dpd),
        total_cents=total,
        paid_cents=paid,
        status=status,
    )


# ---------------------------------------------------------------------------
# bucket_for — DPD boundary table (the locked thresholds)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dpd,expected",
    [
        (0, BUCKET_CURRENT),  # due exactly at month-end: not yet late
        (1, BUCKET_CURRENT_MONTH_LATE),
        (29, BUCKET_CURRENT_MONTH_LATE),
        (30, BUCKET_POT_30),
        (31, BUCKET_POT_30),
        (59, BUCKET_POT_30),
        (60, BUCKET_POT_60),
        (89, BUCKET_POT_60),
        (90, BUCKET_POT_90),
        (119, BUCKET_POT_90),
        (120, BUCKET_POT_90),  # ">120 → default": 120 itself is still pot_90
        (121, BUCKET_DEFAULT),
        (400, BUCKET_DEFAULT),
    ],
)
def test_dpd_boundaries(dpd, expected):
    result = bucket_for(_inputs([_unpaid(dpd)]), MONTH_END)
    assert result.bucket == expected
    assert result.days_past_due == dpd


def test_no_installments_is_current():
    result = bucket_for(_inputs([]), MONTH_END)
    assert result.bucket == BUCKET_CURRENT
    assert result.days_past_due == 0
    assert result.amount_past_due_cents == 0


def test_bucket_from_oldest_unpaid_and_amount_sums_all_past_due():
    result = bucket_for(_inputs([_unpaid(65), _unpaid(35), _unpaid(4)]), MONTH_END)
    assert result.bucket == BUCKET_POT_60  # oldest unpaid drives the bucket
    assert result.days_past_due == 65
    assert result.amount_past_due_cents == 30_000


def test_future_installments_do_not_count():
    future = InstallmentState(
        due_date=MONTH_END + timedelta(days=15), total_cents=10_000, paid_cents=0
    )
    result = bucket_for(_inputs([future]), MONTH_END)
    assert result.bucket == BUCKET_CURRENT


# ---------------------------------------------------------------------------
# Cure semantics (documented assumptions 2 + 3)
# ---------------------------------------------------------------------------


def test_full_catchup_cures_to_current():
    paid = _unpaid(75, total=10_000, paid=10_000)
    result = bucket_for(_inputs([paid], loan_status="delinquent"), MONTH_END)
    assert result.bucket == BUCKET_CURRENT
    assert result.amount_past_due_cents == 0


def test_partial_catchup_does_not_cure():
    partially_paid = _unpaid(65, total=10_000, paid=9_999)
    result = bucket_for(_inputs([partially_paid]), MONTH_END)
    assert result.bucket == BUCKET_POT_60  # 1 cent still past due at 65 DPD
    assert result.amount_past_due_cents == 1


def test_paying_off_oldest_moves_bucket_up_the_ladder():
    # Oldest (95 DPD) fully paid; next (35 DPD) unpaid → pot_30, not pot_90.
    result = bucket_for(
        _inputs([_unpaid(95, paid=10_000), _unpaid(35)]), MONTH_END
    )
    assert result.bucket == BUCKET_POT_30
    assert result.days_past_due == 35


# ---------------------------------------------------------------------------
# Overrides: insolvency > written_off > DPD ladder
# ---------------------------------------------------------------------------


def test_insolvency_overrides_dpd():
    result = bucket_for(
        _inputs([_unpaid(200)], insolvency="consumer_proposal"), MONTH_END
    )
    assert result.bucket == BUCKET_INSOLVENCY
    assert result.days_past_due == 200  # informational DPD still computed
    assert result.bureau_reportable is False


@pytest.mark.parametrize("status", ["consumer_proposal", "bankruptcy", "credit_counseling"])
def test_all_insolvency_statuses_classify_as_insolvency(status):
    assert bucket_for(_inputs([], insolvency=status), MONTH_END).bucket == BUCKET_INSOLVENCY


def test_insolvency_overrides_written_off():
    # The segregated insolvency portfolio: written off the main book but
    # still maintained — insolvency wins over charged_off.
    result = bucket_for(
        _inputs([], loan_status="charged_off", insolvency="bankruptcy"), MONTH_END
    )
    assert result.bucket == BUCKET_INSOLVENCY


def test_charged_off_is_written_off():
    result = bucket_for(_inputs([_unpaid(45)], loan_status="charged_off"), MONTH_END)
    assert result.bucket == BUCKET_WRITTEN_OFF
    assert result.bureau_reportable is False


# ---------------------------------------------------------------------------
# Bureau-reportable flag (pot_60+; FLAG only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dpd,reportable",
    [(0, False), (15, False), (45, False), (60, True), (95, True), (150, True)],
)
def test_bureau_reportable_flag(dpd, reportable):
    installments = [_unpaid(dpd)] if dpd else []
    assert bucket_for(_inputs(installments), MONTH_END).bureau_reportable is reportable


# ---------------------------------------------------------------------------
# Suspended / waived exclusion (WS-F guard, assumption 7)
# ---------------------------------------------------------------------------


def test_suspended_installments_are_skipped():
    result = bucket_for(_inputs([_unpaid(80, status="suspended")]), MONTH_END)
    assert result.bucket == BUCKET_CURRENT
    assert result.amount_past_due_cents == 0


def test_waived_installments_are_skipped():
    result = bucket_for(_inputs([_unpaid(80, status="waived")]), MONTH_END)
    assert result.bucket == BUCKET_CURRENT


# ---------------------------------------------------------------------------
# As-of replay (deterministic re-runs)
# ---------------------------------------------------------------------------


def _sched(n, due, total=10_000, status="scheduled"):
    return SimpleNamespace(
        installment_number=n, due_date=due, total_cents=total, status=status
    )


def _pay(amount, on: date):
    return SimpleNamespace(
        amount_cents=amount,
        received_at=datetime(on.year, on.month, on.day, 12, tzinfo=timezone.utc),
        created_at=datetime(on.year, on.month, on.day, 12, tzinfo=timezone.utc),
    )


def test_replay_excludes_payments_after_as_of():
    schedule = [_sched(1, date(2026, 6, 15)), _sched(2, date(2026, 7, 15))]
    payments = [_pay(10_000, date(2026, 8, 5))]  # lands AFTER July month-end
    states = installment_states_as_of(schedule, payments, date(2026, 7, 31))
    assert states[0].paid_cents == 0  # August's payment must not rewrite July
    assert states[1].paid_cents == 0

    # …but it does count for August's month-end.
    states_aug = installment_states_as_of(schedule, payments, date(2026, 8, 31))
    assert states_aug[0].paid_cents == 10_000


def test_replay_fills_oldest_installment_first():
    schedule = [_sched(1, date(2026, 5, 15)), _sched(2, date(2026, 6, 15))]
    payments = [_pay(15_000, date(2026, 6, 20))]
    states = installment_states_as_of(schedule, payments, date(2026, 7, 31))
    assert states[0].paid_cents == 10_000
    assert states[1].paid_cents == 5_000


def test_replay_skips_suspended_and_waived_rows():
    schedule = [
        _sched(1, date(2026, 5, 15), status="suspended"),
        _sched(2, date(2026, 6, 15), status="waived"),
        _sched(3, date(2026, 7, 15)),
    ]
    payments = [_pay(10_000, date(2026, 7, 20))]
    states = installment_states_as_of(schedule, payments, date(2026, 7, 31))
    assert states[0].paid_cents == 0
    assert states[1].paid_cents == 0
    assert states[2].paid_cents == 10_000


# ---------------------------------------------------------------------------
# effective_bucket (intra-month display rule, assumptions 1 + 2)
# ---------------------------------------------------------------------------


def test_effective_bucket_keeps_last_snapshot_while_past_due():
    assert effective_bucket("delinquent", None, BUCKET_POT_30, True) == BUCKET_POT_30


def test_effective_bucket_full_catchup_cures_immediately():
    assert effective_bucket("active", None, BUCKET_POT_60, False) == BUCKET_CURRENT


def test_effective_bucket_insolvency_and_writeoff_override_immediately():
    assert effective_bucket("active", "bankruptcy", BUCKET_POT_30, True) == BUCKET_INSOLVENCY
    assert effective_bucket("charged_off", None, BUCKET_POT_90, True) == BUCKET_WRITTEN_OFF


def test_effective_bucket_never_snapshotted_loan_defaults_current():
    assert effective_bucket("active", None, None, False) == BUCKET_CURRENT


# ---------------------------------------------------------------------------
# Debt-roll rates (Dave's must-have report number)
# ---------------------------------------------------------------------------


def test_roll_rate_cml_to_pot30():
    prior = {1: BUCKET_CURRENT_MONTH_LATE, 2: BUCKET_CURRENT_MONTH_LATE,
             3: BUCKET_CURRENT_MONTH_LATE, 4: BUCKET_CURRENT_MONTH_LATE,
             5: BUCKET_POT_30}
    current = {1: BUCKET_POT_30, 2: BUCKET_POT_30, 3: BUCKET_CURRENT,
               4: BUCKET_CURRENT_MONTH_LATE, 5: BUCKET_POT_60}
    rates = {(r["from_bucket"], r["to_bucket"]): r for r in compute_roll_rates(prior, current)}
    cml = rates[(BUCKET_CURRENT_MONTH_LATE, BUCKET_POT_30)]
    assert cml["base_count"] == 4
    assert cml["rolled_count"] == 2
    assert cml["roll_rate_bps"] == 5_000  # 50%
    pot30 = rates[(BUCKET_POT_30, BUCKET_POT_60)]
    assert pot30["base_count"] == 1 and pot30["rolled_count"] == 1
    assert pot30["roll_rate_bps"] == 10_000


def test_roll_rate_empty_base_is_zero_not_division_error():
    rates = compute_roll_rates({}, {})
    assert all(r["roll_rate_bps"] == 0 and r["base_count"] == 0 for r in rates)


# ---------------------------------------------------------------------------
# Month arithmetic
# ---------------------------------------------------------------------------


def test_month_end_of():
    assert month_end_of(date(2026, 7, 1)) == date(2026, 7, 31)
    assert month_end_of(date(2026, 7, 20)) == date(2026, 7, 31)
    assert month_end_of(date(2026, 2, 1)) == date(2026, 2, 28)  # not a leap year
    assert month_end_of(date(2028, 2, 1)) == date(2028, 2, 29)  # leap year
    assert month_end_of(date(2026, 12, 5)) == date(2026, 12, 31)


def test_default_snapshot_month():
    # Run ON a month's last day → that month (the cron's intended slot).
    assert default_snapshot_month(date(2026, 7, 31)) == date(2026, 7, 1)
    # First of the following month → the just-ended month.
    assert default_snapshot_month(date(2026, 8, 1)) == date(2026, 7, 1)
    # Mid-month re-run → the most recently completed month.
    assert default_snapshot_month(date(2026, 8, 15)) == date(2026, 7, 1)
    # Year rollover.
    assert default_snapshot_month(date(2027, 1, 1)) == date(2026, 12, 1)


# ---------------------------------------------------------------------------
# In-memory fakes for the orchestration paths (no DB)
# ---------------------------------------------------------------------------


class _ListQuery:
    """Query stub: ignores filter conditions, serves a fixed list."""

    def __init__(self, items):
        self._items = list(items)

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._items

    def first(self):
        return self._items[0] if self._items else None


class _FakeSession:
    def __init__(self, loans=(), snapshots=()):
        self.loans = list(loans)
        self.snapshots = list(snapshots)
        self.added = []
        self.commits = 0

    def query(self, model, *rest):
        if model is PlatformLoan:
            return _ListQuery(self.loans)
        if model is PlatformLoanDelinquencySnapshot:
            return _ListQuery(self.snapshots)
        return _ListQuery([])

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commits += 1


class _Item:
    def __init__(self, n, due, total=10_000, paid=0, status="scheduled"):
        self.installment_number = n
        self.due_date = due
        self.total_cents = total
        self.paid_cents = paid
        self.status = status


class _Loan:
    def __init__(self, schedule, status="active", insolvency=None):
        self.id = "loan-1"
        self.application_id = None
        self.status = status
        self.schedule = schedule
        self.payments = []
        self.transactions = []
        self.principal_cents = 100_000
        self.principal_balance_cents = 100_000
        self.annual_rate_bps = 0
        self.disbursed_at = None
        self.current_bucket = "current"
        self.insolvency_status = insolvency
        self.insolvency_marked_at = None
        self.insolvency_marked_by = None


def test_run_bucket_snapshot_creates_row_and_assigns_bucket():
    loan = _Loan([_Item(1, date(2026, 6, 15))])  # 46 DPD at July month-end
    db = _FakeSession(loans=[loan])
    result = run_bucket_snapshot(db, date(2026, 7, 1), today=date(2026, 8, 1))

    assert result.snapshot_month == date(2026, 7, 1)
    assert result.month_end == date(2026, 7, 31)
    assert result.loans_snapshotted == 1
    assert result.bucket_counts == {BUCKET_POT_30: 1}
    assert db.commits == 1

    snaps = [o for o in db.added if isinstance(o, PlatformLoanDelinquencySnapshot)]
    assert len(snaps) == 1
    snap = snaps[0]
    assert snap.bucket == BUCKET_POT_30
    assert snap.days_past_due == 46
    assert snap.amount_past_due_cents == 10_000
    assert snap.outstanding_principal_cents == 100_000  # ledger view (no disbursement/rate)
    assert snap.bureau_reportable is False
    # The latest month's snapshot assigns the loan's current bucket.
    assert loan.current_bucket == BUCKET_POT_30


def test_run_bucket_snapshot_is_idempotent_updates_existing_row():
    loan = _Loan([_Item(1, date(2026, 6, 15))])
    existing = PlatformLoanDelinquencySnapshot(
        loan_id=loan.id,
        snapshot_month=date(2026, 7, 1),
        bucket="current",
        days_past_due=0,
        amount_past_due_cents=0,
        outstanding_principal_cents=0,
        bureau_reportable=False,
    )
    db = _FakeSession(loans=[loan], snapshots=[existing])
    run_bucket_snapshot(db, date(2026, 7, 1), today=date(2026, 8, 1))

    # No NEW snapshot row — the existing one was updated in place.
    assert not any(isinstance(o, PlatformLoanDelinquencySnapshot) for o in db.added)
    assert existing.bucket == BUCKET_POT_30
    assert existing.days_past_due == 46
    assert existing.amount_past_due_cents == 10_000


def test_run_bucket_snapshot_replay_ignores_payments_after_month_end():
    loan = _Loan([_Item(1, date(2026, 6, 15), paid=10_000)])
    # The installment shows PAID today, but the cash arrived in August —
    # July's snapshot must still see it unpaid (deterministic re-run).
    loan.payments = [
        SimpleNamespace(
            amount_cents=10_000,
            received_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
            created_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
        )
    ]
    db = _FakeSession(loans=[loan])
    result = run_bucket_snapshot(db, date(2026, 7, 1), today=date(2026, 9, 2))
    assert result.bucket_counts == {BUCKET_POT_30: 1}
    # NOT the latest month (Aug is) → current_bucket untouched by the re-run.
    assert loan.current_bucket == "current"


def test_run_bucket_snapshot_refuses_incomplete_month():
    db = _FakeSession(loans=[])
    with pytest.raises(ValueError):
        run_bucket_snapshot(db, date(2026, 7, 1), today=date(2026, 7, 15))


def test_run_bucket_snapshot_insolvency_and_chargeoff_override():
    insolvent = _Loan([_Item(1, date(2026, 2, 1))], insolvency="consumer_proposal")
    charged = _Loan([_Item(1, date(2026, 2, 1))], status="charged_off")
    charged.id = "loan-2"
    db = _FakeSession(loans=[insolvent, charged])
    result = run_bucket_snapshot(db, date(2026, 7, 1), today=date(2026, 8, 1))
    assert result.bucket_counts == {BUCKET_INSOLVENCY: 1, BUCKET_WRITTEN_OFF: 1}
    assert insolvent.current_bucket == BUCKET_INSOLVENCY
    assert charged.current_bucket == BUCKET_WRITTEN_OFF


# ---------------------------------------------------------------------------
# set_insolvency (manual, audited)
# ---------------------------------------------------------------------------


def _events(db):
    return [o for o in db.added if isinstance(o, PlatformEvent)]


def test_set_insolvency_marks_and_audits():
    loan = _Loan([_Item(1, date(2026, 6, 15))])
    db = _FakeSession()
    set_insolvency(db, loan, "bankruptcy", "Trustee letter received 2026-07-08", "staff-9")

    assert loan.insolvency_status == "bankruptcy"
    assert loan.insolvency_marked_by == "staff-9"
    assert loan.insolvency_marked_at is not None
    assert loan.current_bucket == BUCKET_INSOLVENCY

    events = _events(db)
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == LOAN_INSOLVENCY_MARKED_EVENT
    assert ev.actor == "staff-9"
    assert ev.payload["after"]["insolvency_status"] == "bankruptcy"
    assert ev.payload["comment"] == "Trustee letter received 2026-07-08"
    # Caller owns the commit (endpoint idiom).
    assert db.commits == 0


def test_set_insolvency_clear_rederives_bucket_from_live_state():
    loan = _Loan([_Item(1, date.today() - timedelta(days=45))], insolvency="bankruptcy")
    loan.current_bucket = BUCKET_INSOLVENCY
    loan.insolvency_marked_at = datetime.now(timezone.utc)
    loan.insolvency_marked_by = "staff-9"
    db = _FakeSession()
    set_insolvency(db, loan, "none", "Proposal annulled; regular collections resume", "admin-1")

    assert loan.insolvency_status is None
    assert loan.insolvency_marked_at is None
    assert loan.insolvency_marked_by is None
    assert loan.current_bucket == BUCKET_POT_30  # 45 DPD live → pot_30

    events = _events(db)
    assert events[0].event_type == LOAN_INSOLVENCY_CLEARED_EVENT
    assert events[0].payload["before"]["insolvency_status"] == "bankruptcy"


def test_set_insolvency_clear_on_caught_up_loan_is_current():
    loan = _Loan([_Item(1, date.today() - timedelta(days=45), paid=10_000)],
                 insolvency="credit_counseling")
    db = _FakeSession()
    set_insolvency(db, loan, "none", "Completed counseling program", "admin-1")
    assert loan.current_bucket == BUCKET_CURRENT


def test_set_insolvency_requires_comment_and_valid_status():
    loan = _Loan([])
    db = _FakeSession()
    with pytest.raises(ValueError):
        set_insolvency(db, loan, "bankruptcy", "   ", "staff-9")
    with pytest.raises(ValueError):
        set_insolvency(db, loan, "solvent_again", "comment", "staff-9")
    assert _events(db) == []


# --- migration chain pin (merge-train convention) ---------------------------


def test_migration_chain():
    """Pin 053_delinquency_buckets' place in the migration chain — the merge
    train re-chains down_revisions, and a silent fork would split the alembic
    head."""
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "053_delinquency_buckets.py"
    )
    spec = importlib.util.spec_from_file_location("migration_053", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "053_delinquency_buckets"
    assert mod.down_revision == "049_loan_ledger"
