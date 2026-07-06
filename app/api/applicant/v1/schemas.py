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


class AcceptAllConsentsBody(BaseModel):
    """Body for the consolidated disclosure acceptance.

    All fields optional: a bare ``{}`` (or no body) is a valid acceptance. The
    explicit ``accepted`` flag lets a client signal a deliberate decline; the
    optional ``disclosure_version`` records which consolidated disclosure
    document the patient saw.
    """

    accepted: bool = True
    disclosure_version: Optional[str] = None


class AcceptAllConsentsResponse(BaseModel):
    application_id: UUID
    accepted: bool
    granted_purposes: list[str]
    disclosure_version: Optional[str] = None
    application_disclaimer_version: Optional[str] = None


class ApplicationDisclaimerResponse(BaseModel):
    """The canonical, versioned full application disclaimer served in-flow.

    Text is loaded verbatim from ``config/consent_text/application_disclaimer/``
    via the consent-text loader (version-controlled, immutable per file). The
    version is recorded on the ``consolidated_disclosure_accepted`` event when the
    applicant accepts, giving an auditable purpose+version record.
    """

    purpose: str
    version: str
    text: str


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
    """Manual credit application fields (PaySpyre Credit Application v1.00).

    The fallback when an integration (Didit/Flinks) can't populate the
    application: the borrower supplies the data by hand and the application is
    routed to manual review. Field set mirrors the v1.00 paper application — a
    pragmatic "base" required core (identity + income) with the remaining detail
    optional (to be tightened per Dave). Monetary fields are cents (>= 0).
    Co-borrower and document uploads are intentionally NOT here yet (uploads need
    file-storage infra) — they're follow-ups.
    """

    # Personal information
    first_name: str = Field(..., min_length=1)
    middle_name: Optional[str] = None
    last_name: str = Field(..., min_length=1)
    date_of_birth: date
    email: str = Field(..., min_length=3)
    marital_status: str = Field(..., min_length=1)
    number_of_dependents: int = Field(0, ge=0)
    citizenship: str = Field(..., min_length=1)
    education: Optional[str] = None

    # Address
    residence_years: int = Field(0, ge=0)
    residence_months: int = Field(0, ge=0, le=11)
    street: str = Field(..., min_length=1)
    apartment: Optional[str] = None
    city: str = Field(..., min_length=1)
    province: str = Field(..., min_length=1)
    postal_code: str = Field(..., min_length=1)
    residential_status: str = Field(..., min_length=1)

    # Additional info
    social_insurance_number: Optional[str] = None
    id_verification_type: str = Field(..., min_length=1)
    id_province_of_issue: Optional[str] = None
    main_phone: str = Field(..., min_length=1)
    alternative_phone: Optional[str] = None
    car_owner: Optional[bool] = None
    other_monthly_expenses_cents: int = Field(0, ge=0)

    # Employment / primary income
    income_type: str = Field(..., min_length=1)
    net_monthly_income_cents: int = Field(..., ge=0)
    next_pay_date: Optional[date] = None
    pay_frequency: str = Field(..., min_length=1)
    employer_name: str = Field(..., min_length=1)
    job_title: str = Field(..., min_length=1)
    hire_date: Optional[date] = None
    work_phone: Optional[str] = None
    work_phone_ext: Optional[str] = None


class ManualApplicationResponse(BaseModel):
    application_id: UUID
    status: str
    manual_application: bool


# --- review & finalize (canonical application) -----------------------------


class SecondaryIncomeInput(BaseModel):
    """One declared secondary-income line (all optional; underwriting decides
    which are mandatory). No SIN/PII beyond employment fields."""

    income_type: Optional[str] = None
    net_monthly_income_cents: Optional[int] = Field(default=None, ge=0)
    pay_frequency: Optional[str] = None
    next_pay_date: Optional[date] = None
    employer_name: Optional[str] = None
    job_title: Optional[str] = None
    hire_date: Optional[date] = None
    work_phone: Optional[str] = None
    work_phone_ext: Optional[str] = None
    description: Optional[str] = None


class FinalizeApplicationBody(BaseModel):
    """The applicant's corrected/completed canonical field values, submitted from
    the review-and-finalize screen.

    Every field is OPTIONAL: the review screen submits corrections on top of
    whatever an integration (or a mock fill) already populated, so an unset field
    means "leave as-is", not "clear". Which fields are ultimately mandatory for a
    decision is a business rule owned by the underwriting layer, not enforced here.

    SIN is intentionally NOT accepted here — it is collected via the dedicated
    ``POST /applications/{id}/sin`` path (encrypted at rest, only last-3 retained).
    A raw SIN must never arrive on this body.
    """

    # Personal
    first_name: Optional[str] = None
    middle_name: Optional[str] = None
    last_name: Optional[str] = None
    date_of_birth: Optional[date] = None
    marital_status: Optional[str] = None
    number_of_dependents: Optional[int] = Field(default=None, ge=0)
    citizenship: Optional[str] = None
    education: Optional[str] = None
    main_phone: Optional[str] = None
    alternative_phone: Optional[str] = None
    email: Optional[str] = None

    # ID (NOTE: no SIN — see docstring)
    id_type: Optional[str] = None
    id_number: Optional[str] = None
    id_province_of_issue: Optional[str] = None
    id_expiry: Optional[date] = None

    # Residence
    residence_street: Optional[str] = None
    residence_unit: Optional[str] = None
    residence_city: Optional[str] = None
    residence_province: Optional[str] = None
    residence_postal_code: Optional[str] = None
    time_at_address_years: Optional[int] = Field(default=None, ge=0)
    time_at_address_months: Optional[int] = Field(default=None, ge=0, le=11)
    residential_status: Optional[str] = None
    monthly_housing_payment_cents: Optional[int] = Field(default=None, ge=0)

    # Primary income
    income_type: Optional[str] = None
    net_monthly_income_cents: Optional[int] = Field(default=None, ge=0)
    next_pay_date: Optional[date] = None
    pay_frequency: Optional[str] = None
    employer_name: Optional[str] = None
    hire_date: Optional[date] = None
    job_title: Optional[str] = None
    work_phone: Optional[str] = None
    work_phone_ext: Optional[str] = None
    ok_to_contact_at_work: Optional[bool] = None

    # Financial
    number_of_credit_accounts: Optional[int] = Field(default=None, ge=0)
    car_ownership: Optional[str] = None
    monthly_car_payment_cents: Optional[int] = Field(default=None, ge=0)
    non_discretionary_expenses_cents: Optional[int] = Field(default=None, ge=0)

    # Secondary incomes. None = "leave existing lines untouched"; a list (even
    # empty) REPLACES the declared secondary-income lines wholesale.
    secondary_incomes: Optional[list[SecondaryIncomeInput]] = None


class FinalizeApplicationResponse(BaseModel):
    application_id: UUID
    status: str
    finalized: bool
    routed_to: str  # "manual_review" | "verification"


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
