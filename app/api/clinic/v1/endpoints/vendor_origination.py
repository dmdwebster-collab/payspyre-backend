"""Vendor-originated applications — intake, live preview, request-reprocessing.

WS-I of the P0 Turnkey-parity build. PRIMARY SPEC:
``docs/turnkey_parity/10__Vendor_Access.md`` (Dave's vendor-portal requirements,
narrated over the Turnkey admin backend):

* ``POST /clinic/v1/applications`` — the vendor intake form. The practice
  creates a DRAFT application on the patient's behalf carrying the
  "Application Submission Checklist" money model (treatment cost − insurance −
  down payment = amount financed, validated invariant), the patient's payment
  preferences, and the vendor-arranged term/rate/dates. The borrower then gets
  the STANDARD consent/verification journey via SMS/email magic link —
  **PaySpyre decides, the vendor only originates.**
* ``POST /clinic/v1/applications/preview`` — the live payment preview from the
  Turnkey new-application form (no persistence): installment, principal +
  interest + fees = total, per-frequency Canadian APR (SOR/2001-104 via
  ``loan_quote``), and the full amortization schedule preview.
* ``POST /clinic/v1/applications/{id}/request-reprocessing`` — the ONLY vendor
  underwriting action (Dave: "they should have one button that says 'request
  reprocessing'"). Valid on declined / in-adjudication applications belonging
  to THIS vendor; flips the file to ``under_review`` with a
  ``vendor_reprocessing_requested`` flag the admin queue filters on; audited
  via ``platform_events``.

SCOPING: everything is ``principal.vendor_id``-scoped; cross-vendor access is a
404 (existing clinic-surface pattern). Responses expose ONLY vendor-safe fields
(regression fence: ``tests/test_vendor_visibility_fence.py``) and application
statuses always pass through ``to_vendor_visible_status`` (silent escalation of
auto-declines).

COMMISSION (FLAGGED FOR DAVE): the Turnkey preview shows a "Commission" line
(Principal + Interest + Commission = Total). The typed ``PricingConfig`` has no
vendor-commission concept yet — the preview surfaces the product's configured
fees (the vendor-relevant cost lines) and returns ``commission_cents=None``
with an explanatory note until the commission model is defined.
"""
from __future__ import annotations

from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

# Cross-surface reuse of the applicant magic-link machinery: the borrower
# journey a vendor-originated application triggers is the SAME journey a
# patient-originated one uses (consent-first; PaySpyre decides).
from app.api.applicant.v1.deps import get_patient_auth_service
from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user, get_orchestrator
from app.api.clinic.v1.endpoints.financing_links import (
    _build_patient_flow_url,
    _find_or_create_patient,
    _looks_like_email,
)
from app.api.clinic.v1.status_map import to_vendor_visible_status
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.event import PlatformEvent
from app.schemas.pricing_config import (
    PricingConfig,
    PricingConfigError,
    coerce_frequency,
    parse_pricing_config,
)
from app.services import loan_quote
from app.services.auth.patient_auth_service import PatientAuthService
from app.services.flow_orchestrator import (
    FlowOrchestrator,
    InvalidAmountError,
    InvalidStateTransition,
    OrchestratorError,
    mark_vendor_reprocessing,
)

# The single source of truth for a product's effective rate band (falls back to
# the legacy 12.99% platform default when the config carries no interest block).
from app.services.loan_quote import _rate_bounds  # noqa: PLC2701 — shared rate-band rule

router = APIRouter(prefix="/applications", tags=["clinic-vendor-origination"])


# ---------------------------------------------------------------------------
# Schemas (module-local — the shared schemas.py mirrors the existing frontend
# contract and must not grow ad hoc; same convention as the dashboard modules).
# ---------------------------------------------------------------------------


