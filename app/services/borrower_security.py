"""Borrower 2FA step-up service (WS-J item 1 — Dave's bank-level mandate).

A second factor layered ON TOP of the magic-link login, required for SENSITIVE
actions only (bank-detail changes, profile edits, payments). Strictly additive:

* A patient with no enrollment (and not staff-enforced) sees zero change.
* An ENROLLED (active) patient must present a short-lived step-up token —
  minted by ``verify_step_up`` after a fresh code check — via the
  ``X-Step-Up-Token`` header on sensitive endpoints.
* A staff-ENFORCED but unenrolled patient is refused sensitive actions until
  they enroll (enrollment becomes mandatory).

Methods:

* ``totp`` — RFC-6238 TOTP implemented on the stdlib (hmac/struct/base64), no
  new dependency. The base32 secret is returned ONCE at enrollment and never
  readable again through any API.
* ``sms`` — the Twilio-creds-pending seam: 6-digit codes generated here, sent
  through a SIMULATOR sender that persists only a SHA-256 hash in
  ``platform_events`` (mirroring the magic-link machinery: issuance /
  consumption / failure events, per-patient lockout). Flipping to live SMS is
  a sender swap, not a redesign.

Step-up tokens are HS256 JWTs signed with ``settings.PATIENT_JWT_SECRET``,
``purpose="step_up"``, 10-minute TTL, bound to the patient id.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time as time_mod
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from jose import JWTError, jwt
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.platform.borrower_portal import PlatformPatientSecondFactor
from app.models.platform.event import PlatformEvent

logger = get_logger(__name__)

STEP_UP_TTL_SECONDS = 600  # 10 min — one sensitive action window
STEP_UP_PURPOSE = "step_up"
_JWT_ALGORITHM = "HS256"

SMS_CODE_TTL_SECONDS = 300
_SMS_CODE_DIGITS = 6
_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_WINDOW_SECONDS = 900  # 15 min

_TOTP_PERIOD_SECONDS = 30
_TOTP_DIGITS = 6
_TOTP_DRIFT_STEPS = 1  # accept ±1 period of clock drift

# Dev-only: last plaintext 2FA code per patient id (simulator mode). Mirrors
# mock_notification_dispatcher._DEV_PLAINTEXT_CODES — only a hash is persisted,
# so local demos need a gated dev peek. Never populated by a live sender.
_DEV_2FA_CODES: dict[str, str] = {}


def peek_dev_2fa_code(patient_id: str) -> str | None:
    """Return the last plaintext 2FA code for a patient (dev tools only)."""
    return _DEV_2FA_CODES.get(patient_id)


class TwoFactorError(Exception):
    """Base class for 2FA errors."""


class TwoFactorInvalidCode(TwoFactorError):
    """Wrong / expired / replayed code (→ 401)."""


class TwoFactorLocked(TwoFactorError):
    """Too many failed attempts in the window (→ 429)."""


class TwoFactorStateError(TwoFactorError):
    """Enrollment state doesn't allow the operation (→ 409)."""


# ---------------------------------------------------------------------------
# TOTP (RFC 6238 / HOTP RFC 4226) — stdlib-only, no new dependency.
# ---------------------------------------------------------------------------


def generate_totp_secret() -> str:
    """A fresh 160-bit base32 secret (standard authenticator-app size)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def hotp(secret_b32: str, counter: int, digits: int = _TOTP_DIGITS) -> str:
    """RFC-4226 HOTP value for a base32 secret + counter."""
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code_int % (10**digits)).zfill(digits)


def totp_at(secret_b32: str, at_unix: float, digits: int = _TOTP_DIGITS) -> str:
    """RFC-6238 TOTP value at a unix timestamp (30s period)."""
    return hotp(secret_b32, int(at_unix // _TOTP_PERIOD_SECONDS), digits)


def verify_totp(secret_b32: str, code: str, at_unix: Optional[float] = None) -> bool:
    """Constant-time TOTP check with ±1 period of clock drift."""
    if at_unix is None:
        at_unix = time_mod.time()
    counter = int(at_unix // _TOTP_PERIOD_SECONDS)
    normalized = (code or "").strip()
    for drift in range(-_TOTP_DRIFT_STEPS, _TOTP_DRIFT_STEPS + 1):
        if hmac.compare_digest(hotp(secret_b32, counter + drift), normalized):
            return True
    return False


def totp_provisioning_uri(secret_b32: str, account_label: str) -> str:
    """The otpauth:// URI an authenticator app enrolls from (QR payload)."""
    return (
        f"otpauth://totp/PaySpyre:{account_label}?secret={secret_b32}"
        f"&issuer=PaySpyre&digits={_TOTP_DIGITS}&period={_TOTP_PERIOD_SECONDS}"
    )


# ---------------------------------------------------------------------------
# Step-up tokens (short-lived, purpose-bound JWTs)
# ---------------------------------------------------------------------------


