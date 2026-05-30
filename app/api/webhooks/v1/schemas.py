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
