"""Mock notification dispatcher (P6.5).

Records a would-be magic-link SMS/email as a ``platform_events`` row instead of
calling Twilio/SendGrid (real wiring is P7). The raw token is stored only as a
SHA-256 hash in the event payload; the plaintext token is kept in the in-process
``_sent`` list for test introspection and is NEVER written to an HTTP response.

The event row is added + flushed here; the caller (``PatientAuthService``) owns
the commit, since the magic-link send is a side-effect separate from any
orchestrator transaction (kickoff §3 decision #6).

Flat module (not under a ``notifications/`` package) to avoid shadowing the
existing ``app/services/notifications.py`` module.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.platform.event import PlatformEvent

# Dev-only: the most recent plaintext magic-link code per application_id. In mock
# mode no real SMS/email is sent and only a SHA-256 hash is persisted, so a local
# demo has no way to obtain the code — this lets a gated dev endpoint surface it.
# The mock dispatcher is only selected in non-prod (the prod selector returns
# RealNotificationDispatcher), so this never holds production codes.
_DEV_PLAINTEXT_CODES: dict[str, str] = {}


def peek_dev_code(application_id: str) -> str | None:
    """Return the last plaintext magic-link code for an application (dev only)."""
    return _DEV_PLAINTEXT_CODES.get(application_id)


class MockNotificationDispatcher:
    def __init__(self, db: Session) -> None:
        self.db = db
        # Test-only introspection: plaintext tokens are recorded here, never in a response.
        self._sent: list[dict] = []

    def _lookup_recipient(self, patient_id: UUID, contact_method: str) -> str | None:
        """Best-effort recipient for the comms log. The mock never resolves a
        real destination (nothing is sent), so a missing patient/contact is
        tolerated rather than an error — the log row still lands."""
        from app.models.platform.patient import PlatformPatient

        try:
            patient = (
                self.db.query(PlatformPatient)
                .filter(PlatformPatient.id == patient_id)
                .first()
            )
        except Exception:  # pragma: no cover - fake sessions in unit tests
            return None
        if patient is None:
            return None
        if contact_method == "email":
            return patient.email
        if contact_method == "sms":
            return patient.phone_e164
        return None

    def send_magic_link(
        self,
        patient_id: UUID,
        application_id: UUID,
        contact_method: Literal["sms", "email"],
        token: str,
        ttl_seconds: int = 900,
        enqueue_on_failure: bool = True,
    ) -> int:
        """Write a ``magic_link_issued`` event (hashed token) and return its id.

        ``enqueue_on_failure`` exists only to keep this signature interchangeable
        with ``RealNotificationDispatcher`` (the selector duck-types them); the
        mock never fails a send, so it is ignored here.
        """
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
        # P8.0 — fan-out (magic_link_issued is on the default allowlist). The
        # mock dispatcher is only reachable in tests/dev (the prod selector in
        # ``get_notification_dispatcher`` returns RealNotificationDispatcher
        # when USE_REAL_NOTIFICATIONS=True); with OBSERVABILITY_ENABLED=False
        # this is a no-op, and the hook lives here for symmetry with the real
        # path so any future test that turns observability on inside the mock
        # captures cleanly.
        from app.services.observability.posthog_bridge import capture_event
        capture_event(event)
        # WS-A comms log (mandate #4): mirror the real dispatcher — the sign-in
        # message is a communication, but its body carries the plaintext code,
        # so store the hidden marker (Turnkey's on-camera behavior).
        from app.services import communications_log

        communications_log.record_communication(
            self.db,
            channel=contact_method,
            recipient=self._lookup_recipient(patient_id, contact_method),
            subject="Your PaySpyre verification code",
            body=communications_log.HIDDEN_BODY,
            notification_type="magic_link",
            patient_id=patient_id,
            application_id=application_id,
            status="sent",
            vendor="mock",
            vendor_message_id="mock",
            source_event_id=event.id,
        )
        _DEV_PLAINTEXT_CODES[str(application_id)] = token  # dev-only peek (see module note)
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

    def send_notification(
        self,
        *,
        patient_id: UUID,
        notification_type: str,
        contact_method: Literal["sms", "email"],
        context: dict,
        application_id: UUID | None = None,
        loan_id: str | None = None,
        correlation_id: UUID | None = None,
        source_event_id: int | None = None,
        sent_by: str = "system",
        sent_by_user_id: UUID | None = None,
        comment: str | None = None,
        subject_override: str | None = None,
        body_override: str | None = None,
    ) -> int:
        """Mock parity for ``RealNotificationDispatcher.send_notification`` (WS1).

        Renders the template (so a missing template / missing context key fails
        in dev + tests exactly as it would in prod), then records a
        ``notification_sent`` event with the rendered subject/preview for
        introspection instead of calling a vendor. ``sent_by`` /
        ``sent_by_user_id`` / ``comment`` attribute staff-initiated sends
        (WS-A cockpit compose) in the communications log.
        """
        from app.services import notification_render

        if body_override is not None:
            # Ad-hoc staff compose (P0 T3 "Send Email"/"Send SMS"): the body is
            # authored (or template-seeded then edited) in the cockpit, so
            # there is nothing to render. Everything downstream — audit event,
            # comms log with the FULL body, suppression semantics — is
            # unchanged.
            subject = subject_override if contact_method == "email" else None
            body = body_override
        elif contact_method == "email":
            subject, body = notification_render.render_email(notification_type, context)
        else:
            subject, body = None, notification_render.render_sms(notification_type, context)

        payload = {
            "v": 1,
            "actor": {"type": "system", "id": "system"},
            "application_id": str(application_id) if application_id else None,
            "patient_id": str(patient_id),
            "channel": contact_method,
            "vendor": "mock",
            "vendor_message_id": "mock",
            "status": "sent",
            "notification_type": notification_type,
            "loan_id": loan_id,
            "source_event_id": source_event_id,
            "attempted_at": datetime.now(timezone.utc).isoformat(),
            "error_class": None,
            "error_message_redacted": None,
        }
        event = PlatformEvent(
            event_type="notification_sent",
            actor="system",
            patient_id=patient_id,
            application_id=application_id,
            payload=payload,
            correlation_id=correlation_id,
        )
        self.db.add(event)
        self.db.flush()
        from app.services.observability.posthog_bridge import capture_event
        capture_event(event)
        # WS-A comms log (mandate #4): FULL rendered body, same transaction as
        # the notification_sent event — identical hook to the real dispatcher
        # so staging/demo (mock mode) produces a real-looking legal log.
        from app.services import communications_log

        communications_log.record_communication(
            self.db,
            channel=contact_method,
            recipient=self._lookup_recipient(patient_id, contact_method),
            subject=subject,
            body=body,
            notification_type=notification_type,
            patient_id=patient_id,
            application_id=application_id,
            loan_id=loan_id,
            sent_by=sent_by,
            sent_by_user_id=sent_by_user_id,
            comment=comment,
            status="sent",
            vendor="mock",
            vendor_message_id="mock",
            source_event_id=event.id,
        )
        self._sent.append(
            {
                "notification_type": notification_type,
                "contact_method": contact_method,
                "subject": subject,
                "body": body,  # rendered preview — test introspection only
                "patient_id": patient_id,
                "application_id": application_id,
                "event_id": event.id,
            }
        )
        return event.id
