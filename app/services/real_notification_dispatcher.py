"""Real notification dispatcher — P7.4 (outbound / send_magic_link only).

Lives flat next to ``mock_notification_dispatcher.py`` and implements the same
``send_magic_link(patient_id, application_id, contact_method, token,
ttl_seconds=900) -> int`` contract so it can stand in for the mock when
``settings.USE_REAL_NOTIFICATIONS=True``. The applicant API selects between
the two in ``app/api/applicant/v1/deps.py:get_notification_dispatcher``.

Two-step write semantics (kickoff §6.5 / Q4):

1. Write ``magic_link_issued`` event (uncommitted); ``db.flush()`` to get the
   bigint event id.
2. Call the vendor (Resend for email, Twilio for SMS). The vendor call is
   single-shot — there are NO retries inside this adapter (Twilio Messages
   has no Idempotency-Key; Resend's SDK doesn't expose its Idempotency-Key
   header; symmetric "no retries" keeps both providers consistent).
3. On vendor success: write ``notification_sent`` event referencing the
   magic_link event id. The caller (``PatientAuthService.request_magic_link``)
   commits both events together.
4. On vendor failure: raise ``NotificationError``. The auth service never
   commits → FastAPI's ``get_db`` cleanup rolls the session back → no
   orphan ``magic_link_issued`` event lands (so an applicant retry generates
   a fresh token + a fresh attempt). HTTP layer surfaces 500.

The vendor-result path (delivery confirmations, bounces, opt-outs via Twilio
StatusCallback + Resend Svix webhooks) is **P7.4b**.

PII boundaries (Hard Rule #6):
- The plaintext token is hashed (SHA-256) into the ``magic_link_issued``
  event payload, never written to events or logs.
- The recipient (phone E.164 or lowercased email) is hashed into
  ``recipient_redacted`` on the ``notification_sent`` event; the raw value
  reaches Twilio / Resend but never the event log.
- Vendor error messages are passed through a redaction helper before they
  land anywhere — Twilio occasionally echoes the recipient in error bodies.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from uuid import UUID

import resend
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.platform.event import PlatformEvent

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class NotificationError(Exception):
    """Base class for vendor send failures (P7.4)."""


class TransientNotificationError(NotificationError):
    """Vendor 5xx / 429 / network — caller-level retry is plausible."""


class PermanentNotificationError(NotificationError):
    """Vendor 4xx / invalid recipient / opted out — retry will not help."""


# ---------------------------------------------------------------------------
# Twilio error classification
# ---------------------------------------------------------------------------

# Subset of permanent Twilio Messaging error codes. Anything not in this set
# (and not a 5xx HTTP status) is conservatively classified as transient so the
# caller can decide whether to surface or retry. Cross-referenced against
# Twilio's Messaging Error Codes index.
_TWILIO_PERMANENT_CODES = {
    21211,  # Invalid 'To' phone number
    21408,  # Permission to send SMS not enabled for the region
    21610,  # The recipient has opted out (STOP)
    21612,  # 'To' phone number is not currently reachable
    21614,  # 'To' is not a mobile number
    21617,  # 'To' is not a valid phone number for SMS
    30003,  # Unreachable destination handset (carrier)
    30004,  # Message blocked
    30005,  # Unknown destination handset
    30006,  # Landline or unreachable carrier
    30007,  # Carrier violation
    30008,  # Unknown error (carrier-side, but typically not retry-safe)
}


def _classify_twilio_error(exc: Exception) -> NotificationError:
    """Map a TwilioRestException to the project's NotificationError hierarchy.

    Inspects ``.status`` (HTTP) first, then ``.code`` (Twilio error code).
    Anything that isn't clearly permanent is conservatively transient.
    """
    status = getattr(exc, "status", None)
    code = getattr(exc, "code", None)
    msg = _redact_pii(str(getattr(exc, "msg", "") or str(exc)))
    summary = f"Twilio HTTP {status} code {code}: {msg}"

    if status in (429, 500, 502, 503, 504):
        return TransientNotificationError(summary)
    if isinstance(code, int) and code in _TWILIO_PERMANENT_CODES:
        return PermanentNotificationError(summary)
    if isinstance(status, int) and 400 <= status < 500:
        return PermanentNotificationError(summary)
    # Network errors, non-int status, anything else — conservative transient.
    return TransientNotificationError(summary)


# ---------------------------------------------------------------------------
# Resend error classification — heuristic (SDK has no typed exception
# hierarchy as of resend>=0.8.0; tracked in backlog).
# ---------------------------------------------------------------------------

_RESEND_PERMANENT_HINTS = (
    "invalid_email",
    "invalid_to",
    "invalid_from",
    "invalid_attachment",
    "validation_error",
    "missing_required",
    "not_authorized",
    "domain_not_found",
    "restricted_recipient",
    "blocked",
)


def _classify_resend_error(exc: Exception) -> NotificationError:
    """Heuristic mapping of Resend SDK exceptions until typed exceptions ship.

    The Resend Python SDK raises bare ``Exception`` (or one of its subclasses
    with a ``code`` / ``status_code`` attribute when an httpx error propagates).
    We inspect attributes opportunistically, then fall back to message-string
    hints. Anything ambiguous is conservatively transient.

    Backlog: replace this helper with typed exception handling once
    ``resend.exceptions.*`` ships upstream.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    raw_msg = str(exc)
    msg = _redact_pii(raw_msg)
    summary = f"Resend status={status}: {msg}"

    if isinstance(status, int):
        if status in (429, 500, 502, 503, 504):
            return TransientNotificationError(summary)
        if 400 <= status < 500:
            return PermanentNotificationError(summary)

    lowered = raw_msg.lower()
    for hint in _RESEND_PERMANENT_HINTS:
        if hint in lowered:
            return PermanentNotificationError(summary)
    return TransientNotificationError(summary)


# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------


def _hash_recipient(contact_method: str, recipient: str) -> str:
    """SHA-256 of the recipient — phone E.164 raw, email lowercased.

    Stable across retries (so ops can join two ``notification_sent`` events
    pointing at the same person), but the raw value never lands in the event
    log. Same family as ``mock_notification_dispatcher``'s token_hash.
    """
    canonical = recipient.strip().lower() if contact_method == "email" else recipient.strip()
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")


def _redact_pii(text: str) -> str:
    """Strip email addresses and phone-like number sequences from arbitrary text.

    Used on vendor error messages before they reach the event log or the
    logger — Twilio in particular sometimes echoes the recipient in
    diagnostic strings.
    """
    if not text:
        return text
    redacted = _EMAIL_RE.sub("<email-redacted>", text)
    redacted = _PHONE_RE.sub("<phone-redacted>", redacted)
    return redacted


# ---------------------------------------------------------------------------
# Senders — thin wrappers around the SDKs. Both are sync.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SendOutcome:
    """What a successful vendor send returns to the dispatcher."""
    vendor: Literal["resend", "twilio", "sendgrid"]
    vendor_message_id: str
    # Vendor sync status. Resend always returns "sent" (the API only acks accepted).
    # Twilio returns "queued" / "accepted" / "sent" depending on routing latency.
    # status="failed" / "undelivered" are caught upstream and re-raised, never returned.
    status: Literal["queued", "sent"]


