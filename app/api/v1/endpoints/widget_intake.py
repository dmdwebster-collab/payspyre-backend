"""Embedded pre-qualification widget intake (server-to-server).

Dave's vendor-embedded widget runs a soft pre-qual and currently EMAILS us the
result. This turns that into a pipeline: the widget POSTs the same payload here,
we create a real application in the platform (so it lands in the lender cockpit
and can be carried forward to full KYC + booking), compute OUR regulated quote for
the selected terms, and apply the product's own configured pre-qual gate.

INERT until WIDGET_API_KEY is set: every call is 403 without a matching X-Widget-Key.
"""
from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.services.consent_service as consent_service
from app.core.config import settings
from app.db.base import get_db
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.patient import PlatformPatient
from app.services import flow_engine, loan_quote
from app.services.flow_orchestrator import FlowOrchestrator, InvalidAmountError
from app.services.verifications.mock_dispatcher import MockVerificationDispatcher

router = APIRouter(prefix="/widget", tags=["widget-intake"])


def _require_widget_key(x_widget_key: str | None = Header(default=None)) -> None:
    configured = settings.WIDGET_API_KEY or ""
    if not configured or x_widget_key != configured:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing X-Widget-Key (widget intake is not enabled).",
        )


class WidgetApplicant(BaseModel):
    first_name: str = Field(..., min_length=1)
    last_name: str = Field(..., min_length=1)
    email: str = Field(..., min_length=3)
    date_of_birth: date | None = None
    credit_score: int | None = Field(default=None, ge=300, le=900)


class WidgetFinancing(BaseModel):
    product_code: str = Field(..., min_length=1)
    amount_cents: int = Field(..., gt=0)
    term_months: int = Field(..., gt=0)
    frequency: str = "monthly"
    province: str | None = None
    vendor: str | None = None           # display strings; stored for the reviewer
    store_provider: str | None = None


class WidgetIncome(BaseModel):
    income_type: str | None = None
    net_monthly_income_cents: int = Field(default=0, ge=0)
    housing_cents: int = Field(default=0, ge=0)
    vehicle_cents: int = Field(default=0, ge=0)
    other_expenses_cents: int = Field(default=0, ge=0)


class WidgetPreQualBody(BaseModel):
    applicant: WidgetApplicant
    financing: WidgetFinancing
    income: WidgetIncome | None = None
    widget_outcome: str | None = None   # the widget's OWN pre-qual result, recorded as-is


# The separate pre-qualification disclosure (Dave's exact wording) is a versioned,
# immutable consent-text file served verbatim by the loader. It is surfaced ONLY on
# the pre-qual path (here), distinct from the full application disclaimer.
_PRE_QUAL_DISCLOSURE_PURPOSE = "pre_qualification_disclosure"


class WidgetPreQualResponse(BaseModel):
    application_id: UUID
    outcome: str            # approved | manual_review | declined | unknown (the PLATFORM's call)
    prequalified: bool      # convenience: outcome == "approved"
    reasons: list[str]
    quote: dict
    # The versioned pre-qualification disclosure shown alongside this pre-qual result.
    prequal_disclosure_version: str
    prequal_disclosure_text: str