class VendorApplicationIntakeBody(BaseModel):
    """Dave's Application Submission Checklist as a typed intake body.

    Money model (checklist rows 4-10, integer cents):
        treatment_cost − insurance_coverage − down_payment = amount_financed
    The invariant is validated here so a mistyped amount can never silently
    finance the wrong principal.
    """

    model_config = ConfigDict(extra="forbid")

    # -- patient (find-or-create; same contract as financing-links) ----------
    patient_name: str = Field(..., min_length=1)
    patient_contact: str = Field(
        ..., min_length=1,
        description="Email or phone; also selects the magic-link channel (email vs SMS).",
    )

    # -- product + checklist money model (integer cents) ---------------------
    credit_product_id: UUID
    treatment_cost_cents: int = Field(..., gt=0)
    insurance_coverage_cents: int = Field(0, ge=0)
    down_payment_cents: int = Field(0, ge=0)
    amount_financed_cents: int = Field(..., gt=0)

    # -- vendor-arranged terms ------------------------------------------------
    term_months: int = Field(..., gt=0)
    requested_annual_rate_bps: Optional[int] = Field(
        None, ge=0,
        description=(
            "Custom rate in bps. Only accepted within the product's PricingConfig "
            "band, and only for clinic roles listed in interest.rate_edit_roles."
        ),
    )
    province: Optional[str] = Field(None, min_length=2, max_length=2)
    provider_name: Optional[str] = None
    loan_start_date: Optional[date] = None
    first_due_date: Optional[date] = Field(
        None, description="Custom first due date (must be after loan_start_date)."
    )

    # -- patient payment preferences (checklist rows 11-13) ------------------
    preferred_payment_amount_cents: Optional[int] = Field(None, gt=0)
    preferred_payment_frequency: Optional[str] = None
    preferred_first_due_date: Optional[date] = None

    # -- unstructured extras (checklist rows 14-16) -> self_reported ----------
    alt_contact_name: Optional[str] = None
    alt_contact_relationship: Optional[str] = None
    additional_notes: Optional[str] = None

    @model_validator(mode="after")
    def _checklist_invariants(self) -> "VendorApplicationIntakeBody":
        expected = (
            self.treatment_cost_cents
            - self.insurance_coverage_cents
            - self.down_payment_cents
        )
        if expected != self.amount_financed_cents:
            raise ValueError(
                "Checklist money model violated: treatment_cost_cents "
                f"({self.treatment_cost_cents}) − insurance_coverage_cents "
                f"({self.insurance_coverage_cents}) − down_payment_cents "
                f"({self.down_payment_cents}) = {expected}, but "
                f"amount_financed_cents is {self.amount_financed_cents}."
            )
        if expected <= 0:
            raise ValueError("Insurance + down payment cover the full treatment cost — nothing to finance.")
        if (
            self.first_due_date is not None
            and self.loan_start_date is not None
            and self.first_due_date <= self.loan_start_date
        ):
            raise ValueError("first_due_date must be after loan_start_date.")
        return self


class VendorApplicationCreated(BaseModel):
    """Vendor-safe creation receipt (NO risk/bureau/bank data — fence-tested)."""

    application_id: UUID
    status: str  # vendor-visible bucket, not the raw platform status
    patient_name: str
    product_name: str
    amount_financed_cents: int
    patient_flow_url: str
    verification_channel: str  # 'email' | 'sms' — where the magic link went
    verification_message: str


class PreviewRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credit_product_id: UUID
    amount_cents: int = Field(..., gt=0)
    term_months: int = Field(..., gt=0)
    frequency: str = "monthly"
    annual_rate_bps: Optional[int] = Field(
        None, ge=0, description="Custom rate; must sit within the product's band."
    )


class PreviewFeeLine(BaseModel):
    """One configured fee, surfaced to the vendor (cost lines, no processor data)."""

    fee_type: str
    calc: str            # fixed_cents | rate_bps
    amount: int          # cents or bps per `calc`
    charge_timing: str   # per_payment | at_origination | on_event


class PreviewScheduleRow(BaseModel):
    number: int
    payment_cents: int
    principal_cents: int
    interest_cents: int
    balance_cents: int


class VendorPaymentPreview(BaseModel):
    """The Turnkey new-application computed preview (10__Vendor_Access.md §1B):
    'Approximate payment', 'Principal + Interest (+ Commission) = Total (APR)'
    and the schedule table. Stateless — nothing is persisted."""

    amount_cents: int
    term_months: int
    frequency: str
    frequency_label: str
    num_payments: int
    annual_rate_bps: int
    installment_cents: int
    final_installment_cents: int
    principal_cents: int
    interest_cents: int
    fees_cents: int
    total_of_payments_cents: int
    apr_bps: int
    fee_lines: list[PreviewFeeLine]
    commission_cents: Optional[int] = None
    commission_note: str = (
        "No vendor-commission model exists in PricingConfig yet — flagged for "
        "Dave. The configured product fees above are the vendor-relevant cost lines."
    )
    schedule: list[PreviewScheduleRow]


