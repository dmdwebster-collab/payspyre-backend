"""Tests for ``NotificationSuppressionRepo`` — P7.4b.

Runs against the live Supabase Session Pooler (per house rule). Uses the
``_truncate_suppression`` autouse fixture below to leave the table clean
between tests.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.repositories.notification_suppression import NotificationSuppressionRepo
from app.services.real_notification_dispatcher import _hash_recipient


@pytest.fixture(autouse=True)
def _truncate_suppression(db_session: Session):
    """Keep the suppression table empty per test — it's a singleton resource
    against live Supabase, so tests would interfere otherwise."""
    yield
    db_session.execute(text("TRUNCATE notification_suppression"))
    db_session.commit()


class TestIsSuppressed:
    def test_empty_table_returns_false(self, db_session):
        repo = NotificationSuppressionRepo(db_session)
        assert repo.is_suppressed("email", "alice@example.com") is False
        assert repo.is_suppressed("sms", "+15555550123") is False

    def test_after_upsert_returns_true(self, db_session):
        repo = NotificationSuppressionRepo(db_session)
        repo.upsert(
            contact_method="email",
            recipient_redacted=_hash_recipient("email", "alice@example.com"),
            reason="hard_bounce",
            vendor="resend",
            vendor_event_id="evt_1",
        )
        db_session.commit()
        assert repo.is_suppressed("email", "alice@example.com") is True
        # different recipient still False
        assert repo.is_suppressed("email", "bob@example.com") is False

    def test_email_case_insensitive_via_hash(self, db_session):
        """``_hash_recipient`` lowercases emails; the gate must follow suit."""
        repo = NotificationSuppressionRepo(db_session)
        repo.upsert(
            contact_method="email",
            recipient_redacted=_hash_recipient("email", "Alice@Example.COM"),
            reason="opt_out",
            vendor="resend",
        )
        db_session.commit()
        assert repo.is_suppressed("email", "alice@example.com") is True


class TestUpsertIdempotency:
    def test_upsert_twice_is_idempotent(self, db_session):
        repo = NotificationSuppressionRepo(db_session)
        redacted = _hash_recipient("sms", "+15555550123")
        repo.upsert(
            contact_method="sms", recipient_redacted=redacted,
            reason="opt_out", vendor="twilio", vendor_event_id="n1",
        )
        repo.upsert(
            contact_method="sms", recipient_redacted=redacted,
            reason="opt_out", vendor="twilio", vendor_event_id="n2",
        )
        db_session.commit()
        count = db_session.execute(
            text("SELECT count(*) FROM notification_suppression "
                 "WHERE contact_method='sms' AND recipient_redacted=:r"),
            {"r": redacted},
        ).scalar()
        assert count == 1  # ON CONFLICT DO NOTHING; first reason wins
