"""Vendor webhook HMAC-SHA256 signature verification (P6.6).

Stdlib only (``hmac`` + ``hashlib``) — no third-party crypto. The signature is
computed by the vendor as::

    HMAC-SHA256(vendor_secret, X-Timestamp + "." + raw_body_bytes)

and delivered hex-encoded in ``X-Signature``. Verification has three guards, in
order: timestamp window (5 min), constant-time signature comparison, and nonce
replay (via ``platform_events`` — no new table). Unknown vendor → SignatureInvalid.

Secrets are read from ``settings`` at call time so tests can monkeypatch them.
DIDIT reuses the pre-existing ``DIDIT_WEBHOOK_SECRET`` (shared with V1 KYC).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Callable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings


class SignatureVerificationError(Exception):
    """Base class for webhook signature/replay failures."""


class SignatureInvalid(SignatureVerificationError):
    """Unknown vendor, missing signature, or HMAC mismatch."""


class TimestampExpired(SignatureVerificationError):
    """Timestamp missing, unparseable, or outside the replay window."""


class NonceReplayed(SignatureVerificationError):
    """A webhook with this nonce was already accepted."""


# Read secrets from `settings` at call time (so monkeypatch works in tests).
_VENDOR_SECRET_GETTERS: dict[str, Callable[[], str]] = {
    "didit": lambda: settings.DIDIT_WEBHOOK_SECRET,
    "flinks": lambda: settings.FLINKS_WEBHOOK_SECRET,
    "equifax": lambda: settings.EQUIFAX_WEBHOOK_SECRET,
}


class SignatureVerifier:
    REPLAY_WINDOW_SECONDS = 300  # 5 minutes

    def __init__(self, db: Optional[Session] = None) -> None:
        self.db = db

    def verify(
        self,
        vendor: str,
        raw_body: bytes,
        x_signature: str,
        x_timestamp: str,
        x_nonce: str,
    ) -> None:
        """Run all guards in order. Raises a SignatureVerificationError subclass on failure."""
        secret = self._get_vendor_secret(vendor)
        self._check_timestamp(x_timestamp)
        self._check_signature(secret, raw_body, x_timestamp, x_signature)
        self._check_nonce(x_nonce)

    def _get_vendor_secret(self, vendor: str) -> str:
        getter = _VENDOR_SECRET_GETTERS.get(vendor)
        if getter is None:
            raise SignatureInvalid(f"Unknown vendor '{vendor}'")
        return getter()

    def _check_timestamp(self, x_timestamp: str) -> None:
        try:
            ts = int(x_timestamp)
        except (TypeError, ValueError):
            raise TimestampExpired("Missing or invalid X-Timestamp")
        if abs(time.time() - ts) > self.REPLAY_WINDOW_SECONDS:
            raise TimestampExpired("X-Timestamp outside the replay window")

    def _check_signature(
        self, secret: str, raw_body: bytes, x_timestamp: str, x_signature: str
    ) -> None:
        if not x_signature:
            raise SignatureInvalid("Missing X-Signature")
        sig_input = x_timestamp.encode() + b"." + raw_body
        expected = hmac.new(secret.encode(), sig_input, hashlib.sha256).hexdigest()
        # constant-time comparison (prevents timing attacks)
        if not hmac.compare_digest(expected, x_signature):
            raise SignatureInvalid("Signature mismatch")

    def _check_nonce(self, nonce: str) -> None:
        if self.db is None:
            raise RuntimeError("SignatureVerifier needs a db session for the nonce check")
        row = self.db.execute(
            text(
                """
                SELECT id FROM platform_events
                WHERE event_type = 'webhook_received'
                  AND payload @> :key
                LIMIT 1
                """
            ),
            {"key": json.dumps({"vendor_event_id": nonce})},
        ).first()
        if row is not None:
            raise NonceReplayed("Webhook nonce already processed")