class VendorReprocessingResult(BaseModel):
    application_id: UUID
    status: str  # vendor-visible bucket
    reprocessing_requested: bool


# ---------------------------------------------------------------------------
# Shared validation helpers
# ---------------------------------------------------------------------------


def _load_active_product(db: Session, credit_product_id: UUID) -> PlatformCreditProduct:
    product = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.id == credit_product_id)
        .first()
    )
    if product is None or product.status != "active":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Credit product not found or not active",
        )
    return product


def _parse_config(product: PlatformCreditProduct) -> PricingConfig:
    try:
        return parse_pricing_config(product.pricing_config, context="vendor origination")
    except PricingConfigError as exc:
        # A product with an unparseable config is a platform config problem, not
        # a caller error.
        raise HTTPException(status_code=502, detail=f"Product pricing config invalid: {exc}")


def _validate_term(cfg: PricingConfig, term_months: int) -> None:
    term_min, term_max, _ = loan_quote._term_bounds(cfg)  # canonical bounds rule
    if not (term_min <= term_months <= term_max):
        raise HTTPException(
            status_code=422,
            detail=f"Term {term_months} months is outside the product's range "
                   f"[{term_min}, {term_max}].",
        )


def _validate_frequency(cfg: PricingConfig, frequency: str) -> str:
    freq = coerce_frequency(frequency)
    if freq is None or freq not in cfg.payment_frequencies:
        allowed = ", ".join(f.value for f in cfg.payment_frequencies)
        raise HTTPException(
            status_code=422,
            detail=f"Payment frequency '{frequency}' is not offered by this product "
                   f"(allowed: {allowed}).",
        )
    return freq.value


def resolve_rate_bps(
    cfg: PricingConfig,
    requested_rate_bps: Optional[int],
    *,
    role: Optional[str],
    enforce_role: bool,
) -> int:
    """Resolve the effective annual rate for a vendor-side quote/intake.

    * ``None`` → the product's default rate (no gate).
    * A custom rate must sit within the product's ``[min, max]`` band (422) —
      band enforcement applies to EVERYONE, preview included.
    * When ``enforce_role`` (the persisting intake path), a custom rate is
      additionally gated on ``interest.rate_edit_roles`` (Dave: "if they have
      ability to affect the interest rates, then they would be able to change
      it within their allowed limits") → 403 for unauthorized roles.
    """
    interest = _rate_bounds(cfg)
    if requested_rate_bps is None or requested_rate_bps == interest.annual_rate_bps:
        return interest.annual_rate_bps
    if not (interest.min_rate_bps <= requested_rate_bps <= interest.max_rate_bps):
        raise HTTPException(
            status_code=422,
            detail=f"Rate {requested_rate_bps} bps is outside the product's allowed "
                   f"band [{interest.min_rate_bps}, {interest.max_rate_bps}] bps.",
        )
    if enforce_role and (role or "") not in (interest.rate_edit_roles or []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your clinic role is not permitted to set a custom interest "
                   "rate on this product.",
        )
    return requested_rate_bps


