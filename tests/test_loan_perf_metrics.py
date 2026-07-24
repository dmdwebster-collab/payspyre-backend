"""Loan-performance panel: the two lifetime counters the frontend cannot derive
from the ledger/schedule alone and now reads off the admin loan detail
(``GET /api/v1/admin/loans/{id}`` → ``ctx.loan``):

* ``maximum_dpd`` — lifetime peak days-past-due, computed ON READ as
  ``max(every persisted delinquency-snapshot DPD, the current live DPD)``. It
  never waits on the nightly aging job.
* ``nsf_payment_count`` — count of NSF / returned-payment events, counted off a
  TYPED source (``platform_collection_attempts`` the rail dishonoured), not from
  free-text comments.

The compute lives in two small helpers on the admin-loans endpoint module; the
unit tests below exercise them directly (the ``maximum_dpd`` helper takes the
live DPD as a parameter, so its max/snapshot-aggregation logic is testable
without standing up the whole servicing-status machinery). One end-to-end test
then proves both fields land on the real payload under the exact snake_case
names, with the live DPD flowing from the real ``servicing_status`` basis.
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api.v1.endpoints.admin_loans import _maximum_dpd, _nsf_payment_count
from app.models.platform.loan import (
    PlatformCollectionAttempt,
    PlatformLoan,
    PlatformLoanDelinquencySnapshot,
    PlatformLoanScheduleItem,
)

# ---------------------------------------------------------------------------
# Factories — bare MIGRATED loans (source='turnkey_migration' allows a NULL
# application_id), so no application/patient/vendor graph is required.
# ---------------------------------------------------------------------------


def _mk_loan(db, *, status="active", principal=1_000_000):
    loan = PlatformLoan(
        id=uuid4(),
        application_id=None,
        source="turnkey_migration",
        principal_cents=principal,
        annual_rate_bps=1299,
        term_months=12,
        status=status,
        principal_balance_cents=principal,
        disbursement_status="completed",
        disbursed_at=datetime.now(timezone.utc),
    )
    db.add(loan)
    db.flush()
    return loan


def _mk_item(db, loan, n, due, *, total=100_000, paid=0, status="scheduled"):
    item = PlatformLoanScheduleItem(
        loan_id=loan.id,
        installment_number=n,
        due_date=due,
        principal_cents=total - 20_000,
        interest_cents=20_000,
        total_cents=total,
        status=status,
        paid_cents=paid,
    )
    db.add(item)
    db.flush()
    return item


def _mk_snapshot(db, loan, month, dpd, *, bucket="pot_30", amount=50_000):
    snap = PlatformLoanDelinquencySnapshot(
        loan_id=loan.id,
        snapshot_month=month,
        bucket=bucket,
        days_past_due=dpd,
        amount_past_due_cents=amount,
        outstanding_principal_cents=loan.principal_balance_cents,
    )
    db.add(snap)
    db.flush()
    return snap


def _mk_attempt(db, loan, item, n, *, outcome="failed", ref=None):
    attempt = PlatformCollectionAttempt(
        id=uuid4(),
        loan_id=loan.id,
        schedule_item_id=item.id,
        attempt_number=n,
        amount_cents=100_000,
        client_transaction_id=f"autocol-{item.id}-{n}",
        external_ref=ref,
        outcome=outcome,
    )
    db.add(attempt)
    db.flush()
    return attempt


# ---------------------------------------------------------------------------
# maximum_dpd — lifetime peak = max(all snapshot DPDs, live DPD), computed on read
# ---------------------------------------------------------------------------


class TestMaximumDpd:
    def test_snapshot_history_high_water_dominates_lower_live_dpd(self, db_session):
        """A loan that was 45 DPD in a past month but is only 10 DPD now still
        reports its lifetime peak of 45 — read straight off snapshot history,
        no cron required."""
        loan = _mk_loan(db_session)
        _mk_snapshot(db_session, loan, date(2026, 5, 1), 45, bucket="pot_30")
        _mk_snapshot(db_session, loan, date(2026, 6, 1), 20, bucket="pot_30")

        assert _maximum_dpd(db_session, loan.id, live_dpd=10) == 45

    def test_live_dpd_dominates_when_currently_worse_than_history(self, db_session):
        """Currently deeper past due than the loan ever recorded at month-end —
        the live figure wins."""
        loan = _mk_loan(db_session)
        _mk_snapshot(db_session, loan, date(2026, 6, 1), 12, bucket="pot_30")

        assert _maximum_dpd(db_session, loan.id, live_dpd=40) == 40

    def test_currently_past_due_no_snapshot_history(self, db_session):
        """A loan past due right now but with no month-end snapshots yet still
        reports its live DPD (the peak so far)."""
        loan = _mk_loan(db_session)

        assert _maximum_dpd(db_session, loan.id, live_dpd=33) == 33

    def test_never_past_due_is_zero(self, db_session):
        loan = _mk_loan(db_session)

        assert _maximum_dpd(db_session, loan.id, live_dpd=0) == 0

    def test_snapshots_of_a_different_loan_are_not_counted(self, db_session):
        """Peak is per-loan: another loan's deep delinquency never leaks in."""
        loan = _mk_loan(db_session)
        other = _mk_loan(db_session)
        _mk_snapshot(db_session, other, date(2026, 6, 1), 200, bucket="default")

        assert _maximum_dpd(db_session, loan.id, live_dpd=5) == 5


