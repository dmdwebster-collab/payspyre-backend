"""Integration tests for POST /api/webhooks/v1/notifications/twilio — P7.4b.

Runs against the live Supabase Session Pooler (per house rule). Seeds a
``notification_sent`` event by hand (the real send path is exercised in
``test_real_notification_dispatcher.py``); each test POSTs a Twilio-signed
form body and asserts the right combination of status event +
``notification_suppression`` row.
"""
import hashlib
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from twilio.request_validator import RequestValidator

from app.core.config import settings
from app.models.platform.event import PlatformEvent

_AUTH_TOKEN = "twilio_inbound_test_auth_token"
_URL = "http://testserver/api/webhooks/v1/notifications/twilio"

_PATH = "/api/webhooks/v1/notifications/twilio"


@pytest.fixture(autouse=True)
def _patch_secret(monkeypatch):
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", _AUTH_TOKEN)
    monkeypatch.setattr(settings, "WEBHOOK_PUBLIC_BASE_URL", "")


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
    db: Session, *, sid: str, channel: str = "sms",
    recipient: str = "+15555550123",
    magic_link_event_id: int = 1,
) -> tuple[int, str]:
    """Insert a ``notification_sent`` event the inbound endpoint can correlate."""
    payload = {
        "v": 1,
        "actor": {"type": "system", "id": "system"},
        "channel": channel,
        "vendor": "twilio",
        "vendor_message_id": sid,
        "status": "queued",
        "recipient_redacted": _hash(channel, recipient),
        "magic_link_event_id": magic_link_event_id,
    }
    event = PlatformEvent(
        event_type="notification_sent",
        actor="system",
        payload=payload,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return int(event.id), payload["recipient_redacted"]


def _post(
    client: TestClient,
    form: dict[str, str],
    *,
    token: str = _AUTH_TOKEN,
    sign_url: str = _URL,
):
    sig = RequestValidator(token).compute_signature(sign_url, form)
    return client.post(
        _PATH,
        data=form,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Twilio-Signature": sig,
        },
    )


def _status_event_count(db: Session, sid: str) -> int:
    return db.execute(
        text(
            "SELECT count(*) FROM platform_events "
            "WHERE event_type='notification_status_updated' AND payload @> :k"
        ),
        {"k": json.dumps({"vendor": "twilio", "vendor_message_id": sid})},
    ).scalar()


def _orphan_count(db: Session, sid: str) -> int:
    return db.execute(
        text(
            "SELECT count(*) FROM platform_events "
            "WHERE event_type='webhook_orphaned' AND payload @> :k"
        ),
        {"k": json.dumps({"vendor": "twilio", "vendor_message_id": sid})},
    ).scalar()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTerminalDelivered:
    def test_delivered_emits_event_no_suppression(
        self, client: TestClient, db_session: Session
    ):
        sid = "SM" + "f" * 32
        _seed_notification_sent(db_session, sid=sid)
        r = _post(client, {"MessageSid": sid, "MessageStatus": "delivered"})
        assert r.status_code == 202, r.text
        assert r.json()["status"] == "accepted"
        assert _status_event_count(db_session, sid) == 1
        # No suppression for a clean delivered.
        n = db_session.execute(
            text("SELECT count(*) FROM notification_suppression")
        ).scalar()
        assert n == 0


