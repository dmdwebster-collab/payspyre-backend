"""Vendor webhook HMAC signature verification (P6.6 + P7.2b).

This module dispatches per-vendor signature schemes; each vendor uses its own
header(s) and canonicalization (no shared "X-Signature" envelope outside of the
``equifax`` MVP path). Three guards always run, in this order: vendor-specific
signature check, vendor-specific timestamp/freshness check (when the vendor
provides one), and nonce replay via ``platform_events`` (no new table).

Schemes:

- ``didit`` (P7.2b) — Didit-recommended ``X-Signature-V2``: HMAC-SHA256 over
  ``JSON.stringify(sortKeys(shortenFloats(parsed_body)))``, hex-encoded.
  ``X-Timestamp`` (unix seconds) is enforced inside a 5-minute window. The
  per-delivery ``event_id`` (parsed from the body by the endpoint, passed in
  here as ``vendor_event_id``) acts as the idempotency nonce.
  Ref: https://docs.didit.me/integration/webhooks

- ``flinks`` (P7.2b) — ``flinks-authenticity-key``: standard-Base64 of
  ``HMAC-SHA256(secret, raw_body_utf8)``. Flinks does not ship a timestamp
  header — replay protection relies on the composite nonce
  ``"flinks:<Login.Id>:<ResponseType>"`` (each combo is delivered once per
  session; retries reuse it and are no-ops via the nonce check).
  Ref: https://docs.flinks.com/reference/hmac

- ``equifax`` (P6.6 MVP) — the original ``X-Signature`` /
  ``X-Timestamp`` / ``X-Nonce`` envelope: HMAC-SHA256 of
  ``X-Timestamp + "." + raw_body``. Stays until a real Equifax adapter ships.

Stdlib only (``hmac`` + ``hashlib`` + ``base64`` + ``json``).
Secrets are read from ``settings`` at call time so tests can monkeypatch them.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Callable, Optional

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
# DIDIT reuses the pre-existing DIDIT_WEBHOOK_SECRET (shared with V1 KYC).
_VENDOR_SECRET_GETTERS: dict[str, Callable[[], str]] = {
    "didit": lambda: settings.DIDIT_WEBHOOK_SECRET,
    "flinks": lambda: settings.FLINKS_WEBHOOK_SECRET,
    "equifax": lambda: settings.EQUIFAX_WEBHOOK_SECRET,
}


# ---------------------------------------------------------------------------
# Didit canonical-JSON helpers (X-Signature-V2)
# ---------------------------------------------------------------------------


def _shorten_floats(value: Any) -> Any:
    """Convert whole-valued floats to ints; recurse into dicts/lists.

    Mirrors Didit's documented ``shortenFloats`` step. ``1.0`` becomes ``1``
    so the canonical string matches what Didit signed regardless of which
    JSON encoder it ran through.
    """
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return value
    if isinstance(value, dict):
        return {k: _shorten_floats(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shorten_floats(v) for v in value]
    return value


def _didit_canonicalize(parsed_body: Any) -> str:
    """Sort keys recursively + shortenFloats + compact JSON, Unicode-preserved."""
    shortened = _shorten_floats(parsed_body)
    return json.dumps(shortened, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


class SignatureVerifier:
    REPLAY_WINDOW_SECONDS = 300  # 5 minutes (Didit + Equifax MVP only)

    def __init__(self, db: Optional[Session] = None) -> None:
        self.db = db

    def verify_signature(
        self,
        vendor: str,
        raw_body: bytes,
        headers: dict[str, str],
    ) -> None:
        """Vendor-specific signature + timestamp guards. Does NOT touch the DB.

        Call this BEFORE parsing the body — a signature failure means the body
        is untrusted and should never be parsed/persisted. The nonce check is
        separate (``check_nonce``) because real vendors carry their nonce in
        the body, which can only be safely read after the signature verifies.
        """
        secret = self._get_vendor_secret(vendor)
        if vendor == "didit":
            self._verify_didit(secret, raw_body, headers)
        elif vendor == "flinks":
            self._verify_flinks(secret, raw_body, headers)
        elif vendor == "equifax":
            self._verify_mvp(secret, raw_body, headers)
        else:  # pragma: no cover — guarded by SUPPORTED_VENDORS in deps.py
            raise SignatureInvalid(f"Unknown vendor '{vendor}'")

    def check_nonce(self, vendor_event_id: str) -> None:
        """Replay check against the ``platform_events`` log. Call AFTER signature
        verifies + the body is parsed (so the vendor's own nonce can be used)."""
        self._check_nonce(vendor_event_id)

    def _get_vendor_secret(self, vendor: str) -> str:
        getter = _VENDOR_SECRET_GETTERS.get(vendor)
        if getter is None:
            raise SignatureInvalid(f"Unknown vendor '{vendor}'")
        return getter()

    # -- per-vendor schemes -------------------------------------------------

    def _verify_didit(self, secret: str, raw_body: bytes, headers: dict[str, str]) -> None:
        x_signature = _header(headers, "X-Signature-V2")
        x_timestamp = _header(headers, "X-Timestamp")
        if not x_signature:
            raise SignatureInvalid("Missing X-Signature-V2")
        self._check_timestamp(x_timestamp)
        # Re-parse the body and canonicalize. If the body isn't valid JSON we
        # surface that as a signature failure (an unsigned body can't be trusted).
        try:
            parsed = json.loads(raw_body)
        except (ValueError, TypeError):
            raise SignatureInvalid("Body is not valid JSON")
        canonical = _didit_canonicalize(parsed)
        expected = hmac.new(
            secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, x_signature):
            raise SignatureInvalid("Signature mismatch")

    def _verify_flinks(self, secret: str, raw_body: bytes, headers: dict[str, str]) -> None:
        provided = _header(headers, "flinks-authenticity-key")
        if not provided:
            raise SignatureInvalid("Missing flinks-authenticity-key")
        # Flinks ships no timestamp header — replay protection lives in the
        # composite nonce (see translate_flinks_payload).
        expected = base64.b64encode(
            hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
        ).decode("ascii")
        if not hmac.compare_digest(expected, provided):
            raise SignatureInvalid("Signature mismatch")

    def _verify_mvp(self, secret: str, raw_body: bytes, headers: dict[str, str]) -> None:
        """The original P6.6 envelope — kept for ``equifax`` until a real adapter lands."""
        x_signature = _header(headers, "X-Signature")
        x_timestamp = _header(headers, "X-Timestamp")
        if not x_signature:
            raise SignatureInvalid("Missing X-Signature")
        self._check_timestamp(x_timestamp)
        sig_input = x_timestamp.encode() + b"." + raw_body
        expected = hmac.new(secret.encode(), sig_input, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, x_signature):
            raise SignatureInvalid("Signature mismatch")

    # -- shared guards ------------------------------------------------------

    def _check_timestamp(self, x_timestamp: str) -> None:
        try:
            ts = int(x_timestamp)
        except (TypeError, ValueError):
            raise TimestampExpired("Missing or invalid X-Timestamp")
        if abs(time.time() - ts) > self.REPLAY_WINDOW_SECONDS:
            raise TimestampExpired("X-Timestamp outside the replay window")

    def _check_nonce(self, nonce: str) -> None:
        if not nonce:
            raise SignatureInvalid("Missing vendor_event_id (nonce)")
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


def _header(headers: dict[str, str], name: str) -> str:
    """Case-insensitive header lookup; Starlette already lowercases keys."""
    if name in headers:
        return headers[name]
    lowered = name.lower()
    if lowered in headers:
        return headers[lowered]
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return ""
