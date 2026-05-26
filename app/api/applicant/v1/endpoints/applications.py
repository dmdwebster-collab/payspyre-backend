"""Applicant application endpoints (P6.5).

Each protected endpoint validates the patient JWT (``get_current_applicant``)
then checks per-application scope (``require_app_scope`` → 403). Each delegates
to exactly one orchestrator method (plus minimal plumbing: patient
find-or-create on POST /applications per kickoff §3 #6, and verification lookup
on the callback). Orchestrator exceptions are mapped to HTTP codes by the
``_http_errors`` context manager — the only logic in these routers.
"""
from __future__ import annotations

import ipaddress
from contextlib import contextmanager
from typing import Iterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import (
    ApplicantClaims,
    get_current_applicant,
    get_orchestrator,
    get_patient_auth_service,
    require_app_scope,
)
from app.api.applicant.v1.schemas import (
    ApplicationStateResponse,
    AuthChallenge,
    ConsentGrantResponse,
    CreateApplicationBody,
    CreateApplicationResponse,
    InitiateVerificationResponse,
    RequiredConsentsResponse,
    SubmitResponse,
    VerificationCallbackBody,
    VerificationCallbackResponse,
)
from app.core.logging import get_logger
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.patient import PlatformPatient
from app.models.platform.verification import PlatformVerification
from app.services.auth.patient_auth_service import PatientAuthService
from app.services.flow_orchestrator import (
    CONSENT_TO_VERIFICATION_TYPE,
    ApplicationNotFoundError,
    ConsentMissingError,
    DuplicateVerificationError,
    FlowOrchestrator,
    InvalidStateTransition,
    StillPendingError,
    UnknownVerificationType,
)
from app.services.verifications.replay_adapters import ReplayMissingResultError

logger = get_logger(__name__)
router = APIRouter(prefix="/applications", tags=["applicant-applications"])

_PENDING_STATUSES = ("pending", "in_progress")


@contextmanager
def _http_errors() -> Iterator[None]:
    """Map orchestrator exceptions to HTTP status codes (kickoff §3 #5)."""
    try:
        yield
    except ConsentMissingError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except UnknownVerificationType as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except StillPendingError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "Verifications still pending", "pending": exc.pending},
        )
    except (DuplicateVerificationError, InvalidStateTransition) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ReplayMissingResultError as exc:
        logger.error("replay_missing_result", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Verification replay error",
        )


def _client_ip(request: Request) -> str | None:
    host = request.client.host if request.client else None
    try:
        ipaddress.ip_address(host)  # type: ignore[arg-type]
        return host
    except (ValueError, TypeError):
        return None  # e.g. TestClient's "testclient" — INET column is nullable


# --- POST /applications (unauthenticated: this is the entry point) ---------


@router.post("", response_model=CreateApplicationResponse, status_code=status.HTTP_201_CREATED)
def create_application(
    body: CreateApplicationBody,
    db: Session = Depends(get_db),
    orchestrator: FlowOrchestrator = Depends(get_orchestrator),
    auth_service: PatientAuthService = Depends(get_patient_auth_service),
):
    # Patient find-or-create (kickoff §3 #6): match by phone first, then email.
    profile = body.patient_profile
    patient: PlatformPatient | None = None
    if profile.phone_e164:
        patient = (
            db.query(PlatformPatient)
            .filter(PlatformPatient.phone_e164 == profile.phone_e164)
            .first()
        )
    if patient is None and profile.email:
        patient = db.query(PlatformPatient).filter(PlatformPatient.email == profile.email).first()
    if patient is None:
        patient = PlatformPatient(
            legal_first_name=profile.legal_first_name,
            legal_last_name=profile.legal_last_name,
            email=profile.email,
            phone_e164=profile.phone_e164,
        )
        db.add(patient)
        db.commit()
        db.refresh(patient)

    with _http_errors():
        application = orchestrator.create_application(
            patient_id=patient.id,
            credit_product_id=body.credit_product_id,
            requested_amount_cents=body.requested_amount_cents,
            requested_amount_source=body.requested_amount_source,
            clinic_proposed_amount_cents=body.clinic_proposed_amount_cents,
            patient_proposed_amount_cents=body.patient_proposed_amount_cents,
            treatment_plan_ref=body.treatment_plan_ref,
        )

    # Separate, non-transactional side-effect: kick off the magic-link send.
    auth_service.request_magic_link(application.id, body.contact_method)

    return CreateApplicationResponse(
        application_id=application.id,
        auth_challenge=AuthChallenge(
            method=body.contact_method,
            message=f"A sign-in code was sent via {body.contact_method}.",
        ),
    )


