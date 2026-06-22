"""Tests for the NotificationProcessor — WS2.

Exercises the outbox lane end to end against the real (Postgres) test DB:
mapping (approved → email, declined → nothing, dunning pass-through), dedup,
permanent-skip auditing, and transient-failure cursor-hold/retry.
"""
import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.platform.event import PlatformEvent
from app.models.platform.loan import PlatformLoan, PlatformLoanScheduleItem
from app.models.platform.patient import PlatformPatient
from app.services.notification_processor import NotificationProcessor
from app.services.real_notification_dispatcher import (
    PermanentNotificationError,
    TransientNotificationError,
)


# --- seed helpers -----------------------------------------------------------


def _patient(db: Session, *, email="dest@example.com") -> PlatformPatient:
    p = PlatformPatient(email=email, phone_e164="+15555550123",
                        legal_first_name="Jordan", legal_last_name="Lee")
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _application(db: Session, patient: PlatformPatient) -> uuid.UUID:
    from app.models.platform.credit_product import PlatformCreditProduct
    from app.models.platform.credit_application import PlatformCreditApplication

    product = db.query(PlatformCreditProduct).filter(
        PlatformCreditProduct.code == "dental_full_arch_v1"
    ).first()
    row = PlatformCreditApplication(
        patient_id=patient.id, credit_product_id=product.id,
        credit_product_version=product.version, requested_amount_cents=2_500_000,
        requested_amount_source="clinic", status="approved",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


def _book_loan(db: Session, app_id: uuid.UUID) -> PlatformLoan:
    loan = PlatformLoan(
        application_id=app_id, principal_cents=2_500_000, annual_rate_bps=1299,
        term_months=24, status="pending_disbursement", principal_balance_cents=2_500_000,
    )
    db.add(loan)
    db.flush()
    db.add(PlatformLoanScheduleItem(
        loan_id=loan.id, installment_number=1, due_date=date(2026, 8, 1),
        principal_cents=100000, interest_cents=11800, total_cents=111800, status="scheduled",
    ))
    db.commit()
    db.refresh(loan)
    return loan


def _emit(db: Session, *, event_type: str, app_id, patient_id, payload: dict) -> int:
    full = {"v": 1, "actor": {"type": "system", "id": "system"},
            "application_id": str(app_id) if app_id else None,
            "patient_id": str(patient_id) if patient_id else None, **payload}
    ev = PlatformEvent(event_type=event_type, actor="system",
                       patient_id=patient_id, application_id=app_id, payload=full)
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev.id


def _decision_event(db, app_id, patient_id, decision: str) -> int:
    return _emit(db, event_type="decision_made", app_id=app_id, patient_id=patient_id,
                 payload={"after": {"status": decision, "decision": decision}})


def _count(db, event_type: str, source_event_id: int) -> int:
    return db.execute(
        text("SELECT count(*) FROM platform_events WHERE event_type=:t "
             "AND payload->>'source_event_id' = :s"),
        {"t": event_type, "s": str(source_event_id)},
    ).scalar()


# --- a dispatcher stub for failure injection --------------------------------


class _FakeDispatcher:
    """Records a notification_sent on success (so dedup works) or raises."""
    def __init__(self, db, exc=None):
        self.db = db
        self.exc = exc
        self.calls = []

    def send_notification(self, *, patient_id, notification_type, contact_method,
                          context, application_id=None, loan_id=None,
                          source_event_id=None, correlation_id=None):
        self.calls.append((notification_type, contact_method, source_event_id))
        if self.exc is not None:
            raise self.exc
        ev = PlatformEvent(
            event_type="notification_sent", actor="system",
            patient_id=patient_id, application_id=application_id,
            payload={"v": 1, "channel": contact_method, "vendor": "fake",
                     "notification_type": notification_type,
                     "source_event_id": source_event_id, "status": "sent"},
        )
        self.db.add(ev)
        self.db.flush()
        return ev.id


# --- tests ------------------------------------------------------------------


class TestDecisionMapping:
    def test_approved_sends_approval_email(self, db_session: Session):
        p = _patient(db_session)
        app_id = _application(db_session, p)
        _book_loan(db_session, app_id)
        sid = _decision_event(db_session, app_id, p.id, "approved")

        res = NotificationProcessor(db_session).run()
        assert res.sent == 1
        row = db_session.execute(
            text("SELECT payload FROM platform_events WHERE event_type='notification_sent' "
                 "AND payload->>'source_event_id' = :s LIMIT 1"),
            {"s": str(sid)},
        ).first()
        assert row[0]["notification_type"] == "application_approved"
        assert row[0]["channel"] == "email"

    def test_declined_sends_nothing(self, db_session: Session):
        # Adverse-action notice owns declines; the processor must not touch them.
        p = _patient(db_session)
        app_id = _application(db_session, p)
        sid = _decision_event(db_session, app_id, p.id, "declined")
        res = NotificationProcessor(db_session).run()
        assert res.sent == 0
        assert _count(db_session, "notification_sent", sid) == 0

    def test_under_review_deferred(self, db_session: Session):
        p = _patient(db_session)
        app_id = _application(db_session, p)
        sid = _decision_event(db_session, app_id, p.id, "manual_review")
        res = NotificationProcessor(db_session).run()
        assert res.sent == 0
        assert _count(db_session, "notification_sent", sid) == 0


class TestDedup:
    def test_second_run_does_not_resend(self, db_session: Session):
        p = _patient(db_session)
        app_id = _application(db_session, p)
        _book_loan(db_session, app_id)
        sid = _decision_event(db_session, app_id, p.id, "approved")

        NotificationProcessor(db_session).run()
        # Reset cursor to force a rescan of the same event; dedup must hold.
        db_session.execute(text("UPDATE platform_notification_cursor SET last_event_id = 0"))
        db_session.commit()
        res2 = NotificationProcessor(db_session).run()
        assert res2.sent == 0
        assert _count(db_session, "notification_sent", sid) == 1


class TestPassthroughDunning:
    def test_payment_reminder_fans_out_enabled_channels(self, db_session: Session):
        # The processor filters event channels through the live per-channel rule
        # config. Enable BOTH email + sms for payment_due_reminder, then a 2-channel
        # event fans out to 2 sends. (Default config is email-only, per policy.)
        from app.models.platform.notification_rule import PlatformNotificationRule
        db_session.merge(PlatformNotificationRule(
            notification_type="payment_due_reminder", offsets=[7, 2],
            email_enabled=True, sms_enabled=True, dashboard_enabled=False, enabled=True))
        db_session.commit()

        p = _patient(db_session)
        app_id = _application(db_session, p)
        ctx = dict(borrower_name="Jordan", loan_id="L-1", payment_amount="$250.00",
                   due_date="2026-08-01", payment_method="PAD",
                   payment_url="https://app.payspyre.com/pay/x", late_fee="$25.00",
                   account_url="https://app.payspyre.com/acct", days_until_due="3 days")
        sid = _emit(db_session, event_type="payment_due_reminder", app_id=app_id,
                    patient_id=p.id,
                    payload={"loan_id": "L-1", "channels": ["email", "sms"], "context": ctx})
        res = NotificationProcessor(db_session).run()
        assert res.sent == 2
        assert _count(db_session, "notification_sent", sid) == 2

    def test_payment_reminder_default_config_is_email_only(self, db_session: Session):
        # With no rule override (default seed = email only), an event that lists
        # both channels still sends only email — config governs, not the payload.
        p = _patient(db_session)
        app_id = _application(db_session, p)
        ctx = dict(borrower_name="Jordan", loan_id="L-1", payment_amount="$250.00",
                   due_date="2026-08-01", payment_method="PAD",
                   payment_url="https://app.payspyre.com/pay/x", late_fee="$25.00",
                   account_url="https://app.payspyre.com/acct", days_until_due="3 days")
        sid = _emit(db_session, event_type="payment_due_reminder", app_id=app_id,
                    patient_id=p.id,
                    payload={"loan_id": "L-1", "channels": ["email", "sms"], "context": ctx})
        res = NotificationProcessor(db_session).run()
        assert res.sent == 1
        assert _count(db_session, "notification_sent", sid) == 1


class TestPermanentSkip:
    def test_permanent_failure_records_skip_and_advances(self, db_session: Session):
        p = _patient(db_session)
        app_id = _application(db_session, p)
        _book_loan(db_session, app_id)
        sid = _decision_event(db_session, app_id, p.id, "approved")

        fake = _FakeDispatcher(db_session, exc=PermanentNotificationError("no email"))
        res = NotificationProcessor(db_session, dispatcher=fake).run()
        assert res.skipped == 1 and res.sent == 0
        assert _count(db_session, "notification_skipped", sid) == 1
        # Cursor advanced past the event (permanent skip does not retry).
        cur = db_session.execute(
            text("SELECT last_event_id FROM platform_notification_cursor")
        ).scalar()
        assert cur >= sid


class TestTransientRetry:
    def test_transient_holds_cursor_then_retries(self, db_session: Session):
        p = _patient(db_session)
        app_id = _application(db_session, p)
        _book_loan(db_session, app_id)
        sid = _decision_event(db_session, app_id, p.id, "approved")

        failing = _FakeDispatcher(db_session, exc=TransientNotificationError("503"))
        res = NotificationProcessor(db_session, dispatcher=failing).run()
        assert res.failed == 1 and res.sent == 0
        cur = db_session.execute(
            text("SELECT last_event_id FROM platform_notification_cursor")
        ).scalar()
        assert cur < sid  # held below the failed event

        # Next run with a working dispatcher delivers it.
        ok = _FakeDispatcher(db_session)
        res2 = NotificationProcessor(db_session, dispatcher=ok).run()
        assert res2.sent == 1
        assert _count(db_session, "notification_sent", sid) == 1


class TestCursorBounds:
    def test_events_at_or_below_cursor_not_reprocessed(self, db_session: Session):
        p = _patient(db_session)
        app_id = _application(db_session, p)
        sid = _decision_event(db_session, app_id, p.id, "declined")
        # Seed cursor past the event.
        db_session.execute(
            text("INSERT INTO platform_notification_cursor (name, last_event_id) "
                 "VALUES ('default', :s)"),
            {"s": sid},
        )
        db_session.commit()
        res = NotificationProcessor(db_session).run()
        assert res.scanned == 0