class ResendEmailSender:
    """Wraps ``resend.Emails.send`` for magic-link emails.

    Reads API key from settings at construction time. Caller catches
    ``NotificationError``; the sender itself does NOT log on failure (the
    dispatcher emits the canonical ``notification_sent`` event + raises).
    """

    def __init__(self, api_key: str, from_email: str) -> None:
        self._api_key = api_key
        self._from_email = from_email

    def send_magic_link(
        self, *, to_email: str, token: str, ttl_seconds: int
    ) -> SendOutcome:
        # The SDK reads its api_key from the module-level ``resend.api_key``;
        # set it at call time so monkeypatching ``settings.RESEND_API_KEY`` in
        # tests is honored (and so two dispatchers with different keys could
        # in principle coexist — not exercised today, but cheap correctness).
        resend.api_key = self._api_key
        ttl_minutes = max(1, ttl_seconds // 60)
        # Plaintext token in the HTML body — the recipient is the patient.
        # NEVER logged. The dispatcher's hashed token in the event payload is
        # the audit-side artifact.
        html = (
            "<p>Your PaySpyre verification code is:</p>"
            f"<p><strong>{token}</strong></p>"
            f"<p>It expires in {ttl_minutes} minutes.</p>"
        )
        params: dict = {
            "from": self._from_email,
            "to": to_email,
            "subject": "Your PaySpyre verification code",
            "html": html,
        }
        try:
            result = resend.Emails.send(params)
        except Exception as exc:  # SDK has no typed exception hierarchy yet
            raise _classify_resend_error(exc) from exc
        message_id = (result or {}).get("id", "") or ""
        if not message_id:
            raise TransientNotificationError(
                "Resend ack did not include an email id"
            )
        return SendOutcome(vendor="resend", vendor_message_id=str(message_id), status="sent")


class TwilioSmsSender:
    """Wraps ``twilio.rest.Client.messages.create`` for magic-link SMS.

    Holds an instantiated client so test monkeypatches against
    ``twilio.rest.Client.messages.create`` apply cleanly.
    """

    # Twilio sync response statuses that mean "we accepted it, it's heading out".
    _ACCEPTED_SYNC_STATUSES = {"queued", "sending", "sent", "accepted", "scheduled"}

    def __init__(self, account_sid: str, auth_token: str, from_number: str) -> None:
        from twilio.rest import Client  # local import keeps cold-import cost out of selectors
        self._client = Client(account_sid, auth_token)
        self._from_number = from_number

    def send_magic_link(
        self, *, to_phone: str, token: str, ttl_seconds: int
    ) -> SendOutcome:
        ttl_minutes = max(1, ttl_seconds // 60)
        body = (
            f"Your PaySpyre verification code is {token}. "
            f"It expires in {ttl_minutes} minutes."
        )
        # Tell Twilio where to POST delivery-status updates so the (already-built)
        # StatusCallback receiver at /api/webhooks/v1/notifications/twilio can
        # reconcile delivered/failed — without this, every SMS stays "queued" forever.
        create_kwargs = {"to": to_phone, "from_": self._from_number, "body": body}
        base = settings.WEBHOOK_PUBLIC_BASE_URL.rstrip("/")
        if base:
            create_kwargs["status_callback"] = f"{base}/api/webhooks/v1/notifications/twilio"
        try:
            message = self._client.messages.create(**create_kwargs)
        except Exception as exc:
            raise _classify_twilio_error(exc) from exc
        status_raw = getattr(message, "status", "queued") or "queued"
        if status_raw in ("failed", "undelivered"):
            # Twilio occasionally returns a terminal-fail synchronously; treat
            # as a vendor failure so the auth-service transaction rolls back.
            raise PermanentNotificationError(
                f"Twilio sync status {status_raw}"
            )
        if status_raw not in self._ACCEPTED_SYNC_STATUSES:
            raise TransientNotificationError(
                f"Unexpected Twilio sync status {status_raw}"
            )
        sid = getattr(message, "sid", "") or ""
        if not sid:
            raise TransientNotificationError("Twilio ack did not include a message sid")
        # Twilio statuses 'sent' / 'accepted' / 'scheduled' are reported as
        # "queued" in the event log — the inbound StatusCallback (P7.4b) will
        # upgrade to 'sent' / 'delivered' / 'failed'.
        normalized: Literal["queued", "sent"] = "sent" if status_raw == "sent" else "queued"
        return SendOutcome(vendor="twilio", vendor_message_id=str(sid), status=normalized)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class RealNotificationDispatcher:
    """Real-vendor send_magic_link with the two-step write + rollback semantics.

    Interface-compatible with ``MockNotificationDispatcher``. The selector in
    ``get_notification_dispatcher`` picks one or the other per
    ``settings.USE_REAL_NOTIFICATIONS``; the auth service is duck-typed.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def send_magic_link(
        self,
        patient_id: UUID,
        application_id: UUID,
        contact_method: Literal["sms", "email"],
        token: str,
        ttl_seconds: int = 900,
    ) -> int:
        """Persist the magic-link event, send via vendor, persist the audit, return event id."""
        recipient = self._resolve_recipient(patient_id, contact_method)
        self._check_suppression(contact_method, recipient)
        issued_event = self._record_issued_event(
            patient_id, application_id, contact_method, token, ttl_seconds
        )

        sender = self._build_sender(contact_method)
        try:
            outcome = self._send(sender, contact_method, recipient, token, ttl_seconds)
        except NotificationError:
            # The caller never commits → session rollback clears the issued
            # event. Re-raise so PatientAuthService → FastAPI surface a 500.
            raise

        self._record_sent_event(
            patient_id=patient_id,
            application_id=application_id,
            contact_method=contact_method,
            recipient=recipient,
            outcome=outcome,
            magic_link_event_id=issued_event.id,
        )
        return issued_event.id

    # -- internals ----------------------------------------------------------

    def _check_suppression(self, contact_method: str, recipient: str) -> None:
        """P7.4b — block sends to recipients on the suppression list before any
        vendor HTTP call. Gated by ``settings.USE_SUPPRESSION_CHECK`` so tests
        can disable it; defaults True so prod is safe by default.

        Lazy import avoids a circular dependency: the repo imports
        ``_hash_recipient`` from this module, so this module can't import the
        repo at module load time.
        """
        if not settings.USE_SUPPRESSION_CHECK:
            return
        from app.repositories.notification_suppression import NotificationSuppressionRepo
        if NotificationSuppressionRepo(self.db).is_suppressed(contact_method, recipient):
            raise PermanentNotificationError(
                f"recipient suppressed ({contact_method})"
            )

    def _resolve_recipient(self, patient_id: UUID, contact_method: str) -> str:
        """Look up the patient's phone / email for the requested channel."""
        from app.models.platform.patient import PlatformPatient
        patient = (
            self.db.query(PlatformPatient)
            .filter(PlatformPatient.id == patient_id)
            .first()
        )
        if patient is None:
            raise PermanentNotificationError(
                f"Patient {patient_id} not found"
            )
        if contact_method == "email":
            if not patient.email:
                raise PermanentNotificationError(
                    f"Patient {patient_id} has no email on file"
                )
            return patient.email
        if contact_method == "sms":
            if not patient.phone_e164:
                raise PermanentNotificationError(
                    f"Patient {patient_id} has no phone_e164 on file"
                )
            return patient.phone_e164
        raise PermanentNotificationError(
            f"Unsupported contact_method '{contact_method}'"
        )

    def _record_issued_event(
        self,
        patient_id: UUID,
        application_id: UUID,
        contact_method: str,
        token: str,
        ttl_seconds: int,
    ) -> PlatformEvent:
        """Insert magic_link_issued + flush to get the bigint id; identical
        shape to MockNotificationDispatcher's event payload so the exchange
        endpoint queries (which already look up by token_hash) keep working."""
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
        self.db.flush()  # assign event.id BEFORE the vendor call
        # P8.0 — fan-out (magic_link_issued is on the default allowlist).
        from app.services.observability.posthog_bridge import capture_event
        capture_event(event)
        return event

    def _build_sender(self, contact_method: str):
        # Prefer creds from the settings area (Dave's mandate), env fallback.
        from app.services.integration_creds import resolve

        if contact_method == "email":
            if settings.EMAIL_PROVIDER == "sendgrid":
                from app.services.sendgrid_email import SendGridEmailSender

                c = resolve(
                    self.db, "sendgrid",
                    secret_keys=["api_key"], config_keys=["from_email"],
                    env={"api_key": "SENDGRID_API_KEY", "from_email": "SENDGRID_FROM_EMAIL"},
                )
                return SendGridEmailSender(api_key=c["api_key"], from_email=c["from_email"])
            c = resolve(
                self.db, "resend",
                secret_keys=["api_key"], config_keys=["from_email"],
                env={"api_key": "RESEND_API_KEY", "from_email": "RESEND_FROM_EMAIL"},
            )
            return ResendEmailSender(api_key=c["api_key"], from_email=c["from_email"])
        if contact_method == "sms":
            c = resolve(
                self.db, "twilio",
                secret_keys=["account_sid", "auth_token"], config_keys=["from_number"],
                env={
                    "account_sid": "TWILIO_ACCOUNT_SID",
                    "auth_token": "TWILIO_AUTH_TOKEN",
                    "from_number": "TWILIO_FROM_NUMBER",
                },
            )
            return TwilioSmsSender(
                account_sid=c["account_sid"],
                auth_token=c["auth_token"],
                from_number=c["from_number"],
            )
        # _resolve_recipient already rejected this, but keep the guard local.
        raise PermanentNotificationError(
            f"Unsupported contact_method '{contact_method}'"
        )

    @staticmethod
    def _send(sender, contact_method: str, recipient: str, token: str, ttl_seconds: int) -> SendOutcome:
        if contact_method == "email":
            return sender.send_magic_link(
                to_email=recipient, token=token, ttl_seconds=ttl_seconds
            )
        return sender.send_magic_link(
            to_phone=recipient, token=token, ttl_seconds=ttl_seconds
        )

    def _record_sent_event(
        self,
        *,
        patient_id: UUID,
        application_id: UUID,
        contact_method: str,
        recipient: str,
        outcome: SendOutcome,
        magic_link_event_id: int,
    ) -> None:
        """Write the audit row. Error fields stay None on success (P7.4b's
        status callback will append separate ``notification_status_updated``
        events for delivered / bounced / failed)."""
        payload = {
            "v": 1,
            "actor": {"type": "system", "id": "system"},
            "application_id": str(application_id),
            "patient_id": str(patient_id),
            "channel": contact_method,
            "vendor": outcome.vendor,
            "vendor_message_id": outcome.vendor_message_id,
            "status": outcome.status,
            "recipient_redacted": _hash_recipient(contact_method, recipient),
            "magic_link_event_id": magic_link_event_id,
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
        )
        self.db.add(event)
        self.db.flush()
        # P8.0 — fan-out (notification_sent is on the default allowlist).
        from app.services.observability.posthog_bridge import capture_event
        capture_event(event)

    # -- mock parity: tests that assert dispatcher._sent introspection patterns
    # remain on MockNotificationDispatcher. The real dispatcher has no
    # plaintext-token introspection by design (Hard Rule #6).
    _sent: Optional[list] = None
