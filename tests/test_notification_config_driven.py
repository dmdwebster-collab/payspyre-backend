"""End-to-end tests for the system-configurable notification subsystem.

Proves the wiring the config layer added on top of WS2/WS3:

* config-driven cadence — dunning reads offsets from platform_notification_rules.
* per-channel toggles — enabling dashboard/sms in the rule fans the dunning
  event out to those channels; disabling email drops it.
* dashboard channel — the processor records a dashboard_notification event
  (in-app) instead of calling a vendor.
* send-window defer/allow — a vendor send outside the borrower's per-province
  window is DEFERRED (held below the cursor, retried next run), not sent.

Postgres-backed. DO NOT run via this agent (shared test DB).
"""
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.platform.loan import PlatformLoan, PlatformLoanScheduleItem
from app.models.platform.notification_rule import PlatformNotificationRule
from app.models.platform.patient import PlatformPatient
from app.models.platform.patient_field import PlatformPatientField
from app.services.dunning import run_dunning_scan
from app.services.notification_processor import (
    DASHBOARD_EVENT_TYPE,
    NotificationProcessor,
)

AS_OF = date(2026, 7, 1)
# 13:00 UTC == 09:00 EDT (Toronto) → inside the 08:00–21:00 ON window.
INSIDE_WINDOW = datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc)
# 03:00 UTC == 23:00 EDT previous day → outside the ON window.
OUTSIDE_WINDOW = datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc)


# --- seed helpers -----------------------------------------------------------


def _patient(db: Session, *, province: str | None = "ON") -> PlatformPatient:
    p = PlatformPatient(email="dest@example.com", phone_e164="+15555550123",
                        legal_first_name="Jordan", legal_last_name="Lee")
    db.add(p)
    db.commit()
    db.refresh(p)
    if province is not None:
        db.add(PlatformPatientField(
            patient_id=p.id, field_key="province", field_value=province,
            source="clinic", is_current=True))
        db.commit()
    return p


def _application(db: Session, patient: PlatformPatient) -> uuid.UUID:
    from app.models.platform.credit_product import PlatformCreditProduct
    from app.models.platform.credit_application import PlatformCreditApplication

    product = db.query(PlatformCreditProduct).filter(
        PlatformCreditProduct.code == "dental_full_arch_v1").first()
    row = PlatformCreditApplication(
        patient_id=patient.id, credit_product_id=product.id,
        credit_product_version=product.version, requested_amount_cents=2_500_000,
        requested_amount_source="clinic", status="approved")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


def _loan(db: Session, app_id, status="active") -> PlatformLoan:
    loan = PlatformLoan(application_id=app_id, principal_cents=2_500_000,
                        annual_rate_bps=1299, term_months=24, status=status,
                        principal_balance_cents=2_500_000)
    db.add(loan)
    db.flush()
    db.refresh(loan)
    return loan


def _item(db: Session, loan, *, n: int, due: date, status="scheduled",
          total=111800, paid=0):
    db.add(PlatformLoanScheduleItem(
        loan_id=loan.id, installment_number=n, due_date=due,
        principal_cents=100000, interest_cents=11800, total_cents=total,
        paid_cents=paid, status=status))
    db.commit()


def _rule(db: Session, ntype: str, **kw) -> None:
    defaults = dict(offsets=[7, 2], email_enabled=True, dashboard_enabled=False,
                    sms_enabled=False, content_overrides={}, enabled=True)
    defaults.update(kw)
    db.add(PlatformNotificationRule(notification_type=ntype, **defaults))
    db.commit()


def _events(db, event_type):
    return db.execute(
        text("SELECT payload FROM platform_events WHERE event_type=:t ORDER BY id"),
        {"t": event_type}).fetchall()


# --- a dispatcher stub recording notification_sent --------------------------


class _FakeDispatcher:
    def __init__(self, db):
        self.db = db
        self.calls = []

    def send_notification(self, *, patient_id, notification_type, contact_method,
                          context, application_id=None, loan_id=None,
                          source_event_id=None, correlation_id=None):
        from app.models.platform.event import PlatformEvent
        self.calls.append((notification_type, contact_method, source_event_id))
        ev = PlatformEvent(
            event_type="notification_sent", actor="system",
            patient_id=patient_id, application_id=application_id,
            payload={"v": 1, "channel": contact_method, "vendor": "fake",
                     "notification_type": notification_type,
                     "source_event_id": source_event_id, "status": "sent"})
        self.db.add(ev)
        self.db.flush()
        return ev.id


# --- config-driven cadence --------------------------------------------------


class TestConfigDrivenCadence:
    def test_rule_offsets_drive_milestones(self, db_session: Session):
        # Rule says reminder at 5 days only → an item due in 5 days fires; the
        # dataclass default (7,2) would NOT fire at 5.
        _rule(db_session, "payment_due_reminder", offsets=[5])
        p = _patient(db_session)
        loan = _loan(db_session, _application(db_session, p))
        _item(db_session, loan, n=1, due=AS_OF + timedelta(days=5))
        _item(db_session, loan, n=2, due=AS_OF + timedelta(days=7))  # not in rule
        res = run_dunning_scan(db_session, AS_OF)
        db_session.commit()
        assert res.reminders_emitted == 1
        assert _events(db_session, "payment_due_reminder")[0][0]["dunning_key"].endswith(":due-5")

    def test_disabled_rule_emits_nothing(self, db_session: Session):
        _rule(db_session, "payment_due_reminder", enabled=False)
        p = _patient(db_session)
        loan = _loan(db_session, _application(db_session, p))
        _item(db_session, loan, n=1, due=AS_OF + timedelta(days=7))
        res = run_dunning_scan(db_session, AS_OF)
        db_session.commit()
        assert res.reminders_emitted == 0


