"""Vendor self-serve disbursements engine (W2-DISB) — DB-free unit tests.

Everything here runs against fakes / transient model instances: the pure wallet
math (share, availability, extra-payout planning), the business-day holdback
walk, the claim-before-push execution against a fake Zumrails adapter, and the
money-out feature-flag gate. The DB-backed seams (ledger reads / history /
webhook) are exercised by CI's full suite via the shared entry points.
"""
from datetime import date, datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.models.platform.event import PlatformEvent
from app.models.platform.vendor_disbursement import PlatformVendorDisbursement
from app.services import vendor_disbursements as vd
from app.services.payments.zumrails_adapter import (
    PermanentZumrailsError,
    TransactionResult,
    TransactionStatus,
    TransientZumrailsError,
)

AS_OF = date(2026, 7, 15)  # a Wednesday
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
VENDOR = "11111111-1111-1111-1111-111111111111"


# --- fakes -------------------------------------------------------------------


class FakeSession:
    """Minimal Session stand-in: records adds, counts commits, optionally
    raises IntegrityError on the Nth commit to simulate a lost claim race."""

    def __init__(self, fail_commits: int = 0):
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self._fail_commits = fail_commits

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        if self._fail_commits > 0:
            self._fail_commits -= 1
            raise IntegrityError("duplicate claim", None, Exception("dup"))
        self.commits += 1

    def flush(self):
        pass

    def rollback(self):
        self.rollbacks += 1

    def rows(self):
        return [o for o in self.added if isinstance(o, PlatformVendorDisbursement)]

    def events(self, event_type=None):
        return [
            o
            for o in self.added
            if isinstance(o, PlatformEvent)
            and (event_type is None or o.event_type == event_type)
        ]


class ExplodingDB:
    """A db that fails the test on ANY use — proves the flag gate is inert."""

    def __getattr__(self, name):  # pragma: no cover - only fires on a bug
        raise AssertionError(f"db.{name} touched while the engine is gated off")


class FakeZum:
    def __init__(self, status=TransactionStatus.COMPLETED, raise_exc=None):
        self.status = status
        self.raise_exc = raise_exc
        self.calls = []

    def create_disbursement(
        self, *, recipient_id, amount_cents, client_transaction_id, memo=None
    ):
        if self.raise_exc is not None:
            raise self.raise_exc
        self.calls.append((recipient_id, amount_cents, client_transaction_id))
        return TransactionResult(
            transaction_id=f"Z-{client_transaction_id}",
            status=self.status,
            raw_status=self.status.value,
            amount_cents=amount_cents,
            currency="CAD",
            direction="disbursement",
            client_transaction_id=client_transaction_id,
        )


# --- pure wallet math --------------------------------------------------------


class TestVendorShare:
    def test_full_share(self):
        assert vd.vendor_share_cents(100_00, 10_000) == 100_00

    def test_partial_share_floors(self):
        # 85% of $100.01 = $85.0085 -> floored to 8500 cents.
        assert vd.vendor_share_cents(100_01, 8_500) == 8_500

    def test_zero_and_negative_collected(self):
        assert vd.vendor_share_cents(0, 10_000) == 0
        assert vd.vendor_share_cents(-500, 10_000) == 0

    def test_zero_share_bps(self):
        assert vd.vendor_share_cents(100_00, 0) == 0


class TestAvailability:
    def test_basic(self):
        # due = 100% of 50_000 = 50_000; less 10_000 settled, 5_000 in flight.
        assert vd.compute_available(50_000, 10_000, 5_000, 10_000) == 35_000

    def test_floors_at_zero_when_overdrawn(self):
        assert vd.compute_available(10_000, 9_000, 5_000, 10_000) == 0

    def test_partial_share(self):
        # 50% of 50_000 = 25_000 due; less nothing paid.
        assert vd.compute_available(50_000, 0, 0, 5_000) == 25_000


class TestPlanExtraPayout:
    def test_net_after_fee(self):
        assert vd.plan_extra_payout(10_000, 500) == 9_500

    def test_no_fee(self):
        assert vd.plan_extra_payout(10_000, 0) == 10_000

    def test_nothing_available(self):
        with pytest.raises(vd.DisbursementError):
            vd.plan_extra_payout(0, 0)

    def test_fee_swallows_balance(self):
        with pytest.raises(vd.DisbursementError):
            vd.plan_extra_payout(500, 500)  # net 0 -> reject


