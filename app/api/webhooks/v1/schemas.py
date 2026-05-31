"""Pydantic models for vendor webhook payloads.

P6.6 shipped one MVP envelope shared by all three vendors. P7.2b moves Didit
and Flinks to real, vendor-shaped payloads and keeps the MVP envelope only on
the ``equifax`` route (until a real Equifax adapter is integrated).

The vendor payloads are intentionally **loose** — Pydantic only validates the
handful of fields the translator reads (event id, status, vendor_data / Tag,
Login.Id, etc.) so a vendor adding optional fields tomorrow cannot 400-break
the receiver. The full body is also stored verbatim in the
``verification_completed`` event ``rich_payload`` for audit.
"""
from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Response (shared)
# ---------------------------------------------------------------------------


class WebhookAcceptedResponse(BaseModel):
    status: str
    application_status: Optional[str] = None
    decided: bool = False


# ---------------------------------------------------------------------------
# Equifax MVP envelope (P6.6 — kept until real Equifax lands)
# ---------------------------------------------------------------------------


class WebhookVerificationBody(BaseModel):
    """Flat MVP envelope shared by ``mock_dispatcher.simulate_callback`` and the
    ``equifax`` webhook route. Real Didit/Flinks routes use the schemas below.
    """
    verification_type: str
    result: Literal["passed", "failed"]
    application_id: UUID
    verification_id: UUID
    rich_payload: dict[str, Any] = Field(default_factory=dict)
    vendor: Optional[str] = None  # informational; the path param is authoritative


# ---------------------------------------------------------------------------
# Didit webhook (P7.2b)
# Reference: https://docs.didit.me/integration/webhooks
# ---------------------------------------------------------------------------


class DiditWebhookPayload(BaseModel):
    """The Didit ``status.updated`` webhook envelope.

    Only the fields the translator needs are typed. Everything else (decision
    detail, AML, workflow, etc.) flows through as ``dict[str, Any]`` so future
    Didit additions land in ``rich_payload`` without code changes here.
    """
    model_config = ConfigDict(extra="allow")

    event_id: str
    webhook_type: str
    timestamp: int
    session_id: str
    status: str               # "Approved" | "Declined" | "In Review" | "In Progress" | …
    vendor_data: Optional[str] = None  # our application_id, as set at initiate
    workflow_id: Optional[str] = None
    decision: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Flinks webhook (P7.2b)
# Reference: https://docs.flinks.com/api/connect/webhooks
# ---------------------------------------------------------------------------


class FlinksLogin(BaseModel):
    model_config = ConfigDict(extra="allow")

    Id: str


class FlinksWebhookPayload(BaseModel):
    """The Flinks Connect webhook body (PascalCase JSON, no aliasing required).

    ``Accounts`` is left as ``list[dict]`` — the translator walks transactions
    arithmetically (income / NSF / age) and the full structure also lands in
    ``rich_payload`` for audit / future Enrich-API replacement.
    """
    model_config = ConfigDict(extra="allow")

    ResponseType: str          # "GetAccountsDetail" | "KYC" | …
    HttpStatusCode: int
    Login: FlinksLogin
    Tag: Optional[str] = None  # echoed application_id — the correlation bridge
    Accounts: Optional[list[dict[str, Any]]] = None
    RequestId: Optional[str] = None


# ---------------------------------------------------------------------------
# Twilio Messaging StatusCallback (P7.4b)
# Reference: https://www.twilio.com/docs/messaging/api/message-resource
# Form-encoded body (application/x-www-form-urlencoded).
# ---------------------------------------------------------------------------


class TwilioStatusCallbackBody(BaseModel):
    """The Twilio Messaging StatusCallback form body.

    Only ``MessageSid`` + ``MessageStatus`` are required to correlate + classify
    the delivery; ``ErrorCode`` is required for hard-bounce + opt-out detection
    on the ``failed`` / ``undelivered`` statuses but is absent on ``delivered``.
    Other fields are passed through for audit but unused by the translator.
    """
    model_config = ConfigDict(extra="allow")

    MessageSid: str
    MessageStatus: Literal[
        "queued", "sending", "sent", "delivered", "undelivered", "failed",
        "accepted", "scheduled",
    ]
    AccountSid: Optional[str] = None
    From: Optional[str] = None
    To: Optional[str] = None
    ErrorCode: Optional[str] = None
    SmsSid: Optional[str] = None
    SmsStatus: Optional[str] = None


# ---------------------------------------------------------------------------
# Resend webhook envelope (P7.4b)
# Reference: https://resend.com/docs/dashboard/webhooks/introduction
# JSON, Svix-signed.
# ---------------------------------------------------------------------------


class ResendWebhookEnvelope(BaseModel):
    """The outer envelope Resend (via Svix) delivers for every email event.

    The ``data`` shape is event-type-specific (e.g. ``email.bounced`` adds a
    ``bounce: {type, subType, message}`` object); the translator destructures
    it conditionally and the full body lands in the event payload for audit.
    """
    model_config = ConfigDict(extra="allow")

    type: str                         # "email.delivered" | "email.bounced" | "email.complained" | ...
    created_at: str                   # ISO-8601 string
    data: dict[str, Any]              # event-specific; we read .email_id at minimum
