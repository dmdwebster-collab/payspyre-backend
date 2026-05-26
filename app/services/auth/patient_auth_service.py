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
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from jose import jwt
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.event import PlatformEvent
from app.services.mock_notification_dispatcher import MockNotificationDispatcher

logger = get_logger(__name__)

JWT_TTL_SECONDS = 86_400  # 24h
JWT_ALGORITHM = "HS256"
_TERMINAL_STATUSES = ("withdrawn", "expired")
_TOKEN_ALPHABET = string.ascii_uppercase + string.digits


class MagicLinkError(Exception):
    """Base class for magic-link errors."""


class InvalidMagicLinkToken(MagicLinkError):
    """Token not found, expired, or already consumed (→ 401)."""


class ApplicationNotFound(MagicLinkError):
    """No application for the given id (→ 404)."""


def _generate_token() -> str:
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(6))


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
