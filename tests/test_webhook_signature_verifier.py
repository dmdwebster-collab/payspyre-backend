"""Unit tests for the webhook SignatureVerifier (P6.6). Mostly pure; nonce tests use DB."""
import hashlib
import hmac
import inspect
import time
import uuid

import pytest
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.platform.event import PlatformEvent
from app.services.webhooks import signature_verifier as sv_module
from app.services.webhooks.signature_verifier import (
    NonceReplayed,
    SignatureInvalid,
    SignatureVerifier,
    TimestampExpired,
)

_SECRET = "test_didit_secret"


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


# --- pure checks (no DB) ---------------------------------------------------


class TestSignatureChecks:
    def test_valid_signature_passes(self):
        v = SignatureVerifier()
        body = b'{"x": 1}'
        ts = str(int(time.time()))
        v._check_signature(_SECRET, body, ts, _sign(_SECRET, ts, body))  # no raise

    def test_wrong_signature_raises(self):
        v = SignatureVerifier()
        body = b'{"x": 1}'
        ts = str(int(time.time()))
        with pytest.raises(SignatureInvalid):
            v._check_signature(_SECRET, body, ts, "deadbeef")

    def test_missing_signature_raises(self):
        v = SignatureVerifier()
        with pytest.raises(SignatureInvalid):
            v._check_signature(_SECRET, b"{}", str(int(time.time())), "")

    def test_tampered_body_raises(self):
        v = SignatureVerifier()
        ts = str(int(time.time()))
        sig = _sign(_SECRET, ts, b'{"amount": 100}')
        with pytest.raises(SignatureInvalid):
            v._check_signature(_SECRET, b'{"amount": 999}', ts, sig)  # body changed after signing


class TestTimestampChecks:
    def test_valid_timestamp_passes(self):
        SignatureVerifier()._check_timestamp(str(int(time.time())))  # no raise

    def test_expired_timestamp_raises(self):
        with pytest.raises(TimestampExpired):
            SignatureVerifier()._check_timestamp(str(int(time.time()) - 400))

    def test_future_timestamp_raises(self):
        with pytest.raises(TimestampExpired):
            SignatureVerifier()._check_timestamp(str(int(time.time()) + 600))

    def test_unparseable_timestamp_raises(self):
        with pytest.raises(TimestampExpired):
            SignatureVerifier()._check_timestamp("not-a-number")


class TestVendorSecret:
    def test_unknown_vendor_raises(self):
        with pytest.raises(SignatureInvalid):
            SignatureVerifier()._get_vendor_secret("unknown_corp")

    def test_known_vendor_returns_secret(self, monkeypatch):
        monkeypatch.setattr(settings, "FLINKS_WEBHOOK_SECRET", "test_flinks_secret")
        assert SignatureVerifier()._get_vendor_secret("flinks") == "test_flinks_secret"

    def test_uses_constant_time_compare(self):
        src = inspect.getsource(SignatureVerifier._check_signature)
        assert "hmac.compare_digest" in src  # constant-time, not ==


# --- nonce replay (DB) -----------------------------------------------------


class TestNonceCheck:
    def test_fresh_nonce_passes(self, db_session: Session):
        SignatureVerifier(db_session)._check_nonce(str(uuid.uuid4()))  # no raise

    def test_replayed_nonce_raises(self, db_session: Session):
        nonce = str(uuid.uuid4())
        db_session.add(
            PlatformEvent(
                event_type="webhook_received",
                actor="vendor",
                payload={"vendor_event_id": nonce, "vendor": "didit"},
            )
        )
        db_session.commit()
        with pytest.raises(NonceReplayed):
            SignatureVerifier(db_session)._check_nonce(nonce)
