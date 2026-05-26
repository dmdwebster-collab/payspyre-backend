"""Mock notification dispatcher (P6.5).

Records a would-be magic-link SMS/email as a ``platform_events`` row instead of
calling Twilio/SendGrid (real wiring is P7). The raw token is stored only as a
SHA-256 hash in the event payload; the plaintext token is kept in the in-process
``_sent`` list for test introspection and is NEVER written to an HTTP response.

The event row is added + flushed here; the caller (``PatientAuthService``) owns
the commit, since the magic-link send is a side-effect separate from any
orchestrator transaction (kickoff §3 decision #6).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.platform.event import PlatformEvent


class MockNotificationDispatcher:
    def __init__(self, db: Session) -> None:
        self.db = db
        # Test-only introspection: plaintext tokens are recorded here, never in a response.
        self._sent: list[dict] = []

    def send_magic_link(
        self,
        patient_id: UUID,
        application_id: UUID,
        contact_method: Literal["sms", "email"],
        token: str,
        ttl_seconds: int = 900,
    ) -> int:
        """Write a ``magic_link_issued`` event (hashed token) and return its id."""
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        ttl_expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        payload = {
            "v": 1,
            "actor": {"type": "system", "id": "system"},
            "application_id": str(application_id),
            "patient_id": str(patient_id),
            "token_hash": token_hash,
            "ttl_expires_at": ttl_expires_at.isoformat(),
            "contact_method": contact_method,
            "consumed": False,
        }
        event = PlatformEvent(
            event_type="magic_link_issued",
            actor="system",
            patient_id=patient_id,
            application_id=application_id,
            payload=payload,
        )
        self.db.add(event)
        self.db.flush()  # assign event.id
        self._sent.append(
            {
                "contact_method": contact_method,
                "token": token,  # plaintext — test introspection only
                "patient_id": patient_id,
                "application_id": application_id,
                "event_id": event.id,
            }
        )
        return event.id