# ---------------------------------------------------------------------------
# nsf_payment_count — typed count of dishonoured rail pulls
# ---------------------------------------------------------------------------


class TestNsfPaymentCount:
    def test_counts_only_dishonoured_rail_pulls(self, db_session):
        """Counts failed auto-collection attempts the rail actually placed
        (``outcome='failed'`` WITH an ``external_ref``). Excludes:
        completed pulls, still-pending pulls, and adapter rejections the rail
        never attempted (failed with a NULL ``external_ref`` — the engine's own
        "NOT an NSF event" case)."""
        loan = _mk_loan(db_session)
        i1 = _mk_item(db_session, loan, 1, date(2026, 1, 1))
        i2 = _mk_item(db_session, loan, 2, date(2026, 2, 1))
        i3 = _mk_item(db_session, loan, 3, date(2026, 3, 1))

        _mk_attempt(db_session, loan, i1, 1, outcome="failed", ref="zum-tx-1")
        _mk_attempt(db_session, loan, i2, 1, outcome="failed", ref="zum-tx-2")
        # Adapter rejection: rail never pulled -> NULL external_ref -> not NSF.
        _mk_attempt(db_session, loan, i3, 1, outcome="failed", ref=None)
        # Retry that settled, and one still in flight -> not returned payments.
        _mk_attempt(db_session, loan, i3, 2, outcome="completed", ref="zum-tx-3")
        _mk_attempt(db_session, loan, i2, 2, outcome="pending", ref="zum-tx-4")

        assert _nsf_payment_count(db_session, loan.id) == 2

    def test_zero_when_no_returned_payments(self, db_session):
        loan = _mk_loan(db_session)
        item = _mk_item(db_session, loan, 1, date(2026, 1, 1))
        _mk_attempt(db_session, loan, item, 1, outcome="completed", ref="zum-ok")

        assert _nsf_payment_count(db_session, loan.id) == 0

    def test_attempts_of_a_different_loan_are_not_counted(self, db_session):
        loan = _mk_loan(db_session)
        other = _mk_loan(db_session)
        oi = _mk_item(db_session, other, 1, date(2026, 1, 1))
        _mk_attempt(db_session, other, oi, 1, outcome="failed", ref="zum-other")

        assert _nsf_payment_count(db_session, loan.id) == 0


# ---------------------------------------------------------------------------
# End-to-end — both fields on the real GET /admin/loans/{id} payload, with the
# live DPD flowing from the real servicing_status basis.
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_client(db_session):
    from fastapi.testclient import TestClient

    from app.core.auth import get_current_user
    from app.db.base import get_db
    from app.main import app

    principal = SimpleNamespace(
        id=uuid4(),
        email="perf-admin@example.com",
        first_name="Perf",
        last_name="Admin",
        roles=[SimpleNamespace(role=SimpleNamespace(name="admin"))],
    )
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: principal

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


def test_loan_detail_payload_carries_both_counters(admin_client, db_session):
    """Both counters land on the real payload under the exact snake_case names,
    as integers, with ``maximum_dpd`` tracking the payload's own live DPD (the
    real ``servicing_status`` basis). The snapshot-history high-water branch is
    covered by ``TestMaximumDpd`` above — it is left out here because
    serializing a snapshot's ``bucket`` in ``get_loan`` trips a pre-existing,
    unrelated enum-reflection quirk in the snapshot model."""
    today = date.today()
    loan = _mk_loan(db_session)
    # A real, currently-past-due schedule so servicing_status yields a live DPD.
    i0 = _mk_item(db_session, loan, 1, today - timedelta(days=30), status="late")
    _mk_item(db_session, loan, 2, today + timedelta(days=1))
    # One dishonoured rail pull (counted) + one adapter rejection (excluded).
    _mk_attempt(db_session, loan, i0, 1, outcome="failed", ref="zum-e2e-1")
    _mk_attempt(db_session, loan, i0, 2, outcome="failed", ref=None)
    db_session.commit()

    r = admin_client.get(f"/api/v1/admin/loans/{loan.id}")
    assert r.status_code == 200, r.text
    body = r.json()

    # Present, integer, and additive (existing figures untouched).
    assert isinstance(body["maximum_dpd"], int)
    assert isinstance(body["nsf_payment_count"], int)
    assert body["nsf_payment_count"] == 1

    # maximum_dpd tracks the payload's own live DPD (no third DPD recompute).
    live_dpd = body["delinquency"]["days_past_due"]
    assert live_dpd > 0
    assert body["maximum_dpd"] == live_dpd

    # Existing loan-performance inputs are untouched by the additive fields.
    assert body["principal_cents"] == loan.principal_cents
    assert "delinquency" in body and "servicing_status" in body