# --- per-channel toggles ----------------------------------------------------


class TestChannelToggles:
    def test_dunning_emits_enabled_channels(self, db_session: Session):
        # Enable email + dashboard for overdue → both channels in the event.
        _rule(db_session, "payment_overdue", offsets=[7],
              email_enabled=True, dashboard_enabled=True, sms_enabled=False)
        p = _patient(db_session)
        loan = _loan(db_session, _application(db_session, p))
        _item(db_session, loan, n=1, due=AS_OF - timedelta(days=7), status="late")
        run_dunning_scan(db_session, AS_OF)
        db_session.commit()
        payload = _events(db_session, "payment_overdue")[0][0]
        assert sorted(payload["channels"]) == ["dashboard", "email"]


# --- dashboard channel ------------------------------------------------------


class TestDashboardChannel:
    def test_dashboard_recorded_not_sent_to_vendor(self, db_session: Session):
        _rule(db_session, "payment_overdue", offsets=[7],
              email_enabled=False, dashboard_enabled=True)
        p = _patient(db_session)
        loan = _loan(db_session, _application(db_session, p))
        _item(db_session, loan, n=1, due=AS_OF - timedelta(days=7), status="late")
        run_dunning_scan(db_session, AS_OF)
        db_session.commit()

        dispatcher = _FakeDispatcher(db_session)
        res = NotificationProcessor(db_session, dispatcher=dispatcher,
                                    now=INSIDE_WINDOW).run()
        # No vendor call; one dashboard record.
        assert dispatcher.calls == []
        assert res.dashboard == 1
        assert res.sent == 0
        rows = _events(db_session, DASHBOARD_EVENT_TYPE)
        assert len(rows) == 1
        card = rows[0][0]
        assert card["channel"] == "dashboard"
        assert card["notification_type"] == "payment_overdue"
        assert card["read"] is False
        assert card["subject"] and card["body"]

    def test_dashboard_dedup_on_rerun(self, db_session: Session):
        _rule(db_session, "payment_overdue", offsets=[7],
              email_enabled=False, dashboard_enabled=True)
        p = _patient(db_session)
        loan = _loan(db_session, _application(db_session, p))
        _item(db_session, loan, n=1, due=AS_OF - timedelta(days=7), status="late")
        run_dunning_scan(db_session, AS_OF)
        db_session.commit()

        NotificationProcessor(db_session, dispatcher=_FakeDispatcher(db_session),
                              now=INSIDE_WINDOW).run()
        # Force a rescan by resetting the cursor.
        db_session.execute(text("UPDATE platform_notification_cursor SET last_event_id=0"))
        db_session.commit()
        res2 = NotificationProcessor(db_session, dispatcher=_FakeDispatcher(db_session),
                                     now=INSIDE_WINDOW).run()
        assert res2.dashboard == 0  # already recorded → deduped
        assert len(_events(db_session, DASHBOARD_EVENT_TYPE)) == 1


# --- send-window defer / allow ----------------------------------------------


class TestSendWindow:
    def test_inside_window_sends(self, db_session: Session):
        p = _patient(db_session, province="ON")
        loan = _loan(db_session, _application(db_session, p))
        _item(db_session, loan, n=1, due=AS_OF - timedelta(days=7), status="late")
        run_dunning_scan(db_session, AS_OF)
        db_session.commit()
        dispatcher = _FakeDispatcher(db_session)
        res = NotificationProcessor(db_session, dispatcher=dispatcher,
                                    now=INSIDE_WINDOW).run()
        assert res.sent == 1
        assert res.deferred == 0

    def test_outside_window_defers_and_holds_cursor(self, db_session: Session):
        p = _patient(db_session, province="ON")
        loan = _loan(db_session, _application(db_session, p))
        _item(db_session, loan, n=1, due=AS_OF - timedelta(days=7), status="late")
        run_dunning_scan(db_session, AS_OF)
        db_session.commit()

        dispatcher = _FakeDispatcher(db_session)
        res = NotificationProcessor(db_session, dispatcher=dispatcher,
                                    now=OUTSIDE_WINDOW).run()
        assert res.sent == 0
        assert res.deferred == 1
        assert dispatcher.calls == []          # vendor never called
        # No sent/skip marker written → it will retry.
        assert _events(db_session, "notification_sent") == []
        assert _events(db_session, "notification_skipped") == []

        # Same events, now INSIDE the window → it sends (cursor was held).
        res2 = NotificationProcessor(db_session, dispatcher=_FakeDispatcher(db_session),
                                     now=INSIDE_WINDOW).run()
        assert res2.sent == 1
        assert len(_events(db_session, "notification_sent")) == 1

    def test_dashboard_not_gated_by_window(self, db_session: Session):
        # Dashboard is in-app, not a vendor send → recorded even at night.
        _rule(db_session, "payment_overdue", offsets=[7],
              email_enabled=False, dashboard_enabled=True)
        p = _patient(db_session, province="ON")
        loan = _loan(db_session, _application(db_session, p))
        _item(db_session, loan, n=1, due=AS_OF - timedelta(days=7), status="late")
        run_dunning_scan(db_session, AS_OF)
        db_session.commit()
        res = NotificationProcessor(db_session, dispatcher=_FakeDispatcher(db_session),
                                    now=OUTSIDE_WINDOW).run()
        assert res.dashboard == 1
        assert res.deferred == 0