class TestHoldbackWalk:
    def test_four_business_days_before_wednesday(self):
        # Wed 2026-07-15 back 4 business days: Tue14, Mon13, Fri10, Thu9
        # (weekend Sat11/Sun12 skipped). db=None => weekends+holidays only.
        assert vd._business_days_before(AS_OF, 4, None) == date(2026, 7, 9)

    def test_zero_days_is_identity(self):
        assert vd._business_days_before(AS_OF, 0, None) == AS_OF

    def test_skips_a_weekend(self):
        # Monday back 1 business day -> previous Friday.
        monday = date(2026, 7, 13)
        assert vd._business_days_before(monday, 1, None) == date(2026, 7, 10)


# --- execution (claim-before-push) -------------------------------------------


def _exec(db, zum, **kw):
    params = dict(
        vendor_id=VENDOR,
        kind=vd.KIND_EXTRA,
        net_cents=9_500,
        fee_cents=500,
        holdback_cutoff=date(2026, 7, 9),
        client_transaction_id="vdisb-extra-abc",
        requested_by="user-1",
        recipient_id="ZUSER-1",
        zumrails=zum,
        now=NOW,
    )
    params.update(kw)
    return vd.execute_disbursement(db, **params)


class TestExecuteDisbursement:
    def test_completed_settles_synchronously(self):
        db = FakeSession()
        zum = FakeZum(status=TransactionStatus.COMPLETED)
        res = _exec(db, zum)
        assert res.status == "settled"
        row = db.rows()[0]
        assert row.status == "completed"
        assert row.amount_cents == 9_500 and row.fee_cents == 500
        assert row.external_ref == "Z-vdisb-extra-abc"
        # claim-before-push: adapter called exactly once, after the claim commit.
        assert zum.calls == [("ZUSER-1", 9_500, "vdisb-extra-abc")]
        assert db.events(vd.INITIATED_EVENT)
        assert db.events(vd.COMPLETED_EVENT)

    def test_pending_status_stays_processing(self):
        db = FakeSession()
        zum = FakeZum(status=TransactionStatus.PENDING)
        res = _exec(db, zum)
        assert res.status == "initiated"
        assert db.rows()[0].status == "processing"

    def test_vendor_failed_marks_failed(self):
        db = FakeSession()
        zum = FakeZum(status=TransactionStatus.FAILED)
        res = _exec(db, zum)
        assert res.status == "failed"
        assert db.rows()[0].status == "failed"
        assert db.events(vd.FAILED_EVENT)

    def test_transient_leaves_pending_no_ref(self):
        db = FakeSession()
        zum = FakeZum(raise_exc=TransientZumrailsError("5xx"))
        res = _exec(db, zum)
        assert res.status == "errored"
        row = db.rows()[0]
        # UNKNOWN vendor state -> stays pending with no external ref (blocks
        # re-push via the unique client id); never a double payout.
        assert row.status == "pending"
        assert row.external_ref is None

    def test_permanent_marks_failed(self):
        db = FakeSession()
        zum = FakeZum(raise_exc=PermanentZumrailsError("bad funding source"))
        res = _exec(db, zum)
        assert res.status == "failed"
        assert db.rows()[0].status == "failed"

    def test_duplicate_claim_is_noop(self):
        db = FakeSession(fail_commits=1)  # the claim commit loses the race
        zum = FakeZum(status=TransactionStatus.COMPLETED)
        res = _exec(db, zum)
        assert res.status == "duplicate"
        assert db.rollbacks == 1
        assert zum.calls == []  # never pushed after a lost claim


# --- money-out flag gate -----------------------------------------------------


class TestFlagGate:
    def test_request_extra_raises_when_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "VENDOR_DISBURSEMENTS_ENABLED", False)
        with pytest.raises(vd.DisbursementError):
            vd.request_extra_payout(ExplodingDB(), VENDOR, requested_by="u")

    def test_monthly_run_is_noop_when_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "VENDOR_DISBURSEMENTS_ENABLED", False)
        res = vd.run_monthly_auto_disbursement(ExplodingDB(), AS_OF)
        assert res.enabled is False
        assert res.payouts_initiated == 0

    def test_default_flag_is_off(self):
        # The whole workstream ships inert.
        assert settings.VENDOR_DISBURSEMENTS_ENABLED is False


# --- serialization -----------------------------------------------------------


class TestSerialize:
    def test_serialize_shape(self):
        row = PlatformVendorDisbursement(
            vendor_id=VENDOR,
            kind=vd.KIND_AUTO,
            status="completed",
            amount_cents=1_000,
            fee_cents=0,
            holdback_cutoff=date(2026, 7, 9),
            period_year=2026,
            period_month=7,
            client_transaction_id="vdisb-auto-x-202607",
            requested_by="system:vendor_disbursement",
        )
        out = vd._serialize(row)
        assert out["kind"] == "auto_monthly"
        assert out["amount_cents"] == 1_000
        assert out["holdback_cutoff"] == "2026-07-09"
        assert out["period_month"] == 7