# --- protected endpoints ---------------------------------------------------


@router.get("/{application_id}", response_model=ApplicationStateResponse)
def get_application(
    application_id: UUID,
    claims: ApplicantClaims = Depends(get_current_applicant),
    db: Session = Depends(get_db),
):
    require_app_scope(application_id, claims)
    # No orchestrator read method exists (orchestrator is shipped, not modifiable),
    # so this read queries the model directly — no state change, no business logic.
    application = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    return ApplicationStateResponse.model_validate(application)


@router.get("/{application_id}/consents", response_model=RequiredConsentsResponse)
def get_required_consents(
    application_id: UUID,
    claims: ApplicantClaims = Depends(get_current_applicant),
    orchestrator: FlowOrchestrator = Depends(get_orchestrator),
):
    require_app_scope(application_id, claims)
    with _http_errors():
        required = orchestrator.get_required_consents(application_id)
    return RequiredConsentsResponse(required=required)


@router.post("/{application_id}/consents/{purpose}", response_model=ConsentGrantResponse)
def grant_consent(
    application_id: UUID,
    purpose: str,
    request: Request,
    claims: ApplicantClaims = Depends(get_current_applicant),
    orchestrator: FlowOrchestrator = Depends(get_orchestrator),
):
    require_app_scope(application_id, claims)
    with _http_errors():
        consent = orchestrator.record_consent_grant(
            application_id,
            purpose,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
        )
    return ConsentGrantResponse(
        consent_id=consent.id, purpose=consent.purpose, granted=consent.consent_granted
    )


@router.post(
    "/{application_id}/verifications/{verification_type}/initiate",
    response_model=InitiateVerificationResponse,
)
def initiate_verification(
    application_id: UUID,
    verification_type: str,
    claims: ApplicantClaims = Depends(get_current_applicant),
    orchestrator: FlowOrchestrator = Depends(get_orchestrator),
):
    require_app_scope(application_id, claims)
    with _http_errors():
        verification = orchestrator.initiate_verification(application_id, verification_type)
    return InitiateVerificationResponse(
        verification_id=verification.id,
        verification_type=verification.verification_type,
        status=verification.status,
        vendor_session_ref=verification.vendor_session_ref,
    )


@router.post(
    "/{application_id}/verifications/{verification_type}/callback",
    response_model=VerificationCallbackResponse,
)
def verification_callback(
    application_id: UUID,
    verification_type: str,
    body: VerificationCallbackBody,
    response: Response,
    claims: ApplicantClaims = Depends(get_current_applicant),
    db: Session = Depends(get_db),
    orchestrator: FlowOrchestrator = Depends(get_orchestrator),
):
    # DEPRECATED (P6.6) — applicant-callable callback for the mock dispatcher.
    # Superseded by the HMAC-verified vendor webhook: POST /api/webhooks/v1/{vendor}/verification.
    # Removal tracked for P7 (see payspyre_backlog.md). Kept functional for continuity.
    logger.warning(
        "applicant_callback_deprecated",
        message="MVP-only applicant callback invoked; use the vendor webhook (P6.6); removal in P7",
        application_id=str(application_id),
        verification_type=verification_type,
    )
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "2026-08-01"
    require_app_scope(application_id, claims)
    mapped = CONSENT_TO_VERIFICATION_TYPE.get(verification_type)
    if mapped is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown verification type '{verification_type}'",
        )
    verification = (
        db.query(PlatformVerification)
        .filter(
            PlatformVerification.application_id == application_id,
            PlatformVerification.verification_type == mapped,
            PlatformVerification.status.in_(_PENDING_STATUSES),
        )
        .order_by(PlatformVerification.started_at.desc())
        .first()
    )
    if verification is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No pending {mapped} verification for this application",
        )
    with _http_errors():
        result = orchestrator.handle_verification_result(
            application_id,
            verification.id,
            vendor_event_id=body.vendor_event_id,
            result=body.result,
            rich_payload=body.rich_payload,
        )
    return VerificationCallbackResponse(
        verification_id=result.verification_id,
        application_status=result.application_status,
        decided=result.decided,
        decision=result.decision,
    )


@router.post("/{application_id}/submit", response_model=SubmitResponse)
def submit_for_decision(
    application_id: UUID,
    claims: ApplicantClaims = Depends(get_current_applicant),
    orchestrator: FlowOrchestrator = Depends(get_orchestrator),
):
    require_app_scope(application_id, claims)
    with _http_errors():
        result = orchestrator.submit_for_decision(application_id)
    return SubmitResponse(
        application_id=result.application_id,
        status=result.status,
        decision=result.decision,
        already_decided=result.already_decided,
    )
