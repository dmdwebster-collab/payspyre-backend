"""Tests for the generic notification send path — WS1.

Covers ``RealNotificationDispatcher.send_notification`` (email via stubbed
Resend, SMS via stubbed Twilio sender), ``MockNotificationDispatcher`` parity,
and the ``notification_render`` registry. The magic-link path is covered
separately in ``test_real_notification_dispatcher.py``; here we only assert the
template-driven send + audit-event shape.
"""
import json
import uuid
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.platform.patient import PlatformPatient
from app.services import notification_render as nr
from app.services.mock_notification_dispatcher import MockNotificationDispatcher
from app.services.real_notification_dispatcher import (
    PermanentNotificationError,
    RealNotificationDispatcher,
    SendOutcome,
    _hash_recipient,
)


# --- shared seed helpers (kept local to avoid cross-test imports) -----------


@pytest.fixture
def real_creds(monkeypatch):
    monkeypatch.setattr(settings, "USE_REAL_NOTIFICATIONS", True)
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_xxxxxxxxxxxxxxx")
    monkeypatch.setattr(settings, "RESEND_FROM_EMAIL", "noreply@payspyre.test")
    monkeypatch.setattr(settings, "EMAIL_PROVIDER", "resend")
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC" + "f" * 32)
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "test_token")
    monkeypatch.setattr(settings, "TWILIO_FROM_NUMBER", "+15005550006")


