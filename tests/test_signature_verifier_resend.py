"""Unit tests for the Resend (Svix) signature scheme (P7.4b).

Stdlib HMAC-SHA256 over ``{svix-id}.{svix-timestamp}.{body}`` with the
base64-decoded endpoint secret. The verifier rejects expired timestamps,
malformed secrets, and signature mismatches.
"""
import base64
import hashlib
import hmac
import time

import pytest

from app.core.config import settings
from app.services.webhooks.signature_verifier import (
    SignatureInvalid,
    SignatureVerifier,
    TimestampExpired,
)

# whsec_<base64-of-secret-key> — same format Resend ships in the dashboard.
_RAW_KEY = b"resend_test_secret_key_bytes!!"
_WHSEC_SECRET = "whsec_" + base64.b64encode(_RAW_KEY).decode("ascii")


@pytest.fixture(autouse=True)
def _patch_secret(monkeypatch):
    monkeypatch.setattr(settings, "RESEND_WEBHOOK_SECRET", _WHSEC_SECRET)


def _sign(svix_id: str, svix_timestamp: str, body: bytes,
          key: bytes = _RAW_KEY) -> str:
    signed = f"{svix_id}.{svix_timestamp}.{body.decode('utf-8')}".encode("utf-8")
    digest = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")
    return f"v1,{digest}"


class TestResend:
    def test_valid_signature_passes(self):
        body = b'{"type":"email.delivered","data":{"email_id":"r1"}}'
        ts = str(int(time.time()))
        sig = _sign("msg_test1", ts, body)
        SignatureVerifier().verify_signature(
            "resend", body,
            {"svix-id": "msg_test1", "svix-timestamp": ts, "svix-signature": sig},
        )

    def test_multiple_signatures_picks_first_match(self):
        body = b"{}"
        ts = str(int(time.time()))
        good = _sign("msg_x", ts, body)
        bad = "v1,QUJDREVGRw=="  # arbitrary other base64
        SignatureVerifier().verify_signature(
            "resend", body,
            {"svix-id": "msg_x", "svix-timestamp": ts,
             "svix-signature": f"{bad} {good}"},
        )

    def test_expired_timestamp_fails(self):
        body = b"{}"
        ts = str(int(time.time()) - 400)  # > 5 min ago
        sig = _sign("msg_x", ts, body)
        with pytest.raises(TimestampExpired):
            SignatureVerifier().verify_signature(
                "resend", body,
                {"svix-id": "msg_x", "svix-timestamp": ts, "svix-signature": sig},
            )

    def test_tampered_body_fails(self):
        original = b'{"a":1}'
        ts = str(int(time.time()))
        sig = _sign("msg_x", ts, original)
        with pytest.raises(SignatureInvalid):
            SignatureVerifier().verify_signature(
                "resend", b'{"a":2}',
                {"svix-id": "msg_x", "svix-timestamp": ts, "svix-signature": sig},
            )

    def test_missing_svix_id_fails(self):
        body = b"{}"
        ts = str(int(time.time()))
        sig = _sign("msg_x", ts, body)
        with pytest.raises(SignatureInvalid, match="svix-id"):
            SignatureVerifier().verify_signature(
                "resend", body,
                {"svix-timestamp": ts, "svix-signature": sig},
            )

    def test_missing_signature_header_fails(self):
        body = b"{}"
        ts = str(int(time.time()))
        with pytest.raises(SignatureInvalid, match="svix-signature"):
            SignatureVerifier().verify_signature(
                "resend", body,
                {"svix-id": "x", "svix-timestamp": ts},
            )

    def test_invalid_secret_format_fails(self, monkeypatch):
        monkeypatch.setattr(settings, "RESEND_WEBHOOK_SECRET", "not_a_whsec_secret")
        body = b"{}"
        ts = str(int(time.time()))
        sig = _sign("msg_x", ts, body)
        with pytest.raises(SignatureInvalid, match="whsec_"):
            SignatureVerifier().verify_signature(
                "resend", body,
                {"svix-id": "msg_x", "svix-timestamp": ts, "svix-signature": sig},
            )

    def test_wrong_key_fails(self):
        body = b"{}"
        ts = str(int(time.time()))
        sig = _sign("msg_x", ts, body, key=b"different_key_entirely!")
        with pytest.raises(SignatureInvalid, match="mismatch"):
            SignatureVerifier().verify_signature(
                "resend", body,
                {"svix-id": "msg_x", "svix-timestamp": ts, "svix-signature": sig},
            )
