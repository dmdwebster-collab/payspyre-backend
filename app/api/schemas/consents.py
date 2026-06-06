"""Pydantic schemas for the P5 consent service.

Models only — no FastAPI routes (applicant-facing consent endpoints are P6).
"""
from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ConsentPurpose = Literal[
    "id_verification",
    "bank_verification",
    "soft_bureau_pull",
    "hard_bureau_pull",
    "automated_decision_making",
    "marketing_email",
    "marketplace_listing",
    "cross_sell_eligibility",
    "aggregate_data_use",
]


class ConsentRecordRequest(BaseModel):
    """Payload to record a consent grant/denial. The exact text + version are
    resolved server-side from config — never trusted from the client."""

    purpose: ConsentPurpose
    consent_granted: bool
    application_id: Optional[UUID] = None
    ip_address: Optional[str] = Field(default=None, max_length=45)  # IPv4/IPv6
    user_agent: Optional[str] = Field(default=None, max_length=1000)


class ConsentRead(BaseModel):
    """A consent record as returned to ops/audit surfaces."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    patient_id: UUID
    purpose: ConsentPurpose
    consent_granted: bool
    consent_text_version: str
    granted_at: datetime
    revoked_at: Optional[datetime] = None
    application_id: Optional[UUID] = None


class ConsentTextResponse(BaseModel):
    """The active (or a specific) consent text to display before capturing consent."""

    purpose: ConsentPurpose
    version: str  # e.g. "v1_2026-05"
    version_tag: str  # e.g. "id_verification/v1_2026-05"
    text: str


class ConsentStatusResponse(BaseModel):
    """Whether a patient currently holds an active (granted, non-revoked) consent."""

    patient_id: UUID
    purpose: ConsentPurpose
    has_active_consent: bool
