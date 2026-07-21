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
from app.models.platform.event import PlatformEvent
from app.services import borrower_portal
from app.services.auth.patient_auth_service import PatientAuthService
from app.services.flow_orchestrator import FlowOrchestrator, OrchestratorError

logger = get_logger(__name__)
router = APIRouter(prefix="/new-loan", tags=["borrower-new-loan"])


class NewLoanBody(BaseModel):
    credit_product_id: UUID
    # Terms-calculator output: the amount the borrower settled on.
    requested_amount_cents: int = Field(..., gt=0)
    treatment_plan_ref: Optional[str] = Field(None, max_length=200)


class NewLoanResponse(BaseModel):
    application_id: UUID
    status: str
    prefilled_fields: list[str]
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

    flow_state = dict(application.flow_state or {})
    flow_state["re_origination"] = True
    if source_app is not None:
        flow_state["re_origination_source_application_id"] = str(source_app.id)
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
        jwt=session["jwt"],
        expires_at=session["expires_at"],
    )
