"""Unit tests for the Didit X-Signature-V2 signature scheme (P7.2b).

The verifier's nonce check is exercised separately via the webhook integration
tests; these tests cover the per-vendor *signature + timestamp* algorithm only,
calling ``SignatureVerifier.verify_signature`` (no DB needed).
"""
import hashlib
import hmac
import json
import time

import pytest

from app.core.config import settings
from app.services.webhooks.signature_verifier import (
    SignatureInvalid,
    SignatureVerifier,
    TimestampExpired,
    _didit_canonicalize,
    _shorten_floats,
)


@pytest.fixture
def secret(monkeypatch) -> str:
    value = "didit_test_secret_v2"
    monkeypatch.setattr(settings, "DIDIT_WEBHOOK_SECRET", value)
    return value


def _didit_sign(body: dict, secret: str) -> str:
    canonical = _didit_canonicalize(body)
    return hmac.new(secret.encode(), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


class TestShortenFloats:
    def test_whole_floats_become_ints(self):
        assert _shorten_floats(1.0) == 1
        assert _shorten_floats(42.0) == 42

    def test_fractional_floats_unchanged(self):
        assert _shorten_floats(1.5) == 1.5
        assert _shorten_floats(96.1) == 96.1

    def test_recurses_into_dicts_and_lists(self):
        out = _shorten_floats({"a": 1.0, "b": [2.0, 3.5]})
        assert out == {"a": 1, "b": [2, 3.5]}

    def test_strings_and_none_untouched(self):
        assert _shorten_floats("hello") == "hello"
        assert _shorten_floats(None) is None


class TestCanonicalize:
    def test_sorts_keys_recursively(self):
        # Same data, different key order — must produce identical canonical strings.
        a = {"b": {"y": 1, "x": 2}, "a": 1}
        b = {"a": 1, "b": {"x": 2, "y": 1}}
        assert _didit_canonicalize(a) == _didit_canonicalize(b)

    def test_unicode_preserved_not_escaped(self):
        # ensure_ascii=False — non-ASCII bytes stay literal.
        assert _didit_canonicalize({"name": "José"}) == '{"name":"José"}'

    def test_compact_separators_no_spaces(self):
        out = _didit_canonicalize({"a": 1, "b": 2})
        assert " " not in out  # compact (",", ":") encoding


class TestVerifyDiditSignature:
    def test_valid_signature_passes(self, secret):
        body = {"event_id": "e1", "status": "Approved", "score": 96.1}
        raw = json.dumps(body).encode()
        ts = str(int(time.time()))
        sig = _didit_sign(body, secret)
        verifier = SignatureVerifier(db=None)
        # Should not raise.
        verifier.verify_signature(
            "didit", raw, {"X-Signature-V2": sig, "X-Timestamp": ts}
        )

    def test_signature_survives_body_key_reordering(self, secret):
        # The same logical body re-serialized with different key order — the
        # canonical-JSON algorithm must produce the same signature.
        body = {"event_id": "e1", "status": "Approved"}
        sig = _didit_sign(body, secret)
        ts = str(int(time.time()))
        # Sender serialized one way; receiver serialized another way.
        wire_body = json.dumps({"status": "Approved", "event_id": "e1"}).encode()
        verifier = SignatureVerifier(db=None)
        verifier.verify_signature(
            "didit", wire_body, {"X-Signature-V2": sig, "X-Timestamp": ts}
        )

    def test_signature_survives_whole_float_normalization(self, secret):
        # Sender sent ``1.0``, signed it as ``1``; receiver receives ``1.0`` on the
        # wire but canonicalizes to ``1`` before verifying — must still pass.
        sig = _didit_sign({"a": 1, "b": 2}, secret)
        ts = str(int(time.time()))
        wire_body = json.dumps({"a": 1.0, "b": 2}).encode()
        verifier = SignatureVerifier(db=None)
        verifier.verify_signature(
            "didit", wire_body, {"X-Signature-V2": sig, "X-Timestamp": ts}
        )

    def test_wrong_secret_raises(self, secret, monkeypatch):
        body = {"event_id": "e1"}
        raw = json.dumps(body).encode()
        ts = str(int(time.time()))
        sig = _didit_sign(body, "WRONG_SECRET")
        verifier = SignatureVerifier(db=None)
        with pytest.raises(SignatureInvalid):
            verifier.verify_signature(
                "didit", raw, {"X-Signature-V2": sig, "X-Timestamp": ts}
            )

    def test_missing_signature_header_raises(self, secret):
        verifier = SignatureVerifier(db=None)
        with pytest.raises(SignatureInvalid):
            verifier.verify_signature(
                "didit", b"{}", {"X-Timestamp": str(int(time.time()))}
            )

    def test_expired_timestamp_raises(self, secret):
        body = {"event_id": "e1"}
        raw = json.dumps(body).encode()
        sig = _didit_sign(body, secret)
        old_ts = str(int(time.time()) - 400)  # outside 5-min window
        verifier = SignatureVerifier(db=None)
        with pytest.raises(TimestampExpired):
            verifier.verify_signature(
                "didit", raw, {"X-Signature-V2": sig, "X-Timestamp": old_ts}
            )

    def test_non_json_body_raises(self, secret):
        ts = str(int(time.time()))
        verifier = SignatureVerifier(db=None)
        with pytest.raises(SignatureInvalid):
            verifier.verify_signature(
                "didit", b"not json", {"X-Signature-V2": "deadbeef", "X-Timestamp": ts}
            )