def issue_step_up_token(patient_id: UUID, now: Optional[datetime] = None) -> tuple[str, datetime]:
    """Mint a 10-minute step-up JWT bound to the patient."""
    now = now or datetime.now(timezone.utc)
    iat = int(now.timestamp())
    exp = iat + STEP_UP_TTL_SECONDS
    claims = {"sub": str(patient_id), "purpose": STEP_UP_PURPOSE, "iat": iat, "exp": exp}
    token = jwt.encode(claims, settings.PATIENT_JWT_SECRET, algorithm=_JWT_ALGORITHM)
    return token, datetime.fromtimestamp(exp, tz=timezone.utc)


def validate_step_up_token(token: str, patient_id: UUID) -> bool:
    """True iff the token is a live step-up JWT for THIS patient.

    The purpose claim keeps a (24h) patient session JWT from doubling as a
    step-up proof, and the sub check keeps one patient's step-up from being
    replayed against another's session.
    """
    try:
        payload = jwt.decode(token, settings.PATIENT_JWT_SECRET, algorithms=[_JWT_ALGORITHM])
    except JWTError:
        return False
    return payload.get("purpose") == STEP_UP_PURPOSE and payload.get("sub") == str(patient_id)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def _generate_sms_code() -> str:
    return str(secrets.randbelow(10**_SMS_CODE_DIGITS)).zfill(_SMS_CODE_DIGITS)


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


