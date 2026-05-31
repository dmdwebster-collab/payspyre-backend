"""Integration tests for POST /api/webhooks/v1/notifications/resend — P7.4b.

Live Supabase Session Pooler (house rule). Seeds a ``notification_sent`` event
manually, then POSTs Svix-signed JSON bodies and verifies the status-event +
suppression-row outcomes per event type.
"""
import base64
import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.platform.event import PlatformEvent

_RAW_KEY = b"resend_inbound_test_key_bytes!!"
_WHSEC = "whsec_" + base64.b64encode(_RAW_KEY).decode("ascii")
_PATH = "/api/webhooks/v1/notifications/resend"


@pytest.fixture(autouse=True)
def _patch_secret(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_WEBHOOK_SECRET", _WHSEC)


@pytest.fixture(autouse=True)
def _clean_suppression(db_session: Session):
    yield
    db_session.execute(text("TRUNCATE notification_suppression"))
    db_session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash(method: str, recipient: str) -> str:
    canonical = recipient.strip().lower() if method == "email" else recipient.strip()
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _seed_notification_sent(
    db: Session, *, email_id: str,
    recipient: str = "destination@example.com",
    magic_link_event_id: int = 1,
) -> tuple[int, str]:
    payload = {
        "v": 1,
        "actor": {"type": "system", "id": "system"},
        "channel": "email",
        "vendor": "resend",
        "vendor_message_id": email_id,
        "status": "sent",
        "recipient_redacted": _hash("email", recipient),
        "magic_link_event_id": magic_link_event_id,
    }
    event = PlatformEvent(event_type="notification_sent", actor="system", payload=payload)
    db.add(event)
    db.commit()
    db.refresh(event)
    return int(event.id), payload["recipient_redacted"]


def _sign_and_post(
    client: TestClient,
    body: dict,
    *,
    svix_id: str | None = None,
    svix_timestamp: str | None = None,
    key: bytes = _RAW_KEY,
):
    svix_id = svix_id or f"msg_{uuid.uuid4().hex[:12]}"
    svix_timestamp = svix_timestamp or str(int(time.time()))
    raw_body = json.dumps(body).encode("utf-8")
    signed = f"{svix_id}.{svix_timestamp}.{raw_body.decode('utf-8')}".encode("utf-8")
    digest = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")
    sig_header = f"v1,{digest}"
    return client.post(
        _PATH,
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "svix-id": svix_id,
            "svix-timestamp": svix_timestamp,
            "svix-signature": sig_header,
        },
    )


