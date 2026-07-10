"""Applicant Review & Finalize (canonical credit application).

Two endpoints for the applicant's own review-and-finalize screen:

* ``GET  /applications/{id}/detail``    — the applicant's FULL canonical
  application (the same projection the admin workspace sees), so the frontend can
  render the review screen pre-filled with whatever integrations / mock-fill
  already populated. SIN is surfaced only as ``sin_last3``.
* ``POST /applications/{id}/finalize``  — accepts the applicant's corrected /
  completed field values, persists them onto the canonical columns (+ secondary-
  income child rows), marks the application finalized, and routes it into the
  decision/underwriting flow.

Routing (per Dave): a TEST/MOCK application — i.e. when the platform is in
simulation mode (``USE_REAL_ADAPTERS`` off), so no real bureau/KYC/bank vendor
will ever return a result — is routed to MANUAL underwriting (human review). When
real adapters are configured, finalize moves the application into the VERIFICATION
state so the normal automated flow (verifications → decision) can proceed.

STATUS OWNERSHIP: this module NEVER assigns ``application.status`` directly. All
status transitions go through the orchestrator's transition functions
(``mark_manual_review`` / ``mark_verification``), which own the workflow (spec
§4.3, enforced by tests/test_application_status_writes.py + the money-path
guardrail).

SIN: the finalize body carries no SIN. SIN collection stays on the dedicated
encrypted ``/sin`` path. Nothing here reads or writes the raw/encrypted SIN.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

from app.api.applicant.v1.deps import (
    ApplicantClaims,
    get_current_applicant,
    require_app_scope,
)
from app.api.applicant.v1.schemas import (
    FinalizeApplicationBody,
    FinalizeApplicationResponse,
)
from app.api.platform_application_serialization import (
    CanonicalApplicationDetail,
    build_canonical_detail,
)
from app.core.application_history import (
    ADDRESS_SNAPSHOT_FIELDS,
    EMPLOYMENT_SNAPSHOT_FIELDS,
    snapshot_prior_address,
    snapshot_prior_employment,
    validate_history_entries,
)
from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import get_db
from app.models.platform.application_history import (
    PlatformApplicationAddressHistory,
    PlatformApplicationEmploymentHistory,
)
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.event import PlatformEvent
from app.models.platform.patient import PlatformPatient
from app.models.platform.secondary_income import PlatformApplicationSecondaryIncome
from app.services.flow_orchestrator import mark_manual_review, mark_verification

logger = get_logger(__name__)
router = APIRouter(prefix="/applications", tags=["applicant-finalize"])

# Canonical scalar columns the finalize body may write (SIN excluded by design).
# ``secondary_incomes`` is handled separately (child rows).
_FINALIZE_COLUMNS = (
    # Personal
    "first_name", "middle_name", "last_name", "date_of_birth", "marital_status",
    "number_of_dependents", "citizenship", "education", "main_phone",
    "alternative_phone", "email",
    # ID
    "id_type", "id_number", "id_province_of_issue", "id_expiry",
    # Residence
    "residence_street", "residence_unit", "residence_city", "residence_province",
    "residence_postal_code", "time_at_address_years", "time_at_address_months",
    "residential_status", "monthly_housing_payment_cents",
    # Primary income
    "income_type", "net_monthly_income_cents", "next_pay_date", "pay_frequency",
    "employer_name", "hire_date", "job_title", "work_phone", "work_phone_ext",
    "ok_to_contact_at_work",
    # Financial
    "number_of_credit_accounts", "car_ownership", "monthly_car_payment_cents",
    "non_discretionary_expenses_cents",
    # Co-borrower separate-file linking metadata (migration 046)
    "relationship_to_primary",
)

# Body fields handled as child-row collections, not scalar columns.
_COLLECTION_FIELDS = ("secondary_incomes", "address_history", "employment_history")

# Statuses from which the applicant may finalize: the pre-decision states plus an
# idempotent re-finalize while already routed (under_review / verifying /
# underwriting). A finalize must NEVER reopen a terminal decision.
_FINALIZABLE = (
    "started", "origination", "verifying", "underwriting", "under_review",
)


@router.get("/{application_id}/detail", response_model=CanonicalApplicationDetail)
def get_application_detail(
    application_id: UUID,
    claims: ApplicantClaims = Depends(get_current_applicant),
    db: Session = Depends(get_db),
):
    """The applicant's own full canonical application for the review screen.

    Auth-scoped to the owning applicant (patient JWT + per-application scope).
    Read-only; no state change. SIN is exposed only as ``sin_last3``.
    """
    require_app_scope(application_id, claims)
    application = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .options(
            joinedload(PlatformCreditApplication.secondary_incomes),
            joinedload(PlatformCreditApplication.address_history),
            joinedload(PlatformCreditApplication.employment_history),
        )
        .first()
    )
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    patient = (
        db.query(PlatformPatient)
        .filter(PlatformPatient.id == application.patient_id)
        .first()
    )
    # Co-borrower separate-file linking: a primary file lists its linked
    # co-borrower application files (each a full application of its own).
    linked_ids = [
        row[0]
        for row in db.query(PlatformCreditApplication.id)
        .filter(
            PlatformCreditApplication.co_applicant_of_application_id == application_id
        )
        .all()
    ]
    return build_canonical_detail(application, patient, linked_application_ids=linked_ids)


@router.post("/{application_id}/finalize", response_model=FinalizeApplicationResponse)
def finalize_application(
    application_id: UUID,
    body: FinalizeApplicationBody,
    claims: ApplicantClaims = Depends(get_current_applicant),
    db: Session = Depends(get_db),
):
    """Persist the applicant's corrected fields and route into underwriting.

    Only fields the client actually sent are written (``exclude_unset``) — an
    absent field means "leave as-is". Secondary-income lines are replaced wholesale
    ONLY when ``secondary_incomes`` is present in the payload.

    Routing (per Dave): simulation/mock mode → MANUAL underwriting (human review);
    real-adapter mode → VERIFICATION. Both transitions go through the orchestrator.

    Idempotent/defensive: re-finalizing while already routed refreshes the fields
    and re-emits the event. Refused (409) once the application is terminal.
    """
    require_app_scope(application_id, claims)

    application = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .options(joinedload(PlatformCreditApplication.secondary_incomes))
        .first()
    )
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")

    if application.status not in _FINALIZABLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot finalize an application in status '{application.status}'",
        )

    now = datetime.now(timezone.utc)
    today = now.date()
    provided = body.model_dump(exclude_unset=True)

    # --- validate 3-year history coverage (when history lists are sent) ------
    # Coverage window: 3 years back (config) or since age of majority. DOB comes
    # from the payload when sent, else the stored canonical column.
    dob = provided.get("date_of_birth", application.date_of_birth)
    history_errors: list[str] = []
    if body.address_history is not None:
        history_errors += validate_history_entries(
            body.address_history, today=today, date_of_birth=dob, label="address_history"
        )
    if body.employment_history is not None:
        history_errors += validate_history_entries(
            body.employment_history, today=today, date_of_birth=dob, label="employment_history"
        )
    if history_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=history_errors
        )

    # Capture the prior CURRENT address/employment values before applying the
    # edit, so an edited current entry is VERSIONED into history, not lost.
    prior_address = {col: getattr(application, col) for col in ADDRESS_SNAPSHOT_FIELDS}
    prior_employment = {
        col: getattr(application, col) for col in EMPLOYMENT_SNAPSHOT_FIELDS
    }
    prior_employment["income_type"] = (
        str(application.income_type) if application.income_type is not None else None
    )

    # --- persist scalar canonical fields (only those the client sent) --------
    changed_fields: list[str] = []
    for col in _FINALIZE_COLUMNS:
        if col in provided:
            setattr(application, col, provided[col])
            changed_fields.append(col)

    # --- version-not-overwrite: snapshot the prior current entry -------------
    # Only when the client did NOT send an explicit history list in the same
    # request (an explicit list is the applicant's full declared picture and
    # replaces the applicant-declared rows below).
    if body.address_history is None:
        snap = snapshot_prior_address(prior_address, provided, today=today)
        if snap is not None:
            application.address_history.append(
                PlatformApplicationAddressHistory(application_id=application.id, **snap)
            )
            changed_fields.append("address_history_versioned")
    if body.employment_history is None:
        snap = snapshot_prior_employment(prior_employment, provided, today=today)
        if snap is not None:
            application.employment_history.append(
                PlatformApplicationEmploymentHistory(application_id=application.id, **snap)
            )
            changed_fields.append("employment_history_versioned")

    # --- secondary-income child rows (replace-on-present) --------------------
    if body.secondary_incomes is not None:
        # Wipe existing lines then re-create from the payload. cascade
        # delete-orphan on the relationship removes the detached rows on flush.
        application.secondary_incomes = []
        db.flush()
        for line in body.secondary_incomes:
            application.secondary_incomes.append(
                PlatformApplicationSecondaryIncome(
                    application_id=application.id,
                    **line.model_dump(exclude_unset=False),
                )
            )
        changed_fields.append("secondary_incomes")

    # --- history child rows (replace-on-present, preserving versioned rows) --
    if body.address_history is not None:
        application.address_history = [
            row for row in application.address_history
            if row.entry_source == "versioned_edit"
        ]
        db.flush()
        for entry in body.address_history:
            application.address_history.append(
                PlatformApplicationAddressHistory(
                    application_id=application.id,
                    entry_source="applicant",
                    **entry.model_dump(exclude_unset=False),
                )
            )
        changed_fields.append("address_history")
    if body.employment_history is not None:
        application.employment_history = [
            row for row in application.employment_history
            if row.entry_source == "versioned_edit"
        ]
        db.flush()
        for entry in body.employment_history:
            application.employment_history.append(
                PlatformApplicationEmploymentHistory(
                    application_id=application.id,
                    entry_source="applicant",
                    **entry.model_dump(exclude_unset=False),
                )
            )
        changed_fields.append("employment_history")

    # --- route into the decision/underwriting flow ---------------------------
    # A test/mock application (simulation mode: no real vendor will return a
    # verification result) goes to MANUAL underwriting; a real-adapter application
    # enters VERIFICATION so the automated flow can run. Status transitions are
    # owned by the orchestrator — delegate, never assign application.status here.
    before_status = application.status
    if settings.USE_REAL_ADAPTERS:
        mark_verification(application)
        routed_to = "verification"
    else:
        mark_manual_review(application)
        routed_to = "manual_review"
    application.status_updated_at = now

    # Mark finalized in flow_state (copy-on-write: JSONB only flags dirty on
    # reassignment). The status enum has no dedicated "finalized" value — the
    # finalize is recorded here + via the event, and expressed by the routing move.
    flow_state = dict(application.flow_state or {})
    flow_state["finalized"] = True
    flow_state["finalized_at"] = now.isoformat()
    flow_state["finalize_routed_to"] = routed_to
    application.flow_state = flow_state

    event = PlatformEvent(
        event_type="application_finalized",
        actor="patient",
        patient_id=application.patient_id,
        application_id=application.id,
        payload={
            "v": 1,
            "actor": {"type": "patient", "id": str(claims.patient_id)},
            "application_id": str(application.id),
            "patient_id": str(application.patient_id),
            "before": {"status": before_status},
            "after": {"status": application.status, "routed_to": routed_to},
            # Field KEYS only — never the PII values — keeping the event log PII-free.
            "metadata": {"changed_fields": sorted(changed_fields)},
        },
    )
    db.add(event)
    db.commit()
    db.refresh(application)

    logger.info(
        "application_finalized",
        application_id=str(application.id),
        patient_id=str(application.patient_id),
        before_status=before_status,
        status=application.status,
        routed_to=routed_to,
    )

    return FinalizeApplicationResponse(
        application_id=application.id,
        status=application.status,
        finalized=True,
        routed_to=routed_to,
    )