def _emit_vendor_event(
    db: Session,
    *,
    event_type: str,
    application: PlatformCreditApplication,
    principal: ClinicPrincipal,
    before: dict | None = None,
    after: dict | None = None,
    metadata: dict | None = None,
) -> None:
    """Append one §6-shaped platform_events row for a vendor-surface action."""
    payload = {
        "v": 1,
        "actor": {"type": "vendor", "id": str(principal.user_id)},
        "application_id": str(application.id),
        "patient_id": str(application.patient_id),
        "before": before or {},
        "after": after or {},
        "metadata": {"vendor_id": str(principal.vendor_id), **(metadata or {})},
    }
    db.add(
        PlatformEvent(
            event_type=event_type,
            actor="vendor",
            patient_id=application.patient_id,
            application_id=application.id,
            payload=payload,
        )
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", response_model=VendorApplicationCreated, status_code=status.HTTP_201_CREATED)
def create_vendor_application(
    body: VendorApplicationIntakeBody,
    db: Session = Depends(get_db),
    orchestrator: FlowOrchestrator = Depends(get_orchestrator),
    auth_service: PatientAuthService = Depends(get_patient_auth_service),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
):
    """Vendor intake form — create a draft application on behalf of a patient.

    The vendor ORIGINATES (they arranged the treatment amount with the patient
    — "PaySpyre doesn't know if that patient is supposed to apply for $500 or
    $5,000"); PaySpyre DECIDES. The patient immediately receives the standard
    consent-first magic-link journey (SMS/email per their contact) — no
    verification, bureau pull, or decision happens without their own consents.
    """
    product = _load_active_product(db, body.credit_product_id)
    cfg = _parse_config(product)
    _validate_term(cfg, body.term_months)
    if body.preferred_payment_frequency is not None:
        body_pref_freq = _validate_frequency(cfg, body.preferred_payment_frequency)
    else:
        body_pref_freq = None
    effective_rate_bps = resolve_rate_bps(
        cfg, body.requested_annual_rate_bps, role=principal.role, enforce_role=True
    )

    patient = _find_or_create_patient(db, body)

    try:
        application = orchestrator.create_application(
            patient_id=patient.id,
            credit_product_id=body.credit_product_id,
            requested_amount_cents=body.amount_financed_cents,
            requested_amount_source="clinic",
            clinic_proposed_amount_cents=body.amount_financed_cents,
            vendor_id=principal.vendor_id,
        )
    except InvalidAmountError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except OrchestratorError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    # Persist the structured checklist fields (migration 052) + extras.
    application.treatment_cost_cents = body.treatment_cost_cents
    application.insurance_coverage_cents = body.insurance_coverage_cents
    application.down_payment_cents = body.down_payment_cents
    application.preferred_payment_amount_cents = body.preferred_payment_amount_cents
    application.preferred_payment_frequency = body_pref_freq
    application.preferred_first_due_date = body.preferred_first_due_date
    application.requested_term_months = body.term_months
    application.requested_annual_rate_bps = effective_rate_bps
    application.provider_name = body.provider_name
    application.loan_start_date = body.loan_start_date
    application.first_due_date = body.first_due_date
    if body.province:
        application.residence_province = body.province.upper()
    # Unqueried checklist extras (rows 14-16) live in self_reported.
    extras = {
        k: v
        for k, v in {
            "alt_contact_name": body.alt_contact_name,
            "alt_contact_relationship": body.alt_contact_relationship,
            "additional_notes": body.additional_notes,
        }.items()
        if v is not None
    }
    if extras:
        self_reported = dict(application.self_reported or {})
        self_reported["vendor_intake"] = extras
        application.self_reported = self_reported

    _emit_vendor_event(
        db,
        event_type="vendor_application_submitted",
        application=application,
        principal=principal,
        after={
            "treatment_cost_cents": body.treatment_cost_cents,
            "insurance_coverage_cents": body.insurance_coverage_cents,
            "down_payment_cents": body.down_payment_cents,
            "amount_financed_cents": body.amount_financed_cents,
            "term_months": body.term_months,
            "annual_rate_bps": effective_rate_bps,
            "provider_name": body.provider_name,
        },
    )
    db.commit()

    # Kick off the STANDARD borrower journey: a magic-link code by SMS or email
    # (channel from the contact the vendor captured). The patient authenticates
    # and walks the same consent → verification → decision flow as always.
    channel = "email" if _looks_like_email(body.patient_contact) else "sms"
    link_result = auth_service.request_magic_link(application.id, channel)

    return VendorApplicationCreated(
        application_id=application.id,
        status=to_vendor_visible_status(application.status, application.decision_by),
        patient_name=body.patient_name,
        product_name=product.name,
        amount_financed_cents=body.amount_financed_cents,
        patient_flow_url=_build_patient_flow_url(
            application.id, body.credit_product_id, body.amount_financed_cents
        ),
        verification_channel=channel,
        verification_message=link_result.get("message", "Code sent."),
    )


@router.post("/preview", response_model=VendorPaymentPreview)
def preview_payment(
    body: PreviewRequestBody,
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
):
    """Live payment preview (no persistence) — the Turnkey computed-preview card.

    Rate-band validation applies (a preview outside the band would show terms
    the intake could never accept); the ROLE gate does not — nothing is
    persisted, and staff need to see what a rate change would do before asking
    an authorized colleague to apply it.
    """
    product = _load_active_product(db, body.credit_product_id)
    cfg = _parse_config(product)
    _validate_term(cfg, body.term_months)
    frequency = _validate_frequency(cfg, body.frequency)
    rate_bps = resolve_rate_bps(
        cfg, body.annual_rate_bps, role=principal.role, enforce_role=False
    )
    if not (product.min_amount_cents <= body.amount_cents <= product.max_amount_cents):
        raise HTTPException(
            status_code=422,
            detail=f"Amount must be between {product.min_amount_cents} and "
                   f"{product.max_amount_cents} cents for this product.",
        )

    fees_cents = loan_quote.product_fees_cents(
        product.pricing_config, body.amount_cents, body.term_months, frequency
    )
    n = loan_quote.num_payments(body.term_months, frequency)
    q = loan_quote.quote_loan(
        body.amount_cents,
        rate_bps,
        body.term_months,
        frequency,
        fees_cents=fees_cents,
        preview_rows=n,  # full schedule preview, like the Turnkey form
    )

    fee_lines = [
        PreviewFeeLine(
            fee_type=fee.fee_type.value,
            calc=fee.calc.value,
            amount=fee.amount_for(coerce_frequency(frequency)),
            charge_timing=fee.charge_timing.value,
        )
        for fee in cfg.fees
        if fee.enabled
    ]

    return VendorPaymentPreview(
        amount_cents=q.amount_cents,
        term_months=q.term_months,
        frequency=q.frequency,
        frequency_label=q.frequency_label,
        num_payments=q.num_payments,
        annual_rate_bps=q.annual_rate_bps,
        installment_cents=q.installment_cents,
        final_installment_cents=q.final_installment_cents,
        principal_cents=q.amount_cents,
        interest_cents=q.interest_cents,
        fees_cents=q.fees_cents,
        total_of_payments_cents=q.total_of_payments_cents,
        apr_bps=q.apr_bps,
        fee_lines=fee_lines,
        schedule=[PreviewScheduleRow(**row) for row in q.schedule_preview],
    )


@router.post(
    "/{application_id}/request-reprocessing", response_model=VendorReprocessingResult
)
def request_reprocessing(
    application_id: UUID,
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
):
    """The ONLY vendor underwriting action (Dave's mandate).

    Asks PaySpyre to send the deal back / take another look. Valid on a
    ``declined`` file (including a silently-escalated auto-decline) or one in
    adjudication (``under_review`` / ``underwriting``); the file moves to
    ``under_review`` with ``vendor_reprocessing_requested=True`` so the admin
    queue can lane it. Cross-vendor access is a 404. Audited.
    """
    application = (
        db.query(PlatformCreditApplication)
        .filter(
            PlatformCreditApplication.id == application_id,
            # Vendor scoping: someone else's application is indistinguishable
            # from a nonexistent one (404, never 403 — no existence oracle).
            PlatformCreditApplication.vendor_id == principal.vendor_id,
        )
        .first()
    )
    if application is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Application not found"
        )
    if application.vendor_reprocessing_requested:
        # Idempotent: a second click is a no-op receipt, not a duplicate event.
        return VendorReprocessingResult(
            application_id=application.id,
            status=to_vendor_visible_status(application.status, application.decision_by),
            reprocessing_requested=True,
        )

    before_status = application.status
    try:
        mark_vendor_reprocessing(application)  # status transition owned by orchestrator
    except InvalidStateTransition as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    application.vendor_reprocessing_requested = True

    _emit_vendor_event(
        db,
        event_type="vendor_reprocessing_requested",
        application=application,
        principal=principal,
        before={"status": before_status},
        after={"status": application.status, "vendor_reprocessing_requested": True},
    )
    db.commit()

    return VendorReprocessingResult(
        application_id=application.id,
        status=to_vendor_visible_status(application.status, application.decision_by),
        reprocessing_requested=True,
    )