def _seed_patient(db: Session, *, email="dest@example.com", phone_e164="+15555550123"):
    p = PlatformPatient(email=email, phone_e164=phone_e164, legal_first_name="Jordan")
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _seed_application(db: Session, patient: PlatformPatient) -> uuid.UUID:
    from app.models.platform.credit_product import PlatformCreditProduct
    from app.models.platform.credit_application import PlatformCreditApplication

    product = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
    )
    row = PlatformCreditApplication(
        patient_id=patient.id,
        credit_product_id=product.id,
        credit_product_version=product.version,
        requested_amount_cents=2_500_000,
        requested_amount_source="clinic",
        status="started",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


def _payment_ctx() -> dict:
    return dict(
        borrower_name="Jordan",
        loan_id="L-123",
        payment_amount="$250.00",
        due_date="2026-07-01",
        payment_method="Pre-authorized debit",
        payment_url="https://app.payspyre.com/pay/abc",
        late_fee="$25.00",
        account_url="https://app.payspyre.com/account",
        # int, matching the real producer (dunning._context) — the subject
        # template compares it numerically.
        days_until_due=3,
    )


def _notif_payload(db: Session, patient_id) -> dict:
    row = db.execute(
        text(
            "SELECT payload FROM platform_events WHERE event_type='notification_sent' "
            "AND patient_id=:p ORDER BY id DESC LIMIT 1"
        ),
        {"p": str(patient_id)},
    ).first()
    return row[0]


# --- render registry --------------------------------------------------------


class TestRender:
    def test_email_renders_subject_and_body(self):
        subject, html = nr.render_email("payment_due_reminder", _payment_ctx())
        # Dave's autopay subject: offset-aware, no amount/date in the subject.
        assert "Autopay in 3 Days" in subject
        assert "Jordan" in html and "$250.00" in html

    def test_sms_renders(self):
        body = nr.render_sms("payment_due_reminder", _payment_ctx())
        assert "$250.00" in body and "app.payspyre.com/pay/abc" in body

    def test_email_only_type_has_no_sms(self):
        with pytest.raises(ValueError):
            nr.render_sms("application_declined", {"borrower_name": "X"})

    def test_unknown_type_raises(self):
        with pytest.raises(nr.UnknownNotificationType):
            nr.render_email("does_not_exist", {})

    def test_missing_context_key_fails_loud(self):
        # StrictUndefined: a missing variable must error, not ship "None".
        with pytest.raises(Exception):
            nr.render_email("payment_due_reminder", {"borrower_name": "Jordan"})


# --- real dispatcher --------------------------------------------------------


class TestRealSendNotificationEmail:
    @patch("app.services.real_notification_dispatcher.resend.Emails.send")
    def test_email_send_writes_audit(self, mock_send, db_session: Session, real_creds):
        mock_send.return_value = {"id": "re_notif_1"}
        patient = _seed_patient(db_session, email="Alice@Example.COM")
        app_id = _seed_application(db_session, patient)

        d = RealNotificationDispatcher(db_session)
        eid = d.send_notification(
            patient_id=patient.id,
            notification_type="payment_due_reminder",
            contact_method="email",
            context=_payment_ctx(),
            application_id=app_id,
            loan_id="L-123",
            source_event_id=42,
        )
        db_session.commit()
        assert isinstance(eid, int)

        # Subject + rendered body reached the vendor.
        params = mock_send.call_args[0][0]
        assert params["to"] == "Alice@Example.COM"
        assert "Autopay in 3 Days" in params["subject"]
        assert "Jordan" in params["html"]

        p = _notif_payload(db_session, patient.id)
        assert p["channel"] == "email"
        assert p["vendor"] == "resend"
        assert p["vendor_message_id"] == "re_notif_1"
        assert p["notification_type"] == "payment_due_reminder"
        assert p["loan_id"] == "L-123"
        assert p["source_event_id"] == 42
        assert p["recipient_redacted"] == _hash_recipient("email", "Alice@Example.COM")
        # No raw recipient anywhere in the audit row.
        assert "alice@example.com" not in json.dumps(p).lower()

    @patch("app.services.real_notification_dispatcher.resend.Emails.send")
    def test_vendor_failure_writes_no_audit(self, mock_send, db_session: Session, real_creds):
        mock_send.side_effect = Exception("boom 503")
        patient = _seed_patient(db_session)
        app_id = _seed_application(db_session, patient)
        d = RealNotificationDispatcher(db_session)
        with pytest.raises(Exception):
            d.send_notification(
                patient_id=patient.id,
                notification_type="payment_due_reminder",
                contact_method="email",
                context=_payment_ctx(),
                application_id=app_id,
            )
        db_session.rollback()
        cnt = db_session.execute(
            text("SELECT count(*) FROM platform_events WHERE event_type='notification_sent' "
                 "AND patient_id=:p"),
            {"p": str(patient.id)},
        ).scalar()
        assert cnt == 0


class TestRealSendNotificationSms:
    @patch("app.services.real_notification_dispatcher.TwilioSmsSender")
    def test_sms_send_uses_send_message(self, mock_cls, db_session: Session, real_creds):
        instance = MagicMock()
        instance.send_message.return_value = SendOutcome(
            vendor="twilio", vendor_message_id="SM123", status="sent"
        )
        mock_cls.return_value = instance
        patient = _seed_patient(db_session)
        app_id = _seed_application(db_session, patient)

        d = RealNotificationDispatcher(db_session)
        d.send_notification(
            patient_id=patient.id,
            notification_type="payment_due_reminder",
            contact_method="sms",
            context=_payment_ctx(),
            application_id=app_id,
        )
        db_session.commit()

        body = instance.send_message.call_args.kwargs["body"]
        assert "$250.00" in body
        p = _notif_payload(db_session, patient.id)
        assert p["channel"] == "sms"
        assert p["vendor"] == "twilio"
        assert p["vendor_message_id"] == "SM123"

    def test_suppressed_recipient_blocks_send(self, db_session: Session, real_creds, monkeypatch):
        monkeypatch.setattr(settings, "USE_SUPPRESSION_CHECK", True)
        patient = _seed_patient(db_session, email="blocked@example.com")
        app_id = _seed_application(db_session, patient)
        from app.repositories.notification_suppression import NotificationSuppressionRepo

        NotificationSuppressionRepo(db_session).upsert(
            contact_method="email",
            recipient_redacted=_hash_recipient("email", "blocked@example.com"),
            reason="opt_out",
            vendor="resend",
        )
        db_session.commit()
        d = RealNotificationDispatcher(db_session)
        with pytest.raises(PermanentNotificationError):
            d.send_notification(
                patient_id=patient.id,
                notification_type="payment_due_reminder",
                contact_method="email",
                context=_payment_ctx(),
                application_id=app_id,
            )


# --- mock dispatcher parity -------------------------------------------------


class TestMockSendNotification:
    def test_mock_records_and_renders(self, db_session: Session):
        patient = _seed_patient(db_session)
        app_id = _seed_application(db_session, patient)
        d = MockNotificationDispatcher(db_session)
        eid = d.send_notification(
            patient_id=patient.id,
            notification_type="payment_due_reminder",
            contact_method="email",
            context=_payment_ctx(),
            application_id=app_id,
        )
        db_session.commit()
        assert isinstance(eid, int)
        assert d._sent[-1]["notification_type"] == "payment_due_reminder"
        assert "Jordan" in d._sent[-1]["body"]
        p = _notif_payload(db_session, patient.id)
        assert p["vendor"] == "mock"
        assert p["notification_type"] == "payment_due_reminder"