@router.post(
    "/pre-qualification",
    response_model=WidgetPreQualResponse,
    dependencies=[Depends(_require_widget_key)],
)
def widget_prequalification(body: WidgetPreQualBody, db: Session = Depends(get_db)):
    fin = body.financing
    product = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == fin.product_code)
        .first()
    )
    if product is None:
        raise HTTPException(status_code=404, detail=f"Unknown credit product '{fin.product_code}'.")
    if fin.frequency not in loan_quote.FREQUENCIES:
        raise HTTPException(status_code=422, detail=f"Unsupported frequency '{fin.frequency}'.")
    if not (product.min_amount_cents <= fin.amount_cents <= product.max_amount_cents):
        raise HTTPException(
            status_code=422,
            detail=f"Amount must be between {product.min_amount_cents} and {product.max_amount_cents} cents.",
        )

    # Find-or-create the patient by email (mirrors the public create endpoint).
    email = body.applicant.email.strip().lower()
    patient = (
        db.query(PlatformPatient).filter(func.lower(PlatformPatient.email) == email).first()
    )
    if patient is None:
        patient = PlatformPatient(
            legal_first_name=body.applicant.first_name,
            legal_last_name=body.applicant.last_name,
            email=email,
        )
        db.add(patient)
        try:
            db.commit()
            db.refresh(patient)
        except IntegrityError:
            db.rollback()
            patient = db.query(PlatformPatient).filter(func.lower(PlatformPatient.email) == email).first()

    orchestrator = FlowOrchestrator(db, consent_service, MockVerificationDispatcher())
    try:
        application = orchestrator.create_application(
            patient_id=patient.id,
            credit_product_id=product.id,
            requested_amount_cents=fin.amount_cents,
            requested_amount_source="patient",
        )
    except InvalidAmountError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Record the widget payload (the reviewer + later steps can see it). Money fields
    # are cents; the widget's own outcome + score are kept verbatim, never trusted as
    # OUR decision.
    self_reported = dict(application.self_reported or {})
    self_reported["widget"] = {
        "widget_outcome": body.widget_outcome,   # the widget's OWN call, kept for audit only
        "credit_score": body.applicant.credit_score,
        "date_of_birth": body.applicant.date_of_birth.isoformat() if body.applicant.date_of_birth else None,
        "vendor": fin.vendor,
        "store_provider": fin.store_provider,
        "province": fin.province,
        "term_months": fin.term_months,
        "frequency": fin.frequency,
        "income": body.income.model_dump() if body.income else None,
    }
    # THE PLATFORM decides the pre-qual — using the SAME score bands the full
    # decision uses (flow_engine), so the widget and the platform never diverge.
    # The widget's own outcome above is recorded for audit but NOT used here.
    matrix = product.verification_matrix if isinstance(product.verification_matrix, dict) else {}
    outcome = flow_engine.prequalify_score(body.applicant.credit_score, matrix.get("bureau") or {})
    reasons = {
        "declined": ["credit_score_below_floor"],
        "manual_review": ["credit_score_in_manual_review_band"],
        "unknown": ["no_credit_score_provided"],
        "approved": [],
    }[outcome]

    # The separate pre-qualification disclosure served with this result — recorded
    # (purpose + version) on the application for an auditable trail of what was shown.
    prequal_disclosure = consent_service.get_active_consent_text(_PRE_QUAL_DISCLOSURE_PURPOSE)

    self_reported["widget"]["platform_prequal_outcome"] = outcome
    self_reported["widget"]["prequal_disclosure_purpose"] = prequal_disclosure.purpose
    self_reported["widget"]["prequal_disclosure_version"] = prequal_disclosure.version
    application.self_reported = self_reported
    flow_state = dict(application.flow_state or {})
    flow_state["widget_prequalification"] = True
    application.flow_state = flow_state
    db.commit()

    # OUR regulated quote for the selected terms.
    params = loan_quote.product_terms(product.pricing_config)
    q = loan_quote.quote_loan(
        fin.amount_cents, params["annual_rate_bps"], fin.term_months, fin.frequency,
        fees_cents=params["fees_cents"],
    )

    return WidgetPreQualResponse(
        application_id=application.id,
        outcome=outcome,
        prequalified=outcome == "approved",
        reasons=reasons,
        prequal_disclosure_version=prequal_disclosure.version,
        prequal_disclosure_text=prequal_disclosure.text,
        quote={
            "amount_cents": q.amount_cents,
            "term_months": q.term_months,
            "frequency": q.frequency,
            "num_payments": q.num_payments,
            "installment_cents": q.installment_cents,
            "total_of_payments_cents": q.total_of_payments_cents,
            "interest_cents": q.interest_cents,
            "fees_cents": q.fees_cents,
            "annual_rate_bps": q.annual_rate_bps,
            "apr_bps": q.apr_bps,
        },
    )