class TestTerminalFailures:
    def test_failed_no_error_code_emits_event_and_hard_bounce(
        self, client, db_session
    ):
        sid = "SM" + "a" * 32
        _, redacted = _seed_notification_sent(db_session, sid=sid)
        r = _post(client, {"MessageSid": sid, "MessageStatus": "failed"})
        assert r.status_code == 202
        assert _status_event_count(db_session, sid) == 1
        suppressed = db_session.execute(
            text(
                "SELECT reason, contact_method FROM notification_suppression "
                "WHERE recipient_redacted=:r"
            ), {"r": redacted},
        ).first()
        assert suppressed is not None
        assert suppressed[0] == "hard_bounce"
        assert suppressed[1] == "sms"

    def test_undelivered_21610_records_opt_out(self, client, db_session):
        sid = "SM" + "b" * 32
        _, redacted = _seed_notification_sent(db_session, sid=sid)
        r = _post(client, {
            "MessageSid": sid, "MessageStatus": "undelivered", "ErrorCode": "21610",
        })
        assert r.status_code == 202
        row = db_session.execute(
            text("SELECT reason FROM notification_suppression WHERE recipient_redacted=:r"),
            {"r": redacted},
        ).first()
        assert row is not None and row[0] == "opt_out"

    def test_undelivered_30003_records_invalid_recipient(self, client, db_session):
        sid = "SM" + "c" * 32
        _, redacted = _seed_notification_sent(db_session, sid=sid)
        r = _post(client, {
            "MessageSid": sid, "MessageStatus": "undelivered", "ErrorCode": "30003",
        })
        assert r.status_code == 202
        row = db_session.execute(
            text("SELECT reason FROM notification_suppression WHERE recipient_redacted=:r"),
            {"r": redacted},
        ).first()
        assert row is not None and row[0] == "invalid_recipient"

    def test_undelivered_no_code_is_transient_no_suppression(self, client, db_session):
        sid = "SM" + "d" * 32
        _seed_notification_sent(db_session, sid=sid)
        r = _post(client, {"MessageSid": sid, "MessageStatus": "undelivered"})
        assert r.status_code == 202
        assert _status_event_count(db_session, sid) == 1
        # No suppression for a transient failure.
        n = db_session.execute(
            text("SELECT count(*) FROM notification_suppression")
        ).scalar()
        assert n == 0


class TestInFlight:
    def test_sent_status_emits_event_status_sent(self, client, db_session):
        sid = "SM" + "e" * 32
        _seed_notification_sent(db_session, sid=sid)
        r = _post(client, {"MessageSid": sid, "MessageStatus": "sent"})
        assert r.status_code == 202
        # Look up the recorded status to confirm canonical was "sent".
        row = db_session.execute(
            text(
                "SELECT payload->>'status' FROM platform_events "
                "WHERE event_type='notification_status_updated' AND payload @> :k"
            ),
            {"k": json.dumps({"vendor": "twilio", "vendor_message_id": sid})},
        ).first()
        assert row is not None and row[0] == "sent"


class TestReplay:
    def test_same_sid_and_status_twice_returns_replay(self, client, db_session):
        sid = "SM" + "1" * 32
        _seed_notification_sent(db_session, sid=sid)
        first = _post(client, {"MessageSid": sid, "MessageStatus": "delivered"})
        second = _post(client, {"MessageSid": sid, "MessageStatus": "delivered"})
        assert first.status_code == 202 and first.json()["status"] == "accepted"
        assert second.status_code == 202 and second.json()["status"] == "replay"
        # Only one status event.
        assert _status_event_count(db_session, sid) == 1

    def test_same_sid_different_status_is_not_replay(self, client, db_session):
        """``queued`` then ``delivered`` are distinct deliveries — both accepted."""
        sid = "SM" + "2" * 32
        _seed_notification_sent(db_session, sid=sid)
        first = _post(client, {"MessageSid": sid, "MessageStatus": "sent"})
        second = _post(client, {"MessageSid": sid, "MessageStatus": "delivered"})
        assert first.json()["status"] == "accepted"
        assert second.json()["status"] == "accepted"
        assert _status_event_count(db_session, sid) == 2


class TestOrphan:
    def test_unknown_sid_emits_orphaned(self, client, db_session):
        unknown = "SM" + "9" * 32
        r = _post(client, {"MessageSid": unknown, "MessageStatus": "delivered"})
        assert r.status_code == 202
        assert r.json()["status"] == "orphaned"
        assert _orphan_count(db_session, unknown) == 1
        # No status event written.
        assert _status_event_count(db_session, unknown) == 0


class TestRejections:
    def test_bad_signature_returns_401(self, client, db_session):
        sid = "SM" + "0" * 32
        _seed_notification_sent(db_session, sid=sid)
        # Sign with the wrong token.
        r = _post(
            client, {"MessageSid": sid, "MessageStatus": "delivered"},
            token="WRONG_TOKEN",
        )
        assert r.status_code == 401
        # webhook_rejected emitted.
        n = db_session.execute(
            text("SELECT count(*) FROM platform_events WHERE event_type='webhook_rejected'")
        ).scalar()
        assert n >= 1

    def test_missing_required_field_returns_400(self, client, db_session):
        # No MessageSid. Sign the (broken) form so signature passes.
        form = {"MessageStatus": "delivered"}
        r = _post(client, form)
        assert r.status_code == 400
