"""In-portal new-loan wizard (WS-J item 7) — authenticated re-origination.

A logged-in borrower applies for a NEW, SEPARATE loan (Dave: "designed as a
new account, a new separate account") through the same terms-calculator entry
as the public journey — the FE drives product/amount selection off the
existing ``/products`` + quote endpoints, then POSTs here instead of the
unauthenticated ``POST /applications`` (no new patient record, no magic-link
round-trip: the borrower is already authenticated).

The new application is SEEDED from the borrower's existing verified file: the
canonical field set of their most recent application carries over
(``borrower_portal.PREFILL_FIELDS`` — SIN excluded by design; it lives
encrypted on the patient and is never copied). ``flow_state`` records the
re-origination provenance.

The response includes a REFRESHED patient JWT: the session's ``app_ids`` claim
is an issuance-time snapshot, so without re-issue the borrower couldn't act on
the application they just created.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import (
    ApplicantClaims,
    get_current_applicant,
    get_orchestrator,
    get_patient_auth_service,
)
from app.core.logging import get_logger
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
from app.services import borrower_portal, loan_quote
from app.services.auth.patient_auth_service import PatientAuthService
from app.services.flow_orchestrator import FlowOrchestrator, OrchestratorError

logger = get_logger(__name__)
router = APIRouter(prefix="/new-loan", tags=["borrower-new-loan"])


# ---------------------------------------------------------------------------
# Terms validation — the SAME rules as the vendor origination path
# (``vendor_origination._validate_term`` / ``_validate_frequency``), built on
# the same canonical primitives: the product's parsed PricingConfig,
# ``loan_quote._term_bounds`` for the term band and ``coerce_frequency`` +
# ``cfg.payment_frequencies`` for the offered frequencies. Re-stated here
# rather than imported so the borrower surface never pulls in the clinic
# router module.
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
        return parse_pricing_config(product.pricing_config, context="borrower new loan")
    except PricingConfigError as exc:
        # An unparseable product config is a platform problem, not a caller one.
        raise HTTPException(
            status_code=502, detail=f"Product pricing config invalid: {exc}"
        )


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


class NewLoanBody(BaseModel):
    credit_product_id: UUID
    # Terms-calculator output: the amount the borrower settled on.
    requested_amount_cents: int = Field(..., gt=0)
    treatment_plan_ref: Optional[str] = Field(None, max_length=200)

    # --- the rest of the calculator's output (B6) ---------------------------
    # Validated against the PRODUCT'S PricingConfig by the same helpers the
    # vendor origination path uses (vendor_origination._validate_term /
    # _validate_frequency) — a borrower cannot request a term or frequency the
    # product does not offer.
    term_months: Optional[int] = Field(
        None, gt=0, description="Requested term; must sit in the product's term band."
    )
    payment_frequency: Optional[str] = Field(
        None, description="Requested repayment frequency; must be offered by the product."
    )
    province: Optional[str] = Field(
        None, min_length=2, max_length=2,
        description="Two-letter province code; stored as residence_province.",
    )
    # ACCEPTED BUT INERT: there is no promo-code entity, table or pricing hook
    # anywhere in the backend, so this applies NO discount and gates NO
    # product. It is recorded verbatim as provenance and echoed back with
    # ``promo_code_applied=false`` so the UI can never imply it did something.
    promo_code: Optional[str] = Field(None, max_length=64)


class NewLoanResponse(BaseModel):
    application_id: UUID
    status: str
    prefilled_fields: list[str]
    # Echo of the validated terms actually recorded on the application.
    term_months: Optional[int] = None
    payment_frequency: Optional[str] = None
    province: Optional[str] = None
    # Always False: no promo engine exists yet (see NewLoanBody.promo_code).
    promo_code_applied: bool = False
    # Refreshed session covering the new application.
    jwt: str
    expires_at: str


@router.post("", response_model=NewLoanResponse, status_code=status.HTTP_201_CREATED)
def start_new_loan(
    body: NewLoanBody,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
    orchestrator: FlowOrchestrator = Depends(get_orchestrator),
    auth_service: PatientAuthService = Depends(get_patient_auth_service),
):
    """Seed a new application from the borrower's existing verified profile."""
    # Terms validation FIRST — a rejected term/frequency must not leave a
    # half-created application behind (nothing is written until this passes).
    term_months = body.term_months
    payment_frequency: Optional[str] = None
    if term_months is not None or body.payment_frequency is not None:
        product = _load_active_product(db, body.credit_product_id)
        cfg = _parse_config(product)
        if term_months is not None:
            _validate_term(cfg, term_months)
        if body.payment_frequency is not None:
            payment_frequency = _validate_frequency(cfg, body.payment_frequency)

    source_app = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.patient_id == claims.patient_id)
        .order_by(PlatformCreditApplication.created_at.desc())
        .first()
    )

    try:
        application = orchestrator.create_application(
            patient_id=claims.patient_id,
            credit_product_id=body.credit_product_id,
            requested_amount_cents=body.requested_amount_cents,
            requested_amount_source="patient",
            patient_proposed_amount_cents=body.requested_amount_cents,
            treatment_plan_ref=body.treatment_plan_ref,
        )
    except OrchestratorError as exc:
        # Bad product id / out-of-bounds amount → client error, never a 500
        # (mirrors applications.py's _http_errors mapping).
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )

    prefilled: list[str] = []
    if source_app is not None:
        carry = borrower_portal.prefill_from_application(source_app)
        for field, value in carry.items():
            setattr(application, field, value)
        prefilled = sorted(carry.keys())

    # Calculator terms onto the canonical application columns (same columns the
    # vendor origination path writes).
    province = body.province.upper() if body.province else None
    if term_months is not None:
        application.requested_term_months = term_months
    if payment_frequency is not None:
        application.preferred_payment_frequency = payment_frequency
    if province is not None:
        application.residence_province = province

    flow_state = dict(application.flow_state or {})
    flow_state["re_origination"] = True
    if source_app is not None:
        flow_state["re_origination_source_application_id"] = str(source_app.id)
    if body.promo_code:
        # Provenance only. No promo entity exists; this is NOT applied to
        # pricing and does not gate product selection.
        flow_state["promo_code"] = body.promo_code
        flow_state["promo_code_applied"] = False
    application.flow_state = flow_state

    db.add(
        PlatformEvent(
            event_type="new_loan_application_started",
            actor="patient",
            patient_id=claims.patient_id,
            application_id=application.id,
            payload={
                "v": 1,
                "actor": {"type": "patient", "id": str(claims.patient_id)},
                "patient_id": str(claims.patient_id),
                "application_id": str(application.id),
                "source_application_id": str(source_app.id) if source_app else None,
                # Field NAMES only in the event log, never values.
                "prefilled_fields": prefilled,
            },
        )
    )
    db.commit()
    db.refresh(application)

    session = auth_service.issue_patient_session(claims.patient_id)
    logger.info(
        "new_loan_application_started",
        patient_id=str(claims.patient_id),
        application_id=str(application.id),
        source_application_id=str(source_app.id) if source_app else None,
    )
    return NewLoanResponse(
        application_id=application.id,
        status=application.status,
        prefilled_fields=prefilled,
        term_months=application.requested_term_months,
        payment_frequency=application.preferred_payment_frequency,
        province=application.residence_province,
        promo_code_applied=False,
        jwt=session["jwt"],
        expires_at=session["expires_at"],
    )