class TwoFactorService:
    """Enrollment, challenges, and verification for borrower 2FA."""

    def __init__(self, db: Session) -> None:
        self.db = db

    # -- state ---------------------------------------------------------------

    def get_state(self, patient_id: UUID) -> Optional[PlatformPatientSecondFactor]:
        return (
            self.db.query(PlatformPatientSecondFactor)
            .filter(PlatformPatientSecondFactor.patient_id == patient_id)
            .first()
        )

    def step_up_required(self, patient_id: UUID) -> bool:
        """Sensitive actions need a step-up iff 2FA is active OR staff-enforced."""
        row = self.get_state(patient_id)
        if row is None:
            return False
        return row.status == "active" or row.enforced

    # -- enrollment ----------------------------------------------------------

    def enroll(
        self,
        patient_id: UUID,
        method: str,
        sms_phone_e164: Optional[str] = None,
        account_label: str = "borrower",
    ) -> dict:
        """Start (or restart) enrollment. Never downgrades an ACTIVE enrollment
        implicitly — re-enrolling while active requires a fresh step-up at the
        endpoint layer (this service only refuses nothing here; the row is
        replaced so a lost authenticator can be re-keyed by the same patient).
        """
        if method not in ("totp", "sms"):
            raise TwoFactorStateError("method must be 'totp' or 'sms'")
        if method == "sms":
            phone = (sms_phone_e164 or "").strip()
            if not phone:
                raise TwoFactorStateError("sms enrollment requires sms_phone_e164")

        row = self.get_state(patient_id)
        if row is None:
            row = PlatformPatientSecondFactor(patient_id=patient_id, method=method)
            self.db.add(row)

        row.method = method
        row.status = "pending"
        row.enrolled_at = None
        result: dict = {"method": method, "status": "pending"}

        if method == "totp":
            secret = generate_totp_secret()
            row.totp_secret = secret
            row.sms_phone_e164 = None
            # The ONLY time the secret leaves the server.
            result["totp_secret"] = secret
            result["otpauth_uri"] = totp_provisioning_uri(secret, account_label)
        else:
            row.totp_secret = None
            row.sms_phone_e164 = sms_phone_e164.strip()
            self._send_sms_code(patient_id, purpose="enroll")
            result["message"] = "A verification code was sent to your phone."

        self.db.commit()
        logger.info("second_factor_enroll_started", patient_id=str(patient_id), method=method)
        return result

    def activate(self, patient_id: UUID, code: str) -> dict:
        """Verify the first code and flip the enrollment to ACTIVE."""
        row = self.get_state(patient_id)
        if row is None:
            raise TwoFactorStateError("No 2FA enrollment in progress")
        self._check_code(patient_id, row, code)
        row.status = "active"
        row.enrolled_at = datetime.now(timezone.utc)
        self.db.commit()
        logger.info("second_factor_enrolled", patient_id=str(patient_id), method=row.method)
        return {"method": row.method, "status": "active"}

    # -- step-up -------------------------------------------------------------

    def challenge(self, patient_id: UUID) -> dict:
        """Kick off a step-up challenge (sends an SMS code when method=sms)."""
        row = self.get_state(patient_id)
        if row is None or row.status != "active":
            raise TwoFactorStateError("2FA is not active for this account")
        if row.method == "sms":
            self._send_sms_code(patient_id, purpose="step_up")
            self.db.commit()
            return {"method": "sms", "message": "A verification code was sent to your phone."}
        return {"method": "totp", "message": "Enter the code from your authenticator app."}

    def verify_step_up(self, patient_id: UUID, code: str) -> dict:
        """Verify a fresh code and mint a short-lived step-up token."""
        row = self.get_state(patient_id)
        if row is None or row.status != "active":
            raise TwoFactorStateError("2FA is not active for this account")
        self._check_code(patient_id, row, code)
        token, expires_at = issue_step_up_token(patient_id)
        logger.info("second_factor_step_up_issued", patient_id=str(patient_id))
        return {"step_up_token": token, "expires_at": expires_at.isoformat()}

    # -- internals -----------------------------------------------------------

    def _check_code(self, patient_id: UUID, row: PlatformPatientSecondFactor, code: str) -> None:
        """Verify a TOTP or SMS code with lockout accounting."""
        self._enforce_lockout(patient_id)
        if row.method == "totp":
            if not row.totp_secret or not verify_totp(row.totp_secret, code):
                self._record_failed(patient_id)
                raise TwoFactorInvalidCode("Invalid verification code")
            return
        self._consume_sms_code(patient_id, code)

    def _send_sms_code(self, patient_id: UUID, purpose: str) -> None:
        """SIMULATOR sender: persist a hashed code event; plaintext only in the
        in-process dev peek (never in a response, never when a real sender is
        wired). The live Twilio sender slots in here once creds arrive."""
        code = _generate_sms_code()
        ttl_expires_at = datetime.now(timezone.utc) + timedelta(seconds=SMS_CODE_TTL_SECONDS)
        self.db.add(
            PlatformEvent(
                event_type="second_factor_code_issued",
                actor="system",
                patient_id=patient_id,
                payload={
                    "v": 1,
                    "actor": {"type": "system", "id": "system"},
                    "patient_id": str(patient_id),
                    "purpose": purpose,
                    "code_hash": _hash_code(code),
                    "ttl_expires_at": ttl_expires_at.isoformat(),
                    "channel": "sms_simulator",
                },
            )
        )
        self.db.flush()
        _DEV_2FA_CODES[str(patient_id)] = code
        logger.info("second_factor_code_sent", patient_id=str(patient_id), purpose=purpose)

    def _consume_sms_code(self, patient_id: UUID, code: str) -> None:
        """Single-use, TTL-bound SMS-code check against the event log."""
        issued = self.db.execute(
            text(
                """
                SELECT id, payload FROM platform_events
                WHERE event_type = 'second_factor_code_issued'
                  AND payload @> :key
                ORDER BY occurred_at DESC
                LIMIT 1
                """
            ),
            {
                "key": json.dumps(
                    {"code_hash": _hash_code((code or "").strip()), "patient_id": str(patient_id)}
                )
            },
        ).first()
        if issued is None:
            self._record_failed(patient_id)
            raise TwoFactorInvalidCode("Invalid verification code")

        issued_id, payload = issued
        if datetime.now(timezone.utc) > datetime.fromisoformat(payload["ttl_expires_at"]):
            raise TwoFactorInvalidCode("Verification code expired")

        already = self.db.execute(
            text(
                """
                SELECT id FROM platform_events
                WHERE event_type = 'second_factor_code_consumed'
                  AND payload @> :key
                LIMIT 1
                """
            ),
            {"key": json.dumps({"issued_event_id": issued_id})},
        ).first()
        if already is not None:
            raise TwoFactorInvalidCode("Verification code already used")

        self.db.add(
            PlatformEvent(
                event_type="second_factor_code_consumed",
                actor="patient",
                patient_id=patient_id,
                payload={
                    "v": 1,
                    "actor": {"type": "patient", "id": str(patient_id)},
                    "patient_id": str(patient_id),
                    "issued_event_id": issued_id,
                },
            )
        )
        self.db.flush()

    def _enforce_lockout(self, patient_id: UUID) -> None:
        since = datetime.now(timezone.utc) - timedelta(seconds=_LOCKOUT_WINDOW_SECONDS)
        count = self.db.execute(
            text(
                """
                SELECT count(*) FROM platform_events
                WHERE event_type = 'second_factor_failed'
                  AND patient_id = :pid
                  AND occurred_at >= :since
                """
            ),
            {"pid": str(patient_id), "since": since},
        ).scalar()
        if count is not None and count >= _MAX_FAILED_ATTEMPTS:
            logger.warning("second_factor_locked", patient_id=str(patient_id), failed=count)
            raise TwoFactorLocked("Too many failed attempts. Try again later.")

    def _record_failed(self, patient_id: UUID) -> None:
        self.db.add(
            PlatformEvent(
                event_type="second_factor_failed",
                actor="patient",
                patient_id=patient_id,
                payload={
                    "v": 1,
                    "actor": {"type": "patient", "id": str(patient_id)},
                    "patient_id": str(patient_id),
                    "reason": "invalid_code",
                },
            )
        )
        self.db.commit()
