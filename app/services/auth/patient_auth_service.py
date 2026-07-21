"""Patient auth service (P6.5) — magic-link request/exchange + JWT issuance.

Tokens are 6-char uppercase alphanumeric, stored only as SHA-256 hashes in
``platform_events`` (no migration). Because ``platform_events`` is append-only
(migration-021 WORM trigger — no UPDATE), replay protection is signalled by the
*presence* of a ``magic_link_consumed`` event referencing the issued event's id,
not by mutating the issued row.

JWT: HS256 via ``python-jose`` (already in pyproject; do NOT use pyjwt), signed
with ``settings.PATIENT_JWT_SECRET``, 24h expiry, ``app_ids`` = the patient's
non-terminal applications at issuance time.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from jose import jwt
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.event import PlatformEvent
from app.models.platform.patient import PlatformPatient
from app.services.mock_notification_dispatcher import MockNotificationDispatcher

logger = get_logger(__name__)

JWT_TTL_SECONDS = 86_400  # 24h
JWT_ALGORITHM = "HS256"
_TERMINAL_STATUSES = ("withdrawn", "expired")
_TOKEN_ALPHABET = string.ascii_uppercase + string.digits

# Magic-link token length. 8 chars over a 36-symbol alphabet (~2.8e12 space) vs
# the prior 6 (~2.2e9). Tunable; bump higher (or switch to an emailed
# token_urlsafe link) if UX allows. Brute-force is bounded primarily by the
# per-application attempt lockout below, not by length alone (security #5b).
_TOKEN_LENGTH = 8

# Per-application attempt lockout (security #5b): after this many failed exchange
# attempts (wrong token) for one application within the window, further exchanges
# are refused until a new code is requested. Bounds online brute-forcing of the
# code even if an attacker rotates IPs (per-IP limiting is separate). Counted via
# append-only magic_link_failed events — no new table/migration.
_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_WINDOW_SECONDS = 900  # 15 minutes


class MagicLinkError(Exception):
    """Base class for magic-link errors."""


class InvalidMagicLinkToken(MagicLinkError):
    """Token not found, expired, or already consumed (→ 401)."""


class MagicLinkLocked(MagicLinkError):
    """Too many failed attempts for this application in the window (→ 429)."""


class ApplicationNotFound(MagicLinkError):
    """No application for the given id (→ 404)."""


def _generate_token() -> str:
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(_TOKEN_LENGTH))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class PatientAuthService:
    def __init__(self, db: Session, notification_dispatcher: MockNotificationDispatcher) -> None:
        self.db = db
        self.dispatcher = notification_dispatcher

    def request_magic_link(
        self,
        application_id: UUID,
        contact_method: Literal["sms", "email"],
    ) -> dict:
        """Generate a token, dispatch it (mock), persist the issuance event.

        Resolves the patient from the application so callers only pass the
        application id.
        """
        app = (
            self.db.query(PlatformCreditApplication)
            .filter(PlatformCreditApplication.id == application_id)
            .first()
        )
        if app is None:
            raise ApplicationNotFound(f"Application {application_id} not found")
        token = _generate_token()
        self.dispatcher.send_magic_link(app.patient_id, application_id, contact_method, token)
        self.db.commit()
        logger.info(
            "magic_link_requested",
            patient_id=str(app.patient_id),
            application_id=str(application_id),
            contact_method=contact_method,
        )
        return {"contact_method": contact_method, "message": "Code sent."}

    def exchange_magic_link(self, application_id: UUID, token: str) -> dict:
        """Validate a token, mark it consumed, and return a signed JWT session."""
        self._enforce_attempt_lockout(application_id)
        token_hash = _hash_token(token)
        issued = self.db.execute(
            text(
                """
                SELECT id, payload FROM platform_events
                WHERE event_type = 'magic_link_issued'
                  AND payload @> :key
                ORDER BY occurred_at DESC
                LIMIT 1
                """
            ),
            {"key": json.dumps({"token_hash": token_hash, "application_id": str(application_id)})},
        ).first()
        if issued is None:
            self._record_failed_attempt(application_id)
            raise InvalidMagicLinkToken("Magic-link token not found")

        issued_id, payload = issued
        ttl_expires_at = datetime.fromisoformat(payload["ttl_expires_at"])
        if datetime.now(timezone.utc) > ttl_expires_at:
            raise InvalidMagicLinkToken("Magic-link token expired")

        # Replay protection: a consumption event referencing this issued row means used.
        already = self.db.execute(
            text(
                """
                SELECT id FROM platform_events
                WHERE event_type = 'magic_link_consumed'
                  AND payload @> :key
                LIMIT 1
                """
            ),
            {"key": json.dumps({"issued_event_id": issued_id})},
        ).first()
        if already is not None:
            raise InvalidMagicLinkToken("Magic-link token already consumed")

        patient_id = UUID(payload["patient_id"])
        consumed_event = PlatformEvent(
            event_type="magic_link_consumed",
            actor="patient",
            patient_id=patient_id,
            application_id=application_id,
            payload={
                "v": 1,
                "actor": {"type": "patient", "id": str(patient_id)},
                "application_id": str(application_id),
                "patient_id": str(patient_id),
                "issued_event_id": issued_id,
            },
        )
        self.db.add(consumed_event)
        self.db.commit()

        token_str, expires_at = self._issue_jwt(patient_id)
        logger.info("magic_link_exchanged", patient_id=str(patient_id), application_id=str(application_id))
        return {"jwt": token_str, "expires_at": expires_at.isoformat()}

    def _enforce_attempt_lockout(self, application_id: UUID) -> None:
        """Refuse exchange if too many failed attempts for this application are
        within the window (security #5b). Counts append-only magic_link_failed
        events — no new table. Locked applications must request a fresh code."""
        since = datetime.now(timezone.utc) - timedelta(seconds=_LOCKOUT_WINDOW_SECONDS)
        count = self.db.execute(
            text(
                """
                SELECT count(*) FROM platform_events
                WHERE event_type = 'magic_link_failed'
                  AND application_id = :aid
                  AND occurred_at >= :since
                """
            ),
            {"aid": str(application_id), "since": since},
        ).scalar()
        if count is not None and count >= _MAX_FAILED_ATTEMPTS:
            logger.warning(
                "magic_link_locked", application_id=str(application_id), failed_attempts=count
            )
            raise MagicLinkLocked(
                "Too many failed attempts. Request a new code and try again later."
            )

    def _record_failed_attempt(self, application_id: UUID) -> None:
        """Append a magic_link_failed event for the lockout counter. No-op if the
        application id is unknown (avoids a FK violation; nothing to protect)."""
        app_exists = (
            self.db.query(PlatformCreditApplication.id)
            .filter(PlatformCreditApplication.id == application_id)
            .first()
            is not None
        )
        if not app_exists:
            return
        event = PlatformEvent(
            event_type="magic_link_failed",
            actor="patient",
            application_id=application_id,
            payload={
                "v": 1,
                "actor": {"type": "patient", "id": "unknown"},
                "application_id": str(application_id),
                "reason": "token_not_found",
            },
        )
        self.db.add(event)
        self.db.commit()

    # --- Borrower portal (returning-patient, email-based) login ------------
    # Reuses the magic-link machinery: the issued event payload already carries
    # ``patient_id``, so the borrower exchange looks it up by (token_hash,
    # patient_id) — patient-level, not per-application. Same TTL + replay
    # protection; lockout is keyed per-patient.

    def request_borrower_login(self, email: str) -> dict:
        """Send a borrower a magic-link sign-in by email. Patient-level.

        ALWAYS returns the same generic message so the endpoint never reveals
        whether an account exists for the email (enumeration-safe).
        """
        patient = self._patient_by_email(email)
        if patient is not None:
            # A representative application anchors the magic-link event (the event
            # row + dispatcher carry an application_id); the exchange resolves the
            # issued event by patient_id, so any of the patient's apps works.
            app = (
                self.db.query(PlatformCreditApplication)
                .filter(PlatformCreditApplication.patient_id == patient.id)
                .order_by(PlatformCreditApplication.created_at.desc())
                .first()
            )
            if app is not None:
                token = _generate_token()
                self.dispatcher.send_magic_link(patient.id, app.id, "email", token)
                self.db.commit()
                logger.info("borrower_login_requested", patient_id=str(patient.id))
        return {"message": "If that email matches an account, a sign-in link is on its way."}

    def exchange_borrower_login(self, email: str, token: str) -> dict:
        """Validate a borrower-login token and return a patient JWT (sub=patient_id)."""
        patient = self._patient_by_email(email)
        if patient is None:
            raise InvalidMagicLinkToken("Invalid email or sign-in code")
        self._enforce_borrower_lockout(patient.id)

        token_hash = _hash_token(token)
        issued = self.db.execute(
            text(
                """
                SELECT id, payload FROM platform_events
                WHERE event_type = 'magic_link_issued'
                  AND payload @> :key
                ORDER BY occurred_at DESC
                LIMIT 1
                """
            ),
            {"key": json.dumps({"token_hash": token_hash, "patient_id": str(patient.id)})},
        ).first()
        if issued is None:
            self._record_borrower_failed(patient.id)
            raise InvalidMagicLinkToken("Invalid or expired sign-in code")

        issued_id, payload = issued
        ttl_expires_at = datetime.fromisoformat(payload["ttl_expires_at"])
        if datetime.now(timezone.utc) > ttl_expires_at:
            raise InvalidMagicLinkToken("Sign-in code expired")

        already = self.db.execute(
            text(
                """
                SELECT id FROM platform_events
                WHERE event_type = 'magic_link_consumed'
                  AND payload @> :key
                LIMIT 1
                """
            ),
            {"key": json.dumps({"issued_event_id": issued_id})},
        ).first()
        if already is not None:
            raise InvalidMagicLinkToken("Sign-in code already used")

        self.db.add(
            PlatformEvent(
                event_type="magic_link_consumed",
                actor="patient",
                patient_id=patient.id,
                application_id=UUID(payload["application_id"]),
                payload={
                    "v": 1,
                    "actor": {"type": "patient", "id": str(patient.id)},
                    "patient_id": str(patient.id),
                    "application_id": payload["application_id"],
                    "issued_event_id": issued_id,
                    "via": "borrower_login",
                },
            )
        )
        self.db.commit()
        token_str, expires_at = self._issue_jwt(patient.id)
        logger.info("borrower_login_exchanged", patient_id=str(patient.id))
        return {"jwt": token_str, "expires_at": expires_at.isoformat()}

    def _patient_by_email(self, email: str) -> PlatformPatient | None:
        normalized = (email or "").strip().lower()
        if not normalized:
            return None
        return (
            self.db.query(PlatformPatient)
            .filter(func.lower(PlatformPatient.email) == normalized)
            .first()
        )

    def _enforce_borrower_lockout(self, patient_id: UUID) -> None:
        """Per-patient attempt lockout for borrower login (mirrors the per-app one)."""
        since = datetime.now(timezone.utc) - timedelta(seconds=_LOCKOUT_WINDOW_SECONDS)
        count = self.db.execute(
            text(
                """
                SELECT count(*) FROM platform_events
                WHERE event_type = 'magic_link_failed'
                  AND payload @> :key
                  AND occurred_at >= :since
                """
            ),
            {
                "key": json.dumps({"patient_id": str(patient_id), "reason": "borrower_login"}),
                "since": since,
            },
        ).scalar()
        if count is not None and count >= _MAX_FAILED_ATTEMPTS:
            logger.warning("borrower_login_locked", patient_id=str(patient_id), failed_attempts=count)
            raise MagicLinkLocked(
                "Too many attempts. Request a new sign-in link and try again later."
            )

    def _record_borrower_failed(self, patient_id: UUID) -> None:
        """Append a patient-keyed magic_link_failed event for the lockout counter."""
        app = (
            self.db.query(PlatformCreditApplication.id)
            .filter(PlatformCreditApplication.patient_id == patient_id)
            .order_by(PlatformCreditApplication.created_at.desc())
            .first()
        )
        if app is None:
            return
        self.db.add(
            PlatformEvent(
                event_type="magic_link_failed",
                actor="patient",
                application_id=app[0],
                patient_id=patient_id,
                payload={
                    "v": 1,
                    "actor": {"type": "patient", "id": str(patient_id)},
                    "application_id": str(app[0]),
                    "patient_id": str(patient_id),
                    "reason": "borrower_login",
                },
            )
        )
        self.db.commit()

    def issue_patient_session(self, patient_id: UUID) -> dict:
        """Re-issue a patient session JWT with the CURRENT app_ids claim.

        Public, additive wrapper over ``_issue_jwt`` for flows that change the
        patient's application set mid-session (WS-J in-portal new loan): the
        24h JWT snapshot of ``app_ids`` would otherwise exclude the new
        application until the next login.
        """
        token_str, expires_at = self._issue_jwt(patient_id)
        return {"jwt": token_str, "expires_at": expires_at.isoformat()}

    def _issue_jwt(self, patient_id: UUID) -> tuple[str, datetime]:
        rows = (
            self.db.query(PlatformCreditApplication.id)
            .filter(
                PlatformCreditApplication.patient_id == patient_id,
                PlatformCreditApplication.status.notin_(_TERMINAL_STATUSES),
            )
            .all()
        )
        app_ids = [str(r[0]) for r in rows]
        now = datetime.now(timezone.utc)
        iat = int(now.timestamp())
        exp = iat + JWT_TTL_SECONDS
        claims = {"sub": str(patient_id), "app_ids": app_ids, "iat": iat, "exp": exp}
        token = jwt.encode(claims, settings.PATIENT_JWT_SECRET, algorithm=JWT_ALGORITHM)
        return token, datetime.fromtimestamp(exp, tz=timezone.utc)
