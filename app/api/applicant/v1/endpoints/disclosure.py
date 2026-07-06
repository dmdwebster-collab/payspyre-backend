"""Consolidated disclosure + consent acceptance (applicant API).

Dave's spec: per-item consent prompts are cumbersome. This endpoint shows ONE
consolidated disclosure block (after loan-term selection, before the
application) and captures ALL currently-required disclosures + consent in a
single acceptance, instead of granting each consent purpose individually.

``POST /applications/{id}/consents/accept-all`` (patient-JWT scoped):
  - derives the required purposes from EXACTLY the same source as
    ``GET /applications/{id}/consents`` (``orchestrator.get_required_consents``,
    which reads the application's product verification_matrix + appends
    ``automated_decision_making``) — the list is NEVER hardcoded;
  - records consent (``granted=True``) for each required purpose via the
    orchestrator's ``record_consent_grant`` (which also emits the per-purpose
    ``consent_granted`` event, matching the per-item endpoint exactly);
  - writes a single ``consolidated_disclosure_accepted`` PlatformEvent capturing
    the full list of purposes covered + the disclosure version;
  - is idempotent: re-accepting skips purposes that already have an active grant,
    so it is a safe no-op (still returns ok with the full granted-purpose list).

The existing per-item endpoints in ``applications.py`` are UNCHANGED — this is
purely additive.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

import app.services.consent_service as consent_service
from app.api.applicant.v1.deps import (
    ApplicantClaims,
    get_current_applicant,
    get_orchestrator,
    require_app_scope,
)
from app.api.applicant.v1.endpoints.applications import _client_ip, _http_errors
from app.api.applicant.v1.schemas import (
    AcceptAllConsentsBody,
    AcceptAllConsentsResponse,
    ApplicationDisclaimerResponse,
)
from app.core.logging import get_logger
from app.db.base import get_db
from app.models.platform.event import PlatformEvent
from app.services.flow_orchestrator import FlowOrchestrator

logger = get_logger(__name__)

# Registered by the orchestrator in app/api/applicant/v1/router.py.
router = APIRouter(prefix="/applications", tags=["applicant-applications"])

# The canonical full application disclaimer (Dave's exact wording) is stored as a
# versioned, immutable consent-text file and served verbatim by the loader. This is
# the single source of truth for the in-flow application attestation.
_APPLICATION_DISCLAIMER_PURPOSE = "application_disclaimer"


@router.get(
    "/{application_id}/disclosure",
    response_model=ApplicationDisclaimerResponse,
)
def get_application_disclosure(
    application_id: UUID,
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    """Serve the canonical, versioned full application disclaimer text in-flow."""
    require_app_scope(application_id, claims)
    disclaimer = consent_service.get_active_consent_text(_APPLICATION_DISCLAIMER_PURPOSE)
    return ApplicationDisclaimerResponse(
        purpose=disclaimer.purpose,
        version=disclaimer.version,
        text=disclaimer.text,
    )


@router.post(
    "/{application_id}/consents/accept-all",
    response_model=AcceptAllConsentsResponse,
)
def accept_all_consents(
    application_id: UUID,
    request: Request,
    body: AcceptAllConsentsBody = AcceptAllConsentsBody(),
    claims: ApplicantClaims = Depends(get_current_applicant),
    orchestrator: FlowOrchestrator = Depends(get_orchestrator),
    db: Session = Depends(get_db),
):
    """Record consent for ALL currently-required purposes in one call."""
    require_app_scope(application_id, claims)

    with _http_errors():
        # Reuse the SAME derivation the per-item GET .../consents uses — never a
        # hardcoded list. Raises ApplicationNotFoundError (-> 404) for an unknown app.
        required = orchestrator.get_required_consents(application_id)

        # Resolve the patient + application once so we can (a) skip already-granted
        # purposes (idempotency) and (b) reference patient_id on the event.
        application = orchestrator._get_application(application_id)
        patient_id = application.patient_id

        already_granted = {
            c.purpose
            for c in consent_service.get_active_consents_for_patient(db, patient_id)
            if c.consent_granted
        }

        ip = _client_ip(request)
        ua = request.headers.get("user-agent", "")

        # Capture the canonical application-disclaimer version the applicant is
        # attesting to, so the acceptance event is an auditable purpose+version
        # record of the exact language shown.
        disclaimer = consent_service.get_active_consent_text(
            _APPLICATION_DISCLAIMER_PURPOSE
        )

        for purpose in required:
            if purpose in already_granted:
                continue  # idempotent: a re-accept is a no-op for granted purposes
            orchestrator.record_consent_grant(
                application_id,
                purpose,
                ip_address=ip,
                user_agent=ua,
            )

        # A single consolidated-disclosure acceptance event capturing every
        # purpose covered. Mirrors the orchestrator's §6 payload shape (no PII).
        payload = {
            "v": 1,
            "actor": {"type": "patient", "id": str(patient_id)},
            "application_id": str(application.id),
            "patient_id": str(patient_id),
            "before": {},
            "after": {
                "accepted": bool(body.accepted),
                "purposes": list(required),
                "disclosure_version": body.disclosure_version,
                "application_disclaimer_purpose": disclaimer.purpose,
                "application_disclaimer_version": disclaimer.version,
            },
            "metadata": {"ip_address": ip, "user_agent": ua},
        }
        event = PlatformEvent(
            event_type="consolidated_disclosure_accepted",
            actor="patient",
            patient_id=patient_id,
            application_id=application.id,
            payload=payload,
        )
        db.add(event)
        db.commit()
        db.refresh(event)

    logger.info(
        "consolidated_disclosure_accepted",
        application_id=str(application_id),
        patient_id=str(patient_id),
        purpose_count=len(required),
        accepted=bool(body.accepted),
    )

    return AcceptAllConsentsResponse(
        application_id=application_id,
        accepted=bool(body.accepted),
        granted_purposes=list(required),
        disclosure_version=body.disclosure_version,
        application_disclaimer_version=disclaimer.version,
    )
