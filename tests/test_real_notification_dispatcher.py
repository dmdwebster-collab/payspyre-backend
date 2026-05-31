"""Tests for ``RealNotificationDispatcher`` — P7.4.

Vendor SDK calls are monkeypatched directly (Resend's sync client uses
``requests`` and Twilio's REST client is wholly internal, so respx is the
wrong tool here). We exercise:

- happy path email (Resend) + sms (Twilio)
- transient + permanent error classification per vendor
- two-step write + rollback on vendor failure (no orphan ``magic_link_issued``)
- ``notification_sent`` event payload shape — Q5 of the kickoff
- PII redaction helpers
- recipient resolution edge cases (missing email / phone)
"""
import hashlib
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.platform.event import PlatformEvent
from app.models.platform.patient import PlatformPatient
from app.services.real_notification_dispatcher import (
    NotificationError,
    PermanentNotificationError,
    RealNotificationDispatcher,
    TransientNotificationError,
    _classify_resend_error,
    _classify_twilio_error,
    _hash_recipient,
    _redact_pii,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def real_creds(monkeypatch):
    monkeypatch.setattr(settings, "USE_REAL_NOTIFICATIONS", True)
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_xxxxxxxxxxxxxxx")
    monkeypatch.setattr(settings, "RESEND_FROM_EMAIL", "noreply@payspyre.test")
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC" + "f" * 32)
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "test_token")
    monkeypatch.setattr(settings, "TWILIO_FROM_NUMBER", "+15005550006")


def _seed_patient(db: Session, *, email: str | None = "dest@example.com",
                  phone_e164: str | None = "+15555550123") -> PlatformPatient:
    patient = PlatformPatient(email=email, phone_e164=phone_e164,
                              legal_first_name="Test")
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return patient


def _count_events(db: Session, event_type: str, application_id: uuid.UUID) -> int:
    return db.execute(
        text("SELECT count(*) FROM platform_events WHERE event_type=:t AND application_id=:aid"),
        {"t": event_type, "aid": str(application_id)},
    ).scalar()


def _seed_application(db: Session, patient: PlatformPatient) -> uuid.UUID:
    """Use raw INSERT — we just need a valid application_id for events.
    No orchestrator needed; events accept any app id but FKs are required."""
    from app.models.platform.credit_product import PlatformCreditProduct
    from app.models.platform.credit_application import PlatformCreditApplication
    product = db.query(PlatformCreditProduct).filter(
        PlatformCreditProduct.code == "dental_full_arch_v1"
    ).first()
    app_row = PlatformCreditApplication(
        patient_id=patient.id,
        credit_product_id=product.id,
        credit_product_version=product.version,
        requested_amount_cents=2_500_000,
        requested_amount_source="clinic",
        status="started",
    )
    db.add(app_row)
    db.commit()
    db.refresh(app_row)
    return app_row.id


# ===========================================================================
# Pure helpers
# ===========================================================================


class TestHashRecipient:
    def test_email_lowercased_before_hash(self):
        a = _hash_recipient("email", "Foo@Example.COM")
        b = _hash_recipient("email", "foo@example.com")
        assert a == b
        assert a.startswith("sha256:")

    def test_phone_not_lowercased(self):
        # E.164 has no letters; lowering is a no-op but documented as a no-op.
        v = _hash_recipient("sms", "+15555550123")
        assert v.startswith("sha256:")
        # Stable
        assert v == _hash_recipient("sms", "+15555550123")

    def test_different_recipients_different_hashes(self):
        assert _hash_recipient("email", "a@example.com") != _hash_recipient("email", "b@example.com")


class TestRedactPII:
    def test_email_redacted(self):
        out = _redact_pii("Failed to send to alice@example.com")
        assert "alice@example.com" not in out
        assert "<email-redacted>" in out

    def test_phone_redacted(self):
        out = _redact_pii("Invalid 'To' phone number +15555550123 for region")
        assert "+15555550123" not in out
        assert "<phone-redacted>" in out

    def test_no_pii_unchanged(self):
        assert _redact_pii("Plain error message") == "Plain error message"

    def test_empty_passes_through(self):
        assert _redact_pii("") == ""


