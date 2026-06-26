"""Manual credit-application path (applicant API).

The application has two population modes — *automated* (Didit/Flinks pull the
data) and *manual*. Manual is the fallback when an integration fails (ID not
recognized, bank verification fails): instead of a hard stop, the borrower
completes a manual data-entry application, which is then flagged for MANUAL
verification/review.

Per Dave's spec, "the underlying credit application does not change, only the
manner in which the information is populated." So this endpoint does NOT create a
new application or alter the schema — it records the manually-entered fields on
the existing application's ``self_reported`` JSONB, stamps a manual-review marker
into ``flow_state``, moves the application to ``under_review`` and writes a
``manual_application_submitted`` PlatformEvent. It deliberately does NOT run the
automated decisioning core (``FlowOrchestrator._decide``): a manual application
awaits a HUMAN decision, so it is left ``under_review``.

This is usable for TESTING before live integrations exist.

The router (``router``, prefix ``/applications``) is registered by the router
assembly in ``app/api/applicant/v1/router.py`` (the orchestrator wires it in;
this file does not edit that module).
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import (
    ApplicantClaims,
    get_current_applicant,
    require_app_scope,
)
from app.api.applicant.v1.schemas import (
    ManualApplicationBody,
    ManualApplicationResponse,
)
from app.core.logging import get_logger
from app.core.sin_crypto import encrypt_sin
from app.core.validation import normalize_sin
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.patient import PlatformPatient
from app.services.flow_orchestrator import mark_manual_review
from app.models.platform.event import PlatformEvent

logger = get_logger(__name__)
router = APIRouter(prefix="/applications", tags=["applicant-manual-application"])

# Statuses from which a manual application may be submitted: the pre-decision
# states (the integration-failure fallback happens before any automated
# decision) plus an idempotent re-submit while already under_review. A manual
# application must never overwrite a terminal decision.
_MANUAL_SUBMITTABLE = ("started", "verifying", "under_review")


@router.post(
    "/{application_id}/manual",
    response_model=ManualApplicationResponse,
)
def submit_manual_application(
    application_id: UUID,
    body: ManualApplicationBody,
    claims: ApplicantClaims = Depends(get_current_applicant),
    db: Session = Depends(get_db),
):
    """Capture a MANUAL application and route it to manual verification/review.

    Persists the manually-entered fields (normally pulled from integrations) into
    ``self_reported``, sets ``flow_state.manual_application = true``, moves the
    application to ``under_review`` and emits ``manual_application_submitted``.
    Does NOT auto-decide — a manual application awaits a human review.

    Idempotent/defensive: re-submitting while already ``under_review`` refreshes
    the captured fields and re-stamps the marker. A manual application is refused
    (409) once the application has reached a terminal decision
    (approved/declined/withdrawn/expired) — those must not be silently reopened.
    """
    require_app_scope(application_id, claims)

    application = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if application is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Application not found"
        )

    if application.status not in _MANUAL_SUBMITTABLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot submit a manual application in status "
                f"'{application.status}'"
            ),
        )

    now = datetime.now(timezone.utc)

    # The manually-entered data — the fields normally populated by Didit/Flinks.
    # Persisted under a stable ``manual`` sub-key of self_reported so the human
    # reviewer (and the marketplace denorm) can distinguish manual-entry data
    # from any other self-reported overrides. JSON-serializable shapes only
    # (date → ISO string) so the JSONB column round-trips cleanly.
    # The full v1.00 application field set; mode="json" serializes dates → ISO
    # so the JSONB column round-trips cleanly.
    manual_fields = body.model_dump(mode="json")

    # SECURITY: the SIN is the most sensitive identifier we collect. It must NEVER
    # sit in the self_reported JSONB in plaintext — route it through the SAME
    # encrypted storage as the automated /sin path (Fernet token on the patient,
    # only sin_last3 retained). We replace the raw SIN in manual_fields with the
    # masked last-3 so the reviewer can see it was provided without exposing it.
    raw_sin = manual_fields.pop("social_insurance_number", None)
    if raw_sin:
        digits = normalize_sin(raw_sin)
        patient = (
            db.query(PlatformPatient)
            .filter(PlatformPatient.id == application.patient_id)
            .first()
        )
        if patient is not None:
            patient.sin_encrypted = encrypt_sin(digits)
            patient.sin_last3 = digits[-3:]
            patient.sin_collected_at = now
            patient.sin_declined = False
            patient.sin_declined_at = None
            db.add(patient)
        manual_fields["sin_last3"] = digits[-3:]

    # Copy-on-write: SQLAlchemy only flags a JSONB column dirty on reassignment,
    # not on in-place mutation of the existing dict.
    self_reported = dict(application.self_reported or {})
    self_reported["manual"] = manual_fields
    self_reported["manual_submitted_at"] = now.isoformat()
    application.self_reported = self_reported

    flow_state = dict(application.flow_state or {})
    flow_state["manual_application"] = True
    flow_state["manual_application_at"] = now.isoformat()
    application.flow_state = flow_state

    before_status = application.status
    # Status transitions are owned by the orchestrator (spec §4.3) — delegate the
    # under_review move there rather than writing application.status inline.
    mark_manual_review(application)
    application.status_updated_at = now

    event = PlatformEvent(
        event_type="manual_application_submitted",
        actor="patient",
        patient_id=application.patient_id,
        application_id=application.id,
        payload={
            "v": 1,
            "actor": {"type": "patient", "id": str(claims.patient_id)},
            "application_id": str(application.id),
            "patient_id": str(application.patient_id),
            "before": {"status": before_status},
            "after": {"status": application.status, "manual_application": True},
            # Captured-field keys only — never the PII values — so the event log
            # stays PII-free (mirrors the orchestrator's §6 event convention).
            "metadata": {"manual_fields": sorted(manual_fields.keys())},
        },
    )
    db.add(event)
    db.commit()
    db.refresh(application)

    logger.info(
        "manual_application_submitted",
        application_id=str(application.id),
        patient_id=str(application.patient_id),
        before_status=before_status,
        status=application.status,
    )

    return ManualApplicationResponse(
        application_id=application.id,
        status=application.status,
        manual_application=True,
    )
