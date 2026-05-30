"""Unit tests for the Flinks ``flinks-authenticity-key`` HMAC scheme (P7.2b).

The Flinks scheme has no timestamp header; replay protection is delegated to
the composite nonce derived by the translator. These tests cover the
*signature* algorithm only — base64(HMAC-SHA256(secret, raw_body)).
"""
import base64
import hashlib
import hmac

import pytest

from app.core.config import settings
from app.services.webhooks.signature_verifier import (
    SignatureInvalid,
    SignatureVerifier,
)


@pytest.fixture
def secret(monkeypatch) -> str:
    value = "flinks_test_secret_v2"
    monkeypatch.setattr(settings, "FLINKS_WEBHOOK_SECRET", value)
    return value


def _flinks_sign(raw: bytes, secret: str) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    ).decode("ascii")


class TestVerifyFlinksSignature:
    def test_valid_signature_passes(self, secret):
        raw = b'{"ResponseType":"GetAccountsDetail","Login":{"Id":"l1"},"Tag":"t1"}'
        sig = _flinks_sign(raw, secret)
        verifier = SignatureVerifier(db=None)
        verifier.verify_signature("flinks", raw, {"flinks-authenticity-key": sig})

    def test_header_case_insensitive(self, secret):
        raw = b'{}'
        sig = _flinks_sign(raw, secret)
        verifier = SignatureVerifier(db=None)
        # Starlette lowercases header keys; both spellings must work.
        verifier.verify_signature("flinks", raw, {"Flinks-Authenticity-Key": sig})

    def test_signature_is_base64_not_hex(self, secret):
        # Encode the digest hex by mistake (instead of base64) — must fail.
        raw = b"{}"
        bad_sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        verifier = SignatureVerifier(db=None)
        with pytest.raises(SignatureInvalid):
            verifier.verify_signature("flinks", raw, {"flinks-authenticity-key": bad_sig})

    def test_wrong_secret_raises(self, secret):
        raw = b'{"x":1}'
        sig = _flinks_sign(raw, "WRONG_SECRET")
        verifier = SignatureVerifier(db=None)
        with pytest.raises(SignatureInvalid):
            verifier.verify_signature("flinks", raw, {"flinks-authenticity-key": sig})

    def test_missing_header_raises(self, secret):
        verifier = SignatureVerifier(db=None)
        with pytest.raises(SignatureInvalid):
            verifier.verify_signature("flinks", b"{}", {})

    def test_body_tampering_rejected(self, secret):
        original = b'{"Tag":"original"}'
        sig = _flinks_sign(original, secret)
        tampered = b'{"Tag":"attacker"}'
        verifier = SignatureVerifier(db=None)
        with pytest.raises(SignatureInvalid):
            verifier.verify_signature(
                "flinks", tampered, {"flinks-authenticity-key": sig}
            )