class TestClassifyTwilioError:
    def _make_exc(self, *, status=None, code=None, msg="boom"):
        e = Exception(msg)
        if status is not None:
            e.status = status
        if code is not None:
            e.code = code
        e.msg = msg
        return e

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    def test_5xx_and_429_transient(self, status):
        assert isinstance(_classify_twilio_error(self._make_exc(status=status)), TransientNotificationError)

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_4xx_permanent_when_no_code(self, status):
        assert isinstance(_classify_twilio_error(self._make_exc(status=status)), PermanentNotificationError)

    @pytest.mark.parametrize("code", [21211, 21610, 21614, 30003, 30004, 30007])
    def test_known_permanent_codes(self, code):
        # Status absent / 200 — the code alone classifies.
        assert isinstance(_classify_twilio_error(self._make_exc(code=code)), PermanentNotificationError)

    def test_unknown_status_conservative_transient(self):
        # No status, no known code, no 4xx → transient by default.
        assert isinstance(_classify_twilio_error(self._make_exc(msg="weird")), TransientNotificationError)

    def test_summary_redacts_recipient(self):
        e = self._make_exc(status=400, msg="Invalid 'To' +15555550123")
        result = _classify_twilio_error(e)
        assert "+15555550123" not in str(result)


class TestClassifyResendError:
    def test_5xx_attr_transient(self):
        e = Exception("Internal error")
        e.status_code = 503
        assert isinstance(_classify_resend_error(e), TransientNotificationError)

    def test_4xx_attr_permanent(self):
        e = Exception("Bad request")
        e.status_code = 422
        assert isinstance(_classify_resend_error(e), PermanentNotificationError)

    @pytest.mark.parametrize(
        "msg",
        ["invalid_email address", "validation_error: missing field",
         "Domain not_authorized", "restricted_recipient"],
    )
    def test_hints_classify_permanent(self, msg):
        e = Exception(msg)
        assert isinstance(_classify_resend_error(e), PermanentNotificationError)

    def test_unknown_message_conservative_transient(self):
        e = Exception("mysterious failure")
        assert isinstance(_classify_resend_error(e), TransientNotificationError)


# ===========================================================================
# Dispatcher — happy paths
# ===========================================================================


class TestEmailHappyPath:
    @patch("app.services.real_notification_dispatcher.resend.Emails.send")
    def test_send_returns_event_id(
        self, mock_send, db_session: Session, real_creds
    ):
        mock_send.return_value = {"id": "re_email_uuid_1"}
        patient = _seed_patient(db_session)
        app_id = _seed_application(db_session, patient)

        d = RealNotificationDispatcher(db_session)
        event_id = d.send_magic_link(patient.id, app_id, "email", token="ABC123")
        db_session.commit()  # mirror auth-service commit
        assert isinstance(event_id, int)

        assert _count_events(db_session, "magic_link_issued", app_id) == 1
        assert _count_events(db_session, "notification_sent", app_id) == 1

    @patch("app.services.real_notification_dispatcher.resend.Emails.send")
    def test_send_passes_correct_params(self, mock_send, db_session, real_creds):
        mock_send.return_value = {"id": "re_xxxx"}
        patient = _seed_patient(db_session, email="alice@example.com")
        app_id = _seed_application(db_session, patient)
        RealNotificationDispatcher(db_session).send_magic_link(
            patient.id, app_id, "email", token="XYZ987"
        )
        call_args = mock_send.call_args[0][0]
        assert call_args["to"] == "alice@example.com"
        assert call_args["from"] == "noreply@payspyre.test"
        assert call_args["subject"] == "Your PaySpyre verification code"
        # Token must be in the HTML body — the recipient needs it.
        assert "XYZ987" in call_args["html"]

    @patch("app.services.real_notification_dispatcher.resend.Emails.send")
    def test_notification_sent_payload_shape(
        self, mock_send, db_session, real_creds
    ):
        mock_send.return_value = {"id": "re_msg_abc"}
        patient = _seed_patient(db_session, email="Alice@Example.COM")
        app_id = _seed_application(db_session, patient)

        d = RealNotificationDispatcher(db_session)
        magic_id = d.send_magic_link(patient.id, app_id, "email", token="T1")
        db_session.commit()

        row = db_session.execute(
            text("SELECT payload FROM platform_events WHERE event_type='notification_sent' "
                 "AND application_id=:a LIMIT 1"),
            {"a": str(app_id)},
        ).first()
        p = row[0]
        assert p["channel"] == "email"
        assert p["vendor"] == "resend"
        assert p["vendor_message_id"] == "re_msg_abc"
        assert p["status"] == "sent"
        assert p["magic_link_event_id"] == magic_id
        assert p["error_class"] is None
        assert p["error_message_redacted"] is None
        # Recipient redacted; same hash as the helper produces for the lowercased form.
        expected = _hash_recipient("email", "Alice@Example.COM")
        assert p["recipient_redacted"] == expected
        # No raw email anywhere.
        import json
        assert "alice@example.com" not in json.dumps(p).lower()


