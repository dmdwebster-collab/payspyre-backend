"""Auto-collection engine (WS-G) — DB-free unit tests.

Everything here runs against fakes / transient model instances: the pure
planner (scan + idempotency + retry policy), NSF fee → add-on ledger row,
dead-account handling, charge execution against a fake Zumrails adapter, the
feature-flag gate, and PAD pre-notification emission. The DB-backed seams
(loaders / webhook) are exercised by CI's full suite via the shared entry
points they reuse (record_payment, on_collection_complete).
"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.platform.event import PlatformEvent
from app.models.platform.loan import (
    PlatformCollectionAttempt,
    PlatformLoan,
    PlatformLoanTransaction,
)
from app.schemas.pricing_config import (
    ChargeTiming,
    FeeCalc,
    FeeConfig,
    FeeType,
    PricingConfig,
)
from app.services import auto_collection as ac
from app.services.auto_collection import (
    AttemptView,
    AutoCollectionPolicy,
    ChargeCandidate,
    DEFAULT_POLICY,
    PlannedCharge,
    add_business_days,
    charge_nsf_fee,
    emit_pre_notifications,
    execute_charge,
    handle_failed_attempt,
    is_dead_account_code,
    nsf_fee_cents,
    plan_charges,
    run_auto_collection,
)
from app.services.payments.zumrails_adapter import (
    PermanentZumrailsError,
    TransactionResult,
    TransactionStatus,
    TransientZumrailsError,
)

AS_OF = date(2026, 7, 15)  # a Wednesday
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


# --- fakes -------------------------------------------------------------------


class FakeSession:
    """Minimal Session stand-in: records adds, counts commits, optionally
    raises IntegrityError to simulate a lost claim race."""

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
    def __init__(self, status=TransactionStatus.PENDING, raise_exc=None):
        self.status = status
        self.raise_exc = raise_exc
        self.calls = []

    def create_collection(self, *, payer_id, amount_cents, client_transaction_id, memo=None):
        if self.raise_exc is not None:
            raise self.raise_exc
        self.calls.append((payer_id, amount_cents, client_transaction_id))
        return TransactionResult(
            transaction_id=f"Z-{client_transaction_id}",
            status=self.status,
            raw_status=self.status.value,
            amount_cents=amount_cents,
            currency="CAD",
            direction="collection",
            client_transaction_id=client_transaction_id,
        )


def _loan(**kw) -> PlatformLoan:
    defaults = dict(
        id=uuid4(),
        application_id=None,
        principal_cents=200_000,
        annual_rate_bps=1299,
        term_months=12,
        status="active",
        principal_balance_cents=200_000,
    )
    defaults.update(kw)
    return PlatformLoan(**defaults)


def _attempt(loan, *, n=1, outcome="failed", item_id=None, ref=None, **kw):
    return PlatformCollectionAttempt(
        id=uuid4(),
        loan_id=loan.id,
        schedule_item_id=item_id or uuid4(),
        attempt_number=n,
        amount_cents=10_000,
        client_transaction_id=f"autocol-x-{n}",
        external_ref=ref,
        outcome=outcome,
        **kw,
    )


def _cand(item_id, *, due=AS_OF, total=50_000, paid=0, loan_id="L1"):
    return ChargeCandidate(
        item_id=item_id, loan_id=loan_id, due_date=due, total_cents=total, paid_cents=paid
    )


def _nsf_config(amount=4_500, enabled=True, calc=FeeCalc.FIXED_CENTS) -> PricingConfig:
    return PricingConfig(
        fees=[
            FeeConfig(
                fee_type=FeeType.NSF,
                calc=calc,
                amount=amount,
                charge_timing=ChargeTiming.ON_EVENT,
                add_on=True,
                enabled=enabled,
            )
        ]
    )


# --- policy ------------------------------------------------------------------


class TestPolicy:
    def test_defaults(self):
        p = DEFAULT_POLICY
        assert p.pre_notification_business_days == 3
        assert p.retry_delay_days == 3
        assert p.max_attempts == 2
        assert p.retry_amount_strategy == "full_installment"
        assert "account_closed" in p.dead_account_return_codes

    def test_db_override_overlays_defaults(self, monkeypatch):
        from app.services import integration_settings

        row = SimpleNamespace(
            enabled=True,
            config={
                "retry_delay_days": 5,
                "max_attempts": 3,
                "dead_account_return_codes": ["r02", "r03"],
            },
        )
        monkeypatch.setattr(integration_settings, "get", lambda db, provider: row)
        p = ac.get_policy(object())
        assert p.retry_delay_days == 5
        assert p.max_attempts == 3
        assert p.dead_account_return_codes == ("r02", "r03")
        assert p.pre_notification_business_days == 3  # untouched default

    def test_missing_or_disabled_row_falls_back(self, monkeypatch):
        from app.services import integration_settings

        monkeypatch.setattr(integration_settings, "get", lambda db, provider: None)
        assert ac.get_policy(object()) == DEFAULT_POLICY
        monkeypatch.setattr(
            integration_settings,
            "get",
            lambda db, provider: SimpleNamespace(enabled=False, config={"max_attempts": 9}),
        )
        assert ac.get_policy(object()) == DEFAULT_POLICY


# --- planner (scan + idempotency + retry) -------------------------------------


class TestPlanCharges:
    def test_due_today_plans_initial_full_outstanding(self):
        planned = plan_charges(AS_OF, [_cand("i1", total=50_000, paid=10_000)], {})
        assert planned == [PlannedCharge("i1", "L1", 1, 40_000, "initial")]

    def test_not_due_today_without_history_is_dunnings_business(self):
        planned = plan_charges(AS_OF, [_cand("i1", due=AS_OF - timedelta(days=10))], {})
        assert planned == []
        planned = plan_charges(AS_OF, [_cand("i1", due=AS_OF + timedelta(days=1))], {})
        assert planned == []

    def test_zero_outstanding_skipped(self):
        planned = plan_charges(AS_OF, [_cand("i1", total=50_000, paid=50_000)], {})
        assert planned == []

    def test_pending_attempt_blocks(self):
        attempts = {"i1": [AttemptView(1, "pending", AS_OF)]}
        assert plan_charges(AS_OF, [_cand("i1")], attempts) == []

    def test_completed_attempt_blocks(self):
        attempts = {"i1": [AttemptView(1, "completed", AS_OF - timedelta(days=3))]}
        assert (
            plan_charges(AS_OF, [_cand("i1", due=AS_OF - timedelta(days=3))], attempts) == []
        )

    def test_failed_attempt_retries_after_delay(self):
        due = AS_OF - timedelta(days=3)
        attempts = {"i1": [AttemptView(1, "failed", due)]}
        planned = plan_charges(AS_OF, [_cand("i1", due=due)], attempts)
        assert planned == [PlannedCharge("i1", "L1", 2, 50_000, "retry")]

    def test_failed_attempt_does_not_retry_before_delay(self):
        due = AS_OF - timedelta(days=2)
        attempts = {"i1": [AttemptView(1, "failed", due)]}  # only 2 days ago
        assert plan_charges(AS_OF, [_cand("i1", due=due)], attempts) == []

    def test_missed_cron_day_still_fires_later(self):
        due = AS_OF - timedelta(days=10)
        attempts = {"i1": [AttemptView(1, "failed", due)]}  # long past the delay
        planned = plan_charges(AS_OF, [_cand("i1", due=due)], attempts)
        assert len(planned) == 1 and planned[0].attempt_number == 2

    def test_max_attempts_exhausted_leaves_to_dunning(self):
        due = AS_OF - timedelta(days=20)
        attempts = {
            "i1": [
                AttemptView(1, "failed", due),
                AttemptView(2, "failed", due + timedelta(days=3)),
            ]
        }
        assert plan_charges(AS_OF, [_cand("i1", due=due)], attempts) == []

    def test_cancelled_attempt_is_retry_eligible(self):
        due = AS_OF - timedelta(days=4)
        attempts = {"i1": [AttemptView(1, "cancelled", due)]}
        planned = plan_charges(AS_OF, [_cand("i1", due=due)], attempts)
        assert len(planned) == 1 and planned[0].kind == "retry"

    def test_duplicate_cron_run_plans_nothing_twice(self):
        """THE double-charge test: after run 1 records its attempts, an
        identical run 2 (same as_of) plans zero charges."""
        candidates = [_cand("i1"), _cand("i2", total=30_000)]
        first = plan_charges(AS_OF, candidates, {})
        assert len(first) == 2
        # Run 1's claims are now attempt rows (in flight).
        attempts = {
            p.item_id: [AttemptView(p.attempt_number, "pending", AS_OF)] for p in first
        }
        assert plan_charges(AS_OF, candidates, attempts) == []
        # Even after they settle, no re-charge.
        attempts = {
            p.item_id: [AttemptView(p.attempt_number, "completed", AS_OF)] for p in first
        }
        assert plan_charges(AS_OF, candidates, attempts) == []

    def test_retry_amount_uses_strategy_and_is_clamped(self):
        strategy_calls = []

        def half(policy, outstanding, attempts):
            strategy_calls.append(outstanding)
            return outstanding // 2

        ac.RETRY_AMOUNT_STRATEGIES["half_test"] = half
        try:
            policy = AutoCollectionPolicy(retry_amount_strategy="half_test")
            due = AS_OF - timedelta(days=5)
            attempts = {"i1": [AttemptView(1, "failed", due)]}
            planned = plan_charges(AS_OF, [_cand("i1", due=due, total=50_000)], attempts, policy)
            assert planned[0].amount_cents == 25_000
            assert strategy_calls == [50_000]
        finally:
            del ac.RETRY_AMOUNT_STRATEGIES["half_test"]

    def test_unknown_strategy_falls_back_to_full(self):
        policy = AutoCollectionPolicy(retry_amount_strategy="does_not_exist")
        assert ac.retry_amount_cents(policy, 12_345, ()) == 12_345


class TestBusinessDays:
    def test_weekdays(self):
        assert add_business_days(date(2026, 7, 13), 3) == date(2026, 7, 16)  # Mon → Thu

    def test_skips_weekend(self):
        assert add_business_days(date(2026, 7, 15), 3) == date(2026, 7, 20)  # Wed → Mon
        assert add_business_days(date(2026, 7, 17), 1) == date(2026, 7, 20)  # Fri → Mon

    def test_zero(self):
        assert add_business_days(AS_OF, 0) == AS_OF


# --- dead-account return codes -------------------------------------------------


class TestDeadAccountCodes:
    @pytest.mark.parametrize(
        "code",
        [
            "account_closed",
            "Account Closed",
            "Account Closed - R02",
            "ACCOUNT NOT FOUND",
            "Payor Deceased",
            "funds frozen",
            "Stop Payment on file",
            "invalid account number",
        ],
    )
    def test_dead_codes_match(self, code):
        assert is_dead_account_code(code) is True

    @pytest.mark.parametrize(
        "code", ["NSF", "insufficient funds", "Insufficient Funds - R01", "", None, "timeout"]
    )
    def test_non_dead_codes_do_not_match(self, code):
        assert is_dead_account_code(code) is False

    def test_configured_codes_win(self):
        policy = AutoCollectionPolicy(dead_account_return_codes=("r99",))
        assert is_dead_account_code("R99", policy) is True
        assert is_dead_account_code("account closed", policy) is False


# --- NSF fee → add-on bucket ---------------------------------------------------


class TestNsfFee:
    def test_enabled_fixed_fee(self):
        assert nsf_fee_cents(_nsf_config(4_500), 200_000) == 4_500

    def test_rate_fee_is_bps_of_principal(self):
        cfg = _nsf_config(100, calc=FeeCalc.RATE_BPS)  # 1% of principal
        assert nsf_fee_cents(cfg, 200_000) == 2_000

    def test_disabled_or_absent_fee_is_none(self):
        assert nsf_fee_cents(_nsf_config(enabled=False), 200_000) is None
        assert nsf_fee_cents(PricingConfig(), 200_000) is None
        assert nsf_fee_cents(None, 200_000) is None

    def test_charge_appends_add_on_ledger_row_idempotently(self):
        db = FakeSession()
        loan = _loan()
        attempt = _attempt(loan, outcome="failed")
        txn = charge_nsf_fee(db, loan, attempt, fee_cents=4_500, vendor_id="V1", now=NOW)
        assert isinstance(txn, PlatformLoanTransaction)
        assert txn.txn_type == "fee"
        assert txn.add_on_cents == 4_500  # non-accruing add-on bucket
        assert txn.fees_cents == 0 and txn.principal_cents == 0 and txn.interest_cents == 0
        assert txn.amount_cents == 4_500
        assert txn.reference == f"V1-{loan.id}-1"
        assert attempt.nsf_fee_transaction_id == txn.id
        assert list(loan.transactions) == [txn]
        # Replay (same failed attempt) → no second fee.
        assert charge_nsf_fee(db, loan, attempt, fee_cents=4_500, vendor_id="V1", now=NOW) is None
        assert len(loan.transactions) == 1

    def test_seq_continues_the_ledger(self):
        db = FakeSession()
        loan = _loan()
        loan.transactions.append(
            PlatformLoanTransaction(
                id=uuid4(), loan_id=loan.id, seq=3, reference=f"none-{loan.id}-3",
                txn_type="payment", amount_cents=1, principal_cents=1,
                effective_date=AS_OF, processing_date=AS_OF, created_by="t",
            )
        )
        attempt = _attempt(loan, outcome="failed")
        txn = charge_nsf_fee(db, loan, attempt, fee_cents=4_500, vendor_id=None, now=NOW)
        assert txn.seq == 4
        assert txn.reference == f"none-{loan.id}-4"


# --- failure handling (webhook path core) --------------------------------------


class TestHandleFailedAttempt:
    def _run(self, db, loan, attempt, *, return_code="NSF", cfg=None,
             policy=DEFAULT_POLICY):
        handle_failed_attempt(
            db,
            attempt=attempt,
            loan=loan,
            return_code=return_code,
            policy=policy,
            pricing_cfg=cfg,
            vendor_id="V1",
            now=NOW,
        )

    def test_marks_failed_and_charges_nsf_once(self):
        db = FakeSession()
        loan = _loan()
        attempt = _attempt(loan, outcome="pending", ref="Z1")
        self._run(db, loan, attempt, cfg=_nsf_config(4_500))
        assert attempt.outcome == "failed"
        assert attempt.return_code == "NSF"
        assert attempt.completed_at == NOW
        assert attempt.nsf_fee_transaction_id is not None
        assert len(loan.transactions) == 1
        assert len(db.events(ac.NSF_FEE_CHARGED_EVENT)) == 1
        # Webhook replay → idempotent: no second fee, no second event.
        self._run(db, loan, attempt, cfg=_nsf_config(4_500))
        assert len(loan.transactions) == 1
        assert len(db.events(ac.NSF_FEE_CHARGED_EVENT)) == 1

    def test_no_nsf_fee_without_enabled_fee_config(self):
        db = FakeSession()
        loan = _loan()
        attempt = _attempt(loan, outcome="pending")
        self._run(db, loan, attempt, cfg=None)
        assert attempt.outcome == "failed"
        assert attempt.nsf_fee_transaction_id is None
        assert loan.transactions == []

    def test_dead_account_disables_auto_charge_and_notifies_staff(self):
        db = FakeSession()
        loan = _loan()
        attempt = _attempt(loan, outcome="pending")
        self._run(db, loan, attempt, return_code="Account Closed - R02")
        assert loan.auto_charge_enabled is False
        assert "Account Closed" in loan.auto_charge_disabled_reason
        events = db.events(ac.AUTO_CHARGE_DISABLED_EVENT)
        assert len(events) == 1
        assert events[0].payload["disabled_by"] == "system:auto_collection"
        # Replay does not double-emit (already disabled).
        self._run(db, loan, attempt, return_code="Account Closed - R02")
        assert len(db.events(ac.AUTO_CHARGE_DISABLED_EVENT)) == 1

    def test_regular_nsf_does_not_disable(self):
        db = FakeSession()
        loan = _loan()
        attempt = _attempt(loan, outcome="pending")
        self._run(db, loan, attempt, return_code="insufficient funds")
        assert loan.auto_charge_enabled is None  # untouched
        assert db.events(ac.AUTO_CHARGE_DISABLED_EVENT) == []

    def test_completed_attempt_never_unsettled(self):
        db = FakeSession()
        loan = _loan()
        attempt = _attempt(loan, outcome="completed")
        self._run(db, loan, attempt, cfg=_nsf_config())
        assert attempt.outcome == "completed"
        assert loan.transactions == []


# --- charge execution -----------------------------------------------------------


def _payer(db, loan):
    return "zum-user-1"


class TestExecuteCharge:
    def _planned(self, loan, item_id="item-1", n=1, amount=50_000):
        return PlannedCharge(item_id, loan.id, n, amount, "initial")

    def test_happy_path_claims_then_charges_then_records(self):
        db = FakeSession()
        loan = _loan()
        zum = FakeZum(TransactionStatus.PENDING)
        out = execute_charge(
            db, self._planned(loan), loan, zum, payer_resolver=_payer, now=NOW
        )
        assert out == "initiated"
        attempts = [a for a in db.added if isinstance(a, PlatformCollectionAttempt)]
        assert len(attempts) == 1
        attempt = attempts[0]
        assert attempt.client_transaction_id == "autocol-item-1-1"  # deterministic
        assert attempt.external_ref == "Z-autocol-item-1-1"
        assert attempt.outcome == "pending"
        assert zum.calls == [("zum-user-1", 50_000, "autocol-item-1-1")]
        # The initiation event the webhook resolves settlements by.
        events = db.events(ac.INITIATED_EVENT)
        assert len(events) == 1
        payload = events[0].payload
        assert payload["transaction_id"] == "Z-autocol-item-1-1"
        assert payload["amount_cents"] == 50_000
        assert payload["loan_id"] == str(loan.id)
        assert payload["source"] == "auto_collection"
        # Claim committed BEFORE the vendor call + once after recording.
        assert db.commits == 2

    def test_no_funding_profile_skips_without_claim(self):
        db = FakeSession()
        loan = _loan()
        zum = FakeZum()
        out = execute_charge(
            db, self._planned(loan), loan, zum, payer_resolver=lambda d, l: None, now=NOW
        )
        assert out == "skipped"
        assert zum.calls == []
        assert [a for a in db.added if isinstance(a, PlatformCollectionAttempt)] == []

    def test_lost_claim_race_skips_without_vendor_call(self):
        db = FakeSession(fail_commits=1)  # the claim commit hits the unique constraint
        loan = _loan()
        zum = FakeZum()
        out = execute_charge(
            db, self._planned(loan), loan, zum, payer_resolver=_payer, now=NOW
        )
        assert out == "skipped"
        assert db.rollbacks == 1
        assert zum.calls == []  # never reached the money rail

    def test_transient_error_leaves_pending_block(self):
        db = FakeSession()
        loan = _loan()
        zum = FakeZum(raise_exc=TransientZumrailsError("boom"))
        out = execute_charge(
            db, self._planned(loan), loan, zum, payer_resolver=_payer, now=NOW
        )
        assert out == "errored"
        attempt = [a for a in db.added if isinstance(a, PlatformCollectionAttempt)][0]
        # Vendor state unknown → stays pending (blocks re-attempts), no ref.
        assert attempt.outcome == "pending"
        assert attempt.external_ref is None
        assert attempt.error.startswith("transient")

    def test_permanent_rejection_fails_attempt_without_nsf(self):
        db = FakeSession()
        loan = _loan()
        zum = FakeZum(raise_exc=PermanentZumrailsError("bad funding source"))
        out = execute_charge(
            db, self._planned(loan), loan, zum, payer_resolver=_payer, now=NOW
        )
        assert out == "errored"
        attempt = [a for a in db.added if isinstance(a, PlatformCollectionAttempt)][0]
        assert attempt.outcome == "failed"
        assert attempt.return_code == "adapter_permanent_error"
        assert loan.transactions == []  # rail never pulled → no NSF fee

    def test_synchronous_completion_settles_via_existing_path(self, monkeypatch):
        db = FakeSession()
        loan = _loan()
        settled = []
        from app.services import loan_payments

        monkeypatch.setattr(
            loan_payments, "on_collection_complete", lambda d, t: settled.append(("pay", t)) or True
        )
        monkeypatch.setattr(
            ac, "on_collection_settled", lambda d, t: settled.append(("attempt", t)) or True
        )
        out = execute_charge(
            db,
            self._planned(loan),
            loan,
            FakeZum(TransactionStatus.COMPLETED),
            payer_resolver=_payer,
            now=NOW,
        )
        assert out == "settled"
        assert settled == [
            ("pay", "Z-autocol-item-1-1"),      # money applied by record_payment path
            ("attempt", "Z-autocol-item-1-1"),  # attempt bookkeeping
        ]


# --- the flag gate + orchestration ----------------------------------------------


class TestRunGate:
    def test_flag_off_is_a_strict_noop(self):
        # Default settings: AUTO_COLLECTION_ENABLED is False.
        result = run_auto_collection(ExplodingDB(), AS_OF)
        assert result.enabled is False
        assert result.charges_initiated == 0
        assert result.pre_notifications_emitted == 0

    def test_flag_on_but_no_adapter_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(
            ac, "settings", SimpleNamespace(AUTO_COLLECTION_ENABLED=True)
        )
        import app.services.loan_lifecycle as loan_lifecycle

        monkeypatch.setattr(loan_lifecycle, "_build_zumrails_adapter", lambda db: None)
        result = run_auto_collection(ExplodingDB(), AS_OF)
        assert result.enabled is True
        assert result.adapter_available is False
        assert result.charges_initiated == 0

    def _wire(self, monkeypatch, candidates, attempts_by_item, loans_by_id):
        monkeypatch.setattr(
            ac, "settings", SimpleNamespace(AUTO_COLLECTION_ENABLED=True)
        )
        monkeypatch.setattr(ac, "get_policy", lambda db: DEFAULT_POLICY)
        monkeypatch.setattr(ac, "emit_pre_notifications", lambda db, as_of, policy: 0)
        monkeypatch.setattr(
            ac,
            "_load_candidates",
            lambda db, as_of: (candidates, attempts_by_item, loans_by_id, {}),
        )

    def test_full_run_initiates_and_second_run_is_idempotent(self, monkeypatch):
        loan = _loan()
        candidates = [
            ChargeCandidate(item_id="i1", loan_id=loan.id, due_date=AS_OF, total_cents=50_000)
        ]
        loans = {loan.id: loan}
        zum = FakeZum(TransactionStatus.PENDING)

        # Run 1: charge initiated.
        self._wire(monkeypatch, candidates, {}, loans)
        db = FakeSession()
        result = run_auto_collection(
            db, AS_OF, zumrails=zum, payer_resolver=_payer, now=NOW
        )
        assert result.charges_planned == 1
        assert result.charges_initiated == 1
        assert len(zum.calls) == 1

        # Run 2 (duplicate cron run, same day): the attempt now exists → the
        # planner produces nothing and the money rail is never called again.
        attempts = {"i1": [AttemptView(1, "pending", AS_OF)]}
        self._wire(monkeypatch, candidates, attempts, loans)
        result2 = run_auto_collection(
            FakeSession(), AS_OF, zumrails=zum, payer_resolver=_payer, now=NOW
        )
        assert result2.charges_planned == 0
        assert result2.charges_initiated == 0
        assert len(zum.calls) == 1  # STILL exactly one vendor call — no double charge


# --- PAD pre-notifications -------------------------------------------------------


def _prenotify_row(item_id="item-1", **kw):
    row = {
        "item_id": item_id,
        "total_cents": 26_284,
        "paid_cents": 0,
        "due_date": date(2026, 7, 20),
        "loan_id": uuid4(),
        "application_id": uuid4(),
        "patient_id": uuid4(),
        "first_name": "Raleigh",
        "last_name": "Bailey",
    }
    row.update(kw)
    return row


class TestPreNotifications:
    def test_emits_event_with_context_and_pad_key(self):
        db = FakeSession()
        emitted = emit_pre_notifications(
            db, AS_OF, DEFAULT_POLICY,
            rows=[_prenotify_row()],
            already_emitted=lambda d, k: False,
        )
        assert emitted == 1
        events = db.events(ac.PRE_NOTIFICATION_EVENT)
        assert len(events) == 1
        payload = events[0].payload
        assert payload["pad_key"] == "item-1:pad-pre"
        assert payload["channels"] == ["email"]
        ctx = payload["context"]
        assert ctx["borrower_name"] == "Raleigh Bailey"
        assert ctx["payment_amount"] == "$262.84"
        assert ctx["charge_date"] == "July 20, 2026"

    def test_idempotent_via_pad_key(self):
        db = FakeSession()
        emitted = emit_pre_notifications(
            db, AS_OF, DEFAULT_POLICY,
            rows=[_prenotify_row()],
            already_emitted=lambda d, k: True,  # already in the event log
        )
        assert emitted == 0
        assert db.events(ac.PRE_NOTIFICATION_EVENT) == []

    def test_template_renders_with_emitted_context(self):
        """StrictUndefined render — proves template + subject variables all
        exist in the context the engine emits."""
        from app.services.notification_render import render_email, render_sms

        db = FakeSession()
        emit_pre_notifications(
            db, AS_OF, DEFAULT_POLICY,
            rows=[_prenotify_row()],
            already_emitted=lambda d, k: False,
        )
        ctx = db.events(ac.PRE_NOTIFICATION_EVENT)[0].payload["context"]
        subject, html = render_email("pad_pre_notification", ctx)
        assert "$262.84" in subject and "July 20, 2026" in subject
        assert "pre-authorized debit" in html
        sms = render_sms("pad_pre_notification", ctx)
        assert "$262.84" in sms

    def test_processor_fallback_rule_allows_email(self):
        from app.services.notification_config import _fallback_rule
        from app.services.dunning import DEFAULT_POLICY as DUNNING_POLICY

        rule = _fallback_rule("pad_pre_notification", DUNNING_POLICY)
        assert rule.enabled is True
        assert "email" in rule.enabled_channels


# --- webhook return-code extraction ----------------------------------------------


class TestReturnCodeExtraction:
    def test_probes_case_variants(self):
        from app.api.webhooks.v1.endpoints.payments import _extract_return_code

        assert _extract_return_code({"result": {"FailureReason": "Account Closed"}}) == "Account Closed"
        assert _extract_return_code({"Result": {"ReturnCode": "R02"}}) == "R02"
        assert _extract_return_code({"failureReason": "NSF"}) == "NSF"
        assert _extract_return_code({"result": {"Message": "insufficient funds"}}) == "insufficient funds"
        assert _extract_return_code({"result": {}}) is None
        assert _extract_return_code({}) is None
