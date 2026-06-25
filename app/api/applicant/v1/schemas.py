"""Pydantic request/response models for the applicant API (P6.5)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ContactMethod = Literal["sms", "email"]
AmountSource = Literal["clinic", "patient", "clinic_then_patient_adjusted"]


# --- auth ------------------------------------------------------------------


class MagicLinkRequestBody(BaseModel):
    application_id: UUID
    contact_method: ContactMethod


class MagicLinkRequestResponse(BaseModel):
    contact_method: ContactMethod
    message: str


class MagicLinkExchangeBody(BaseModel):
    application_id: UUID
    token: str = Field(..., min_length=4, max_length=12)


class MagicLinkExchangeResponse(BaseModel):
    jwt: str
    expires_at: str


# --- applications ----------------------------------------------------------


class PatientProfileInput(BaseModel):
    legal_first_name: Optional[str] = None
    legal_last_name: Optional[str] = None
    email: Optional[str] = None
    phone_e164: Optional[str] = None


class CreateApplicationBody(BaseModel):
    patient_profile: PatientProfileInput
    credit_product_id: UUID
    requested_amount_cents: int = Field(..., gt=0)
    requested_amount_source: AmountSource
    clinic_proposed_amount_cents: Optional[int] = Field(default=None, gt=0)
    patient_proposed_amount_cents: Optional[int] = Field(default=None, gt=0)
    treatment_plan_ref: Optional[str] = None
    contact_method: ContactMethod = "email"


class AuthChallenge(BaseModel):
    method: ContactMethod
    message: str


class CreateApplicationResponse(BaseModel):
    application_id: UUID
    auth_challenge: AuthChallenge


class ApplicationStateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str
    credit_product_id: UUID
    credit_product_version: int
    requested_amount_cents: int
    decision: Optional[dict[str, Any]] = None
    created_at: datetime
    status_updated_at: datetime


class RequiredConsentsResponse(BaseModel):
    required: list[str]


class ConsentGrantResponse(BaseModel):
    consent_id: UUID
    purpose: str
    granted: bool


class InitiateVerificationResponse(BaseModel):
    verification_id: UUID
    verification_type: str
    status: str
    vendor_session_ref: Optional[str] = None


# VerificationCallbackBody / VerificationCallbackResponse removed in P7.3 —
# the deprecated applicant callback endpoint was excised. Vendor results now
# arrive exclusively via the HMAC-verified webhook at /api/webhooks/v1/{vendor}/verification.


class SubmitResponse(BaseModel):
    application_id: UUID
    status: str
    decision: Optional[dict[str, Any]] = None
    already_decided: bool


# --- manual application path -----------------------------------------------


class ManualApplicationBody(BaseModel):
    """Fields normally pulled from integrations (Didit/Flinks), entered manually.

    The fallback when an integration fails: the borrower supplies the data by
    hand and the application is routed to manual review. All fields are required
    — a manual application is incomplete without them (a missing one → 422).
    Monetary fields are cents (integers, ``>= 0``) to match the rest of the
    platform's money handling.
    """

    legal_name: str = Field(..., min_length=1)
    date_of_birth: date
    address: str = Field(..., min_length=1)
    employer_name: str = Field(..., min_length=1)
    monthly_income_cents: int = Field(..., ge=0)
    monthly_shelter_cents: int = Field(..., ge=0)
    monthly_non_discretionary_expenses_cents: int = Field(..., ge=0)


class ManualApplicationResponse(BaseModel):
    application_id: UUID
    status: str
    manual_application: bool


# --- products (applicant-facing catalogue) ---------------------------------


class ProductSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    vertical: str
    min_amount_cents: int
    max_amount_cents: int
    currency: str


class ProductsResponse(BaseModel):
    products: list[ProductSummary]


# --- SIN collection (C-3) --------------------------------------------------


class SinCollectBody(BaseModel):
    """Either ``{"sin": "<9 digits>"}`` OR ``{"declined": true}``.

    The full SIN is accepted on the request only; it is NEVER echoed back. The
    model validator enforces exactly one of the two intents.
    """

    sin: Optional[str] = Field(default=None, min_length=9, max_length=11)
    declined: bool = False

    def model_post_init(self, __context: Any) -> None:  # pydantic v2 hook
        provided_sin = self.sin is not None and self.sin.strip() != ""
        if provided_sin and self.declined:
            raise ValueError("Provide either 'sin' or 'declined', not both")
        if not provided_sin and not self.declined:
            raise ValueError("Provide either 'sin' or 'declined'")


class SinCollectResponse(BaseModel):
    """The ONLY SIN-related shape ever returned. Never contains the full SIN."""

    sin_last3: Optional[str] = None
    collected: bool
    declined: bool
