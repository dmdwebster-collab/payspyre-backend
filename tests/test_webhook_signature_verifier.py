"""Unit tests for the webhook SignatureVerifier.

P6.6 shipped this file as ``SignatureChecks`` directly probing the internal
``_check_signature`` helper. P7.2b split ``verify()`` per vendor, so the MVP
scheme now lives on ``_verify_mvp`` (used by the ``equifax`` route until a
real Equifax adapter ships); the per-vendor Didit / Flinks signature schemes
are covered in dedicated files (``test_signature_verifier_didit.py`` and
``test_signature_verifier_flinks.py``). This file keeps the MVP / timestamp /
vendor-secret / nonce coverage that has always lived here.
"""
import hashlib
import hmac
import inspect
import time
import uuid

import pytest
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.platform.event import PlatformEvent
from app.services.webhooks.signature_verifier import (
    NonceReplayed,
    SignatureInvalid,
    SignatureVerifier,
    TimestampExpired,
)

_SECRET = "test_equifax_secret"  # MVP scheme is equifax-only post-P7.2b


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


# --- pure checks (no DB) ---------------------------------------------------


class TestEmptySecretFailsClosed:
    """Security: an empty/unset vendor secret must FAIL CLOSED — verification
    must reject, never compute an HMAC with an empty key (which an attacker could
    also compute → forgeable webhooks). Several secrets default to "" (e.g.
    DIDIT_WEBHOOK_SECRET, TWILIO_AUTH_TOKEN)."""

    @pytest.mark.parametrize("vendor", ["didit", "flinks", "equifax", "twilio", "resend"])
    def test_empty_secret_rejected_at_choke_point(self, monkeypatch, vendor):
        for attr in (
            "DIDIT_WEBHOOK_SECRET",
            "FLINKS_WEBHOOK_SECRET",
            "EQUIFAX_WEBHOOK_SECRET",
            "TWILIO_AUTH_TOKEN",
            "RESEND_WEBHOOK_SECRET",
        ):
            monkeypatch.setattr(settings, attr, "")
        with pytest.raises(SignatureInvalid, match="No webhook secret"):
            SignatureVerifier()._get_vendor_secret(vendor)

    def test_didit_empty_secret_fails_via_public_api(self, monkeypatch):
        # End-to-end through verify_signature: empty secret rejects before any
        # HMAC comparison, so a forged X-Signature-V2 can never validate.
        monkeypatch.setattr(settings, "DIDIT_WEBHOOK_SECRET", "")
        with pytest.raises(SignatureInvalid, match="No webhook secret"):
            SignatureVerifier().verify_signature(
                "didit",
                b"{}",
                {"X-Signature-V2": "forged", "X-Timestamp": str(int(time.time()))},
            )


class TestMVPSignatureChecks:
    """Cover the MVP (equifax) X-Signature / X-Timestamp scheme via the public
    ``verify_signature`` API. The Didit and Flinks schemes are covered in their
    own files."""

    def _patch_equifax_secret(self, monkeypatch):
        monkeypatch.setattr(settings, "EQUIFAX_WEBHOOK_SECRET", _SECRET)

    def test_valid_signature_passes(self, monkeypatch):
        self._patch_equifax_secret(monkeypatch)
        body = b'{"x": 1}'
        ts = str(int(time.time()))
        sig = _sign(_SECRET, ts, body)
        SignatureVerifier().verify_signature(
            "equifax", body,
            {"X-Signature": sig, "X-Timestamp": ts, "X-Nonce": "n"},
        )  # no raise

    def test_wrong_signature_raises(self, monkeypatch):
        self._patch_equifax_secret(monkeypatch)
        body = b'{"x": 1}'
        ts = str(int(time.time()))
        with pytest.raises(SignatureInvalid):
            SignatureVerifier().verify_signature(
                "equifax", body,
                {"X-Signature": "deadbeef", "X-Timestamp": ts, "X-Nonce": "n"},
            )

    def test_missing_signature_raises(self, monkeypatch):
        self._patch_equifax_secret(monkeypatch)
        with pytest.raises(SignatureInvalid):
            SignatureVerifier().verify_signature(
                "equifax", b"{}",
                {"X-Timestamp": str(int(time.time())), "X-Nonce": "n"},
            )

    def test_tampered_body_raises(self, monkeypatch):
        self._patch_equifax_secret(monkeypatch)
        ts = str(int(time.time()))
        sig = _sign(_SECRET, ts, b'{"amount": 100}')
        with pytest.raises(SignatureInvalid):
            SignatureVerifier().verify_signature(
                "equifax", b'{"amount": 999}',  # body changed after signing
                {"X-Signature": sig, "X-Timestamp": ts, "X-Nonce": "n"},
            )


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

    def test_mvp_uses_constant_time_compare(self):
        src = inspect.getsource(SignatureVerifier._verify_mvp)
        assert "hmac.compare_digest" in src  # constant-time, not ==

    def test_didit_uses_constant_time_compare(self):
        src = inspect.getsource(SignatureVerifier._verify_didit)
        assert "hmac.compare_digest" in src

    def test_flinks_uses_constant_time_compare(self):
        src = inspect.getsource(SignatureVerifier._verify_flinks)
        assert "hmac.compare_digest" in src


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
