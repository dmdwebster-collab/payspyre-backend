"""Vendor → canonical status translators for inbound notification webhooks (P7.4b).

Pure functions. No DB, no HTTP. Map per-vendor body shapes to a small canonical
status alphabet + the (optional) suppression reason that should be recorded
when the delivery indicates the recipient is unreachable / opted out /
complaining. The endpoint takes it from there.

Canonical status alphabet (lives in the ``notification_status_updated``
event payload — not a DB enum, so no migration is needed if we add values):

    "delivered"        — vendor reports terminal-good
    "sent"             — vendor reports in-flight (queued / sending / accepted)
    "failed_transient" — vendor failure that may succeed on retry
    "failed_permanent" — vendor failure that will not succeed on retry
    "complained"       — recipient marked the email as spam
    "ignored"          — event is informational only (e.g. email.opened);
                         endpoint 202s without writing an event
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from app.api.webhooks.v1.schemas import (
    ResendWebhookEnvelope,
    TwilioStatusCallbackBody,
)

NotificationStatus = Literal[
    "delivered", "sent", "failed_transient", "failed_permanent", "complained", "ignored"
]
SuppressionReason = Literal[
    "opt_out", "invalid_recipient", "hard_bounce", "spam_complaint"
]


@dataclass(frozen=True)
class TranslatedStatus:
    """Output of a translator call.

    ``canonical == "ignored"`` signals the endpoint to 202-ack without writing
    a ``notification_status_updated`` event or touching the suppression table.
    """
    canonical: NotificationStatus
    vendor_raw_status: str
    vendor_error_code: Optional[str]
    vendor_message_id: str
    suppression_reason: Optional[SuppressionReason]


# ---------------------------------------------------------------------------
# Twilio Messaging StatusCallback
# ---------------------------------------------------------------------------

# Permanent: subsequent sends will keep failing → suppress the recipient.
# Refs:
#   21610 — "STOP" reply / Twilio Advanced Opt-Out
#   21614 — "To" is not a mobile number (landline / VOIP that can't SMS)
#   30003 — destination handset unreachable
#   30005 — unknown destination handset (carrier-side: number does not exist)
#   30006 — landline or unreachable carrier
TWILIO_PERMANENT_ERROR_CODES = frozenset({"21610", "21614", "30003", "30005", "30006"})
TWILIO_OPT_OUT_ERROR_CODES = frozenset({"21610"})
TWILIO_INVALID_RECIPIENT_CODES = frozenset({"21614", "30003", "30005", "30006"})


def translate_twilio_status(body: TwilioStatusCallbackBody) -> TranslatedStatus:
    """Map a Twilio Messaging StatusCallback to the canonical alphabet."""
    raw_status = body.MessageStatus
    error_code = body.ErrorCode

    if raw_status == "delivered":
        canonical: NotificationStatus = "delivered"
    elif raw_status == "failed":
        canonical = "failed_permanent"
    elif raw_status == "undelivered":
        canonical = (
            "failed_permanent" if error_code in TWILIO_PERMANENT_ERROR_CODES
            else "failed_transient"
        )
    elif raw_status in ("sent", "queued", "sending", "accepted", "scheduled"):
        # Twilio also fires a callback at the in-flight transitions; record
        # them as "sent" so the audit chain is contiguous, but they're not
        # terminal (a later "delivered" / "failed" arrives separately).
        canonical = "sent"
    else:  # pragma: no cover — schema rejects others
        canonical = "ignored"

    # Suppression: only on terminal-permanent failures with a known reason.
    suppression: Optional[SuppressionReason] = None
    if error_code in TWILIO_OPT_OUT_ERROR_CODES:
        suppression = "opt_out"
    elif error_code in TWILIO_INVALID_RECIPIENT_CODES:
        suppression = "invalid_recipient"
    elif canonical == "failed_permanent" and raw_status == "failed":
        # MessageStatus="failed" with no error code = carrier-side terminal
        # failure; treat as hard bounce.
        suppression = "hard_bounce"

    return TranslatedStatus(
        canonical=canonical,
        vendor_raw_status=raw_status,
        vendor_error_code=error_code,
        vendor_message_id=body.MessageSid,
        suppression_reason=suppression,
    )


# ---------------------------------------------------------------------------
# Resend webhook events
# ---------------------------------------------------------------------------

# Event types we map to a canonical bucket. Anything not in this map (e.g.
# email.opened, email.clicked, email.scheduled, email.received, email.suppressed)
# is "ignored" — recorded only as a 202 no-op by the endpoint.
RESEND_BASIC_MAP: dict[str, NotificationStatus] = {
    "email.delivered": "delivered",
    "email.sent": "sent",
    "email.delivery_delayed": "failed_transient",
    "email.failed": "failed_permanent",
    "email.complained": "complained",
    # email.bounced is handled separately because the Permanent / Transient
    # split lives inside data.bounce.type.
}


def translate_resend_status(payload: ResendWebhookEnvelope) -> TranslatedStatus:
    """Map a Resend webhook envelope to the canonical alphabet.

    ``email.bounced`` is the only event type whose canonical bucket depends
    on a nested field (``data.bounce.type`` ∈ ``{"Permanent","Transient"}``).
    Everything else is a flat lookup.
    """
    event_type = payload.type
    data = payload.data or {}
    email_id = str(data.get("email_id") or "")

    if event_type == "email.bounced":
        bounce = data.get("bounce") or {}
        bounce_type = (bounce.get("type") or "").lower()
        canonical: NotificationStatus = (
            "failed_permanent" if bounce_type == "permanent" else "failed_transient"
        )
        suppression: Optional[SuppressionReason] = (
            "hard_bounce" if canonical == "failed_permanent" else None
        )
        return TranslatedStatus(
            canonical=canonical,
            vendor_raw_status=event_type,
            vendor_error_code=bounce.get("subType") or bounce.get("type"),
            vendor_message_id=email_id,
            suppression_reason=suppression,
        )

    canonical_from_map = RESEND_BASIC_MAP.get(event_type, "ignored")
    suppression = None
    if canonical_from_map == "failed_permanent":
        suppression = "hard_bounce"
    elif canonical_from_map == "complained":
        suppression = "spam_complaint"

    return TranslatedStatus(
        canonical=canonical_from_map,
        vendor_raw_status=event_type,
        vendor_error_code=None,
        vendor_message_id=email_id,
        suppression_reason=suppression,
    )
