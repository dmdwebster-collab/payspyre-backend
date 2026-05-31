"""P7.4b: ``RealNotificationDispatcher`` honors the suppression list at send time.

Single integration test — a suppression row for the resolved recipient must
cause ``send_magic_link`` to raise ``PermanentNotificationError`` BEFORE the
vendor SDK is touched. Bypassing the gate via ``USE_SUPPRESSION_CHECK=False``
restores the P7.4 behavior.
"""
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.patient import PlatformPatient
from app.repositories.notification_suppression import NotificationSuppressionRepo
from app.services.real_notification_dispatcher import (
    PermanentNotificationError,
    RealNotificationDispatcher,
    _hash_recipient,
)


@pytest.fixture(autouse=True)
def _real_creds(monkeypatch):
    monkeypatch.setattr(settings, "USE_REAL_NOTIFICATIONS", True)
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_xxxxxxx")
    monkeypatch.setattr(settings, "RESEND_FROM_EMAIL", "noreply@payspyre.test")
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC" + "f" * 32)
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "test_token")
    monkeypatch.setattr(settings, "TWILIO_FROM_NUMBER", "+15005550006")


@pytest.fixture(autouse=True)
def _clean(db_session: Session):
    yield
    db_session.execute(text("TRUNCATE notification_suppression"))
    db_session.commit()


def _seed_patient_and_app(db: Session, email: str = "alice@example.com"):
    patient = PlatformPatient(email=email, legal_first_name="Suppressed")
    db.add(patient)
    db.commit()
    db.refresh(patient)
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
    return patient, app_row


class TestPreSendSuppression:
    @patch("app.services.real_notification_dispatcher.resend.Emails.send")
    def test_suppressed_email_blocks_send_before_vendor_call(
        self, mock_send, db_session: Session
    ):
        patient, app_row = _seed_patient_and_app(db_session, email="bouncer@example.com")
        # Pre-populate the suppression list with the resolved recipient.
        NotificationSuppressionRepo(db_session).upsert(
            contact_method="email",
            recipient_redacted=_hash_recipient("email", "bouncer@example.com"),
            reason="hard_bounce",
            vendor="resend",
            vendor_event_id="prior-bounce-1",
        )
        db_session.commit()

        d = RealNotificationDispatcher(db_session)
        with pytest.raises(PermanentNotificationError, match="suppressed"):
            d.send_magic_link(patient.id, app_row.id, "email", token="X")

        # Vendor SDK never called.
        assert mock_send.called is False

    @patch("app.services.real_notification_dispatcher.resend.Emails.send")
    def test_flag_off_bypasses_suppression(
        self, mock_send, db_session: Session, monkeypatch
    ):
        """Disabling USE_SUPPRESSION_CHECK restores P7.4 behavior — vendor call
        proceeds even when a suppression row exists."""
        monkeypatch.setattr(settings, "USE_SUPPRESSION_CHECK", False)
        mock_send.return_value = {"id": "re_after_flag_off"}
        patient, app_row = _seed_patient_and_app(db_session, email="ignored@example.com")
        NotificationSuppressionRepo(db_session).upsert(
            contact_method="email",
            recipient_redacted=_hash_recipient("email", "ignored@example.com"),
            reason="hard_bounce", vendor="resend",
        )
        db_session.commit()

        d = RealNotificationDispatcher(db_session)
        event_id = d.send_magic_link(patient.id, app_row.id, "email", token="X")
        db_session.commit()
        assert mock_send.called is True
        assert isinstance(event_id, int)