def _status_event_count(db: Session, email_id: str) -> int:
    return db.execute(
        text(
            "SELECT count(*) FROM platform_events "
            "WHERE event_type='notification_status_updated' AND payload @> :k"
        ),
        {"k": json.dumps({"vendor": "resend", "vendor_message_id": email_id})},
    ).scalar()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEmailEvents:
    def test_delivered_emits_event_no_suppression(self, client, db_session):
        email_id = f"re_{uuid.uuid4().hex[:16]}"
        _seed_notification_sent(db_session, email_id=email_id)
        r = _sign_and_post(client, {
            "type": "email.delivered", "created_at": "2026-05-31T00:00:00Z",
            "data": {"email_id": email_id},
        })
        assert r.status_code == 202, r.text
        assert r.json()["status"] == "accepted"
        assert _status_event_count(db_session, email_id) == 1
        n = db_session.execute(
            text("SELECT count(*) FROM notification_suppression")
        ).scalar()
        assert n == 0

    def test_bounced_permanent_records_hard_bounce(self, client, db_session):
        email_id = f"re_{uuid.uuid4().hex[:16]}"
        _, redacted = _seed_notification_sent(db_session, email_id=email_id)
        r = _sign_and_post(client, {
            "type": "email.bounced", "created_at": "2026-05-31T00:00:00Z",
            "data": {
                "email_id": email_id,
                "bounce": {"type": "Permanent", "subType": "General"},
            },
        })
        assert r.status_code == 202
        row = db_session.execute(
            text("SELECT reason FROM notification_suppression WHERE recipient_redacted=:r"),
            {"r": redacted},
        ).first()
        assert row is not None and row[0] == "hard_bounce"

    def test_bounced_transient_no_suppression(self, client, db_session):
        email_id = f"re_{uuid.uuid4().hex[:16]}"
        _seed_notification_sent(db_session, email_id=email_id)
        r = _sign_and_post(client, {
            "type": "email.bounced", "created_at": "2026-05-31T00:00:00Z",
            "data": {
                "email_id": email_id,
                "bounce": {"type": "Transient", "subType": "MailboxFull"},
            },
        })
        assert r.status_code == 202
        n = db_session.execute(
            text("SELECT count(*) FROM notification_suppression")
        ).scalar()
        assert n == 0
        # Recorded status is failed_transient.
        row = db_session.execute(
            text(
                "SELECT payload->>'status' FROM platform_events "
                "WHERE event_type='notification_status_updated' AND payload @> :k"
            ),
            {"k": json.dumps({"vendor": "resend", "vendor_message_id": email_id})},
        ).first()
        assert row is not None and row[0] == "failed_transient"

    def test_complained_records_spam_complaint(self, client, db_session):
        email_id = f"re_{uuid.uuid4().hex[:16]}"
        _, redacted = _seed_notification_sent(db_session, email_id=email_id)
        r = _sign_and_post(client, {
            "type": "email.complained", "created_at": "2026-05-31T00:00:00Z",
            "data": {"email_id": email_id},
        })
        assert r.status_code == 202
        row = db_session.execute(
            text("SELECT reason FROM notification_suppression WHERE recipient_redacted=:r"),
            {"r": redacted},
        ).first()
        assert row is not None and row[0] == "spam_complaint"

    def test_failed_records_hard_bounce(self, client, db_session):
        email_id = f"re_{uuid.uuid4().hex[:16]}"
        _, redacted = _seed_notification_sent(db_session, email_id=email_id)
        r = _sign_and_post(client, {
            "type": "email.failed", "created_at": "2026-05-31T00:00:00Z",
            "data": {"email_id": email_id},
        })
        assert r.status_code == 202
        row = db_session.execute(
            text("SELECT reason FROM notification_suppression WHERE recipient_redacted=:r"),
            {"r": redacted},
        ).first()
        assert row is not None and row[0] == "hard_bounce"

    def test_delivery_delayed_is_transient_no_suppression(self, client, db_session):
        email_id = f"re_{uuid.uuid4().hex[:16]}"
        _seed_notification_sent(db_session, email_id=email_id)
        r = _sign_and_post(client, {
            "type": "email.delivery_delayed", "created_at": "2026-05-31T00:00:00Z",
            "data": {"email_id": email_id},
        })
        assert r.status_code == 202
        n = db_session.execute(
            text("SELECT count(*) FROM notification_suppression")
        ).scalar()
        assert n == 0

    def test_sent_event_recorded_as_sent(self, client, db_session):
        email_id = f"re_{uuid.uuid4().hex[:16]}"
        _seed_notification_sent(db_session, email_id=email_id)
        r = _sign_and_post(client, {
            "type": "email.sent", "created_at": "2026-05-31T00:00:00Z",
            "data": {"email_id": email_id},
        })
        assert r.status_code == 202
        row = db_session.execute(
            text(
                "SELECT payload->>'status' FROM platform_events "
                "WHERE event_type='notification_status_updated' AND payload @> :k"
            ),
            {"k": json.dumps({"vendor": "resend", "vendor_message_id": email_id})},
        ).first()
        assert row is not None and row[0] == "sent"


class TestInformationalEvents:
    def test_opened_returns_ignored(self, client, db_session):
        email_id = f"re_{uuid.uuid4().hex[:16]}"
        _seed_notification_sent(db_session, email_id=email_id)
        r = _sign_and_post(client, {
            "type": "email.opened", "created_at": "2026-05-31T00:00:00Z",
            "data": {"email_id": email_id},
        })
        assert r.status_code == 202
        assert r.json()["status"] == "ignored"
        # No event written.
        assert _status_event_count(db_session, email_id) == 0


class TestReplay:
    def test_same_svix_id_returns_replay(self, client, db_session):
        email_id = f"re_{uuid.uuid4().hex[:16]}"
        _seed_notification_sent(db_session, email_id=email_id)
        svix_id = f"msg_{uuid.uuid4().hex[:12]}"
        body = {
            "type": "email.delivered", "created_at": "2026-05-31T00:00:00Z",
            "data": {"email_id": email_id},
        }
        first = _sign_and_post(client, body, svix_id=svix_id)
        second = _sign_and_post(client, body, svix_id=svix_id)
        assert first.json()["status"] == "accepted"
        assert second.json()["status"] == "replay"
        assert _status_event_count(db_session, email_id) == 1


class TestOrphan:
    def test_unknown_email_id_emits_orphaned(self, client, db_session):
        unknown = f"re_{uuid.uuid4().hex[:16]}"
        r = _sign_and_post(client, {
            "type": "email.delivered", "created_at": "2026-05-31T00:00:00Z",
            "data": {"email_id": unknown},
        })
        assert r.status_code == 202
        assert r.json()["status"] == "orphaned"
        n = db_session.execute(
            text(
                "SELECT count(*) FROM platform_events "
                "WHERE event_type='webhook_orphaned' AND payload @> :k"
            ),
            {"k": json.dumps({"vendor": "resend", "vendor_message_id": unknown})},
        ).scalar()
        assert n == 1