class TestSmsHappyPath:
    @patch("app.services.real_notification_dispatcher.TwilioSmsSender")
    def test_send_returns_event_id_and_writes_event(
        self, mock_sender_cls, db_session, real_creds
    ):
        # Build a fake sender whose send_magic_link returns a SendOutcome.
        from app.services.real_notification_dispatcher import SendOutcome
        instance = mock_sender_cls.return_value
        instance.send_magic_link.return_value = SendOutcome(
            vendor="twilio", vendor_message_id="SM" + "f" * 32, status="queued"
        )

        patient = _seed_patient(db_session)
        app_id = _seed_application(db_session, patient)
        d = RealNotificationDispatcher(db_session)
        event_id = d.send_magic_link(patient.id, app_id, "sms", token="SMS01")
        db_session.commit()

        assert isinstance(event_id, int)
        assert _count_events(db_session, "magic_link_issued", app_id) == 1
        assert _count_events(db_session, "notification_sent", app_id) == 1

        row = db_session.execute(
            text("SELECT payload FROM platform_events WHERE event_type='notification_sent' "
                 "AND application_id=:a LIMIT 1"),
            {"a": str(app_id)},
        ).first()
        p = row[0]
        assert p["channel"] == "sms"
        assert p["vendor"] == "twilio"
        assert p["status"] == "queued"   # Twilio sync ack normalizes to "queued"
        assert p["vendor_message_id"].startswith("SM")

    @patch("app.services.real_notification_dispatcher.TwilioSmsSender")
    def test_twilio_terminal_failure_raises_permanent(
        self, mock_sender_cls, db_session, real_creds
    ):
        instance = mock_sender_cls.return_value
        instance.send_magic_link.side_effect = PermanentNotificationError(
            "Twilio sync status failed"
        )
        patient = _seed_patient(db_session)
        app_id = _seed_application(db_session, patient)

        with pytest.raises(PermanentNotificationError):
            RealNotificationDispatcher(db_session).send_magic_link(
                patient.id, app_id, "sms", token="X"
            )


# ===========================================================================
# Rollback semantics — Q4
# ===========================================================================


class TestRollbackOnVendorFailure:
    @patch("app.services.real_notification_dispatcher.resend.Emails.send")
    def test_resend_failure_does_not_commit_either_event(
        self, mock_send, db_session: Session, real_creds
    ):
        mock_send.side_effect = Exception("connection refused")
        patient = _seed_patient(db_session)
        app_id = _seed_application(db_session, patient)

        with pytest.raises(TransientNotificationError):
            RealNotificationDispatcher(db_session).send_magic_link(
                patient.id, app_id, "email", token="T"
            )
        # No COMMIT happened — session is dirty. Roll back as auth service would on error.
        db_session.rollback()

        assert _count_events(db_session, "magic_link_issued", app_id) == 0
        assert _count_events(db_session, "notification_sent", app_id) == 0

    @patch("app.services.real_notification_dispatcher.TwilioSmsSender")
    def test_twilio_failure_does_not_commit_either_event(
        self, mock_sender_cls, db_session, real_creds
    ):
        instance = mock_sender_cls.return_value
        instance.send_magic_link.side_effect = TransientNotificationError("Twilio HTTP 503")
        patient = _seed_patient(db_session)
        app_id = _seed_application(db_session, patient)

        with pytest.raises(TransientNotificationError):
            RealNotificationDispatcher(db_session).send_magic_link(
                patient.id, app_id, "sms", token="T"
            )
        db_session.rollback()
        assert _count_events(db_session, "magic_link_issued", app_id) == 0
        assert _count_events(db_session, "notification_sent", app_id) == 0


# ===========================================================================
# Recipient resolution edges
# ===========================================================================


class TestRecipientResolution:
    def test_unknown_patient_permanent_error(self, db_session, real_creds):
        # No patient seeded; random UUID won't resolve.
        d = RealNotificationDispatcher(db_session)
        with pytest.raises(PermanentNotificationError, match="not found"):
            d.send_magic_link(uuid.uuid4(), uuid.uuid4(), "email", token="X")

    def test_email_channel_without_email_permanent_error(self, db_session, real_creds):
        patient = _seed_patient(db_session, email=None, phone_e164="+15555550999")
        app_id = _seed_application(db_session, patient)
        d = RealNotificationDispatcher(db_session)
        with pytest.raises(PermanentNotificationError, match="no email"):
            d.send_magic_link(patient.id, app_id, "email", token="X")

    def test_sms_channel_without_phone_permanent_error(self, db_session, real_creds):
        patient = _seed_patient(db_session, email="x@example.com", phone_e164=None)
        app_id = _seed_application(db_session, patient)
        d = RealNotificationDispatcher(db_session)
        with pytest.raises(PermanentNotificationError, match="no phone"):
            d.send_magic_link(patient.id, app_id, "sms", token="X")
