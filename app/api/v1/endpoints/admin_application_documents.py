"""Admin application-document surface: agreement PREVIEW + e-sign hand-off.

Mounted under ``/admin/applications``. Two things live here:

* **e-sign (activation rework Wave 1)** — send-for-signature + simulate-signing,
  mirroring ``admin_loan_documents.py`` but targeting the PRE-LOAN agreement
  state on ``PlatformCreditApplication`` (migration 078).
* **agreement preview (QC)** — ``GET /{id}/documents/agreement-preview`` renders
  what WOULD be sent, from the application's current terms, so staff can verify
  accuracy before anything reaches the borrower. Read-only and never persisted;
  see :mod:`app.services.application_agreement_preview`.

Same ``admin``/``staff`` RBAC as the loan-document routes.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status as http_status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.services import (
    application_agreement,
    application_agreement_preview,
    integration_mode,
)
from app.services.loan_lifecycle import SIMULATED_AGREEMENT_REF_PREFIX

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])


class SendForSignatureResponse(BaseModel):
    application_id: UUID
    agreement_status: str
    agreement_ref: Optional[str] = None
    # SIMULATOR / LIVE — so the UI labels a simulated send honestly and shows the
    # "Simulate Signing" control only in simulator mode.
    mode: str = "simulator"
    simulated: bool = False


class SimulateSigningResponse(BaseModel):
    application_id: UUID
    agreement_status: str
    agreement_ref: Optional[str] = None
    agreement_signed_at: Optional[datetime] = None
    mode: str = "simulator"
    simulated: bool = True


class FieldNoteOut(BaseModel):
    """One merge field that did not resolve to a real value."""

    field: str
    #: Where the value should have come from — the QC hint.
    source: str
    #: Why it is not there.
    reason: str


class TermsSnapshotOut(BaseModel):
    """The terms the preview merged from (so the figures can be tied back)."""

    principal_cents: Optional[int] = None
    annual_rate_bps: Optional[int] = None
    apr_bps: Optional[int] = None
    term_months: Optional[int] = None
    payment_frequency: str = "monthly"
    terms_source: str = "unknown"
    first_due_date: Optional[date] = None
    start_date: Optional[date] = None
    installment_count: int = 0
    regular_installment_cents: Optional[int] = None
    total_of_payments_cents: Optional[int] = None
    total_interest_cents: Optional[int] = None
    total_fees_cents: Optional[int] = None
    finance_charge_cents: Optional[int] = None
    per_payment_fee_cents: int = 0
    origination_fee_cents: int = 0


class AgreementPreviewResponse(BaseModel):
    application_id: UUID
    application_status: str
    #: ALWAYS true. This endpoint cannot return an executed document.
    is_preview: bool = True
    disclaimer: str
    generated_at: datetime
    #: 'db_template' (the configured loan_agreement template) or
    #: 'builtin_skeleton' (generic terms data sheet — no template configured).
    template_source: str
    template_id: Optional[UUID] = None
    template_version: Optional[int] = None
    title: str
    html: str
    #: Required fields with no source yet — rendered as [NOT AVAILABLE: Field].
    missing_fields: list[FieldNoteOut] = Field(default_factory=list)
    #: Fields that legitimately do not apply (no co-borrower, uncharged fee).
    not_applicable_fields: list[FieldNoteOut] = Field(default_factory=list)
    #: Template placeholders the engine does not know at all (template typos).
    unknown_fields: list[str] = Field(default_factory=list)
    #: Free-text QC signals (defaulted dates, criminal-rate APR, …).
    warnings: list[str] = Field(default_factory=list)
    terms: TermsSnapshotOut
    #: The full resolved merge context, for field-by-field review.
    merge_data: dict[str, str] = Field(default_factory=dict)


def _notes(notes) -> list[FieldNoteOut]:
    return [
        FieldNoteOut(field=n.field, source=n.source, reason=n.reason) for n in notes
    ]


def _get_application(db: Session, application_id: UUID) -> PlatformCreditApplication:
    application = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if application is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Application not found"
        )
    return application


def _actor_str(user) -> Optional[str]:
    uid = getattr(user, "id", None)
    return str(uid) if uid is not None else None


@router.get(
    "/{application_id}/documents/agreement-preview",
    response_model=AgreementPreviewResponse,
)
def agreement_preview(
    application_id: UUID,
    db: Session = Depends(get_db),
) -> AgreementPreviewResponse:
    """Preview the loan agreement for a PENDING application (quality control).

    Dave's QC step: render what WOULD be sent to the borrower, from the
    application's CURRENT terms, so staff can verify accuracy before
    ``send-for-signature``.

    This is a PREVIEW, never an executed document:
      * nothing is persisted — no ``platform_loan_documents`` row, no e-sign
        ref, no state change on the application;
      * it is regenerated on every call, so it always reflects the current
        terms and can never be served stale;
      * the disclaimer is rendered INTO the returned HTML (not only carried in
        this envelope), so a printout is self-identifying.

    Merge fields that cannot be resolved are rendered as
    ``[NOT AVAILABLE: <Field>]`` and listed in ``missing_fields`` — a QC preview
    must expose gaps, never hide them behind a blank cell. Fields that
    legitimately do not apply (no co-borrower; a fee the product does not
    charge) are separated into ``not_applicable_fields`` so real gaps stand out.

    Deliberately NOT status-gated: an agreement preview is read-only and is
    useful at every stage of underwriting, not just one status.
    """
    application = _get_application(db, application_id)
    preview = application_agreement_preview.generate_agreement_preview(db, application)
    return AgreementPreviewResponse(
        application_id=application.id,
        application_status=application.status,
        is_preview=True,
        disclaimer=application_agreement_preview.PREVIEW_DISCLAIMER,
        generated_at=preview.generated_at,
        template_source=preview.template_source,
        template_id=preview.template_id,
        template_version=preview.template_version,
        title=preview.title,
        html=preview.html,
        missing_fields=_notes(preview.missing_fields),
        not_applicable_fields=_notes(preview.not_applicable_fields),
        unknown_fields=list(preview.unknown_fields),
        warnings=list(preview.warnings),
        terms=TermsSnapshotOut(**preview.terms.as_dict()),
        merge_data=preview.merge_data,
    )


@router.post(
    "/{application_id}/documents/send-for-signature",
    response_model=SendForSignatureResponse,
)
def send_for_signature(
    application_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
) -> SendForSignatureResponse:
    """Send the (pre-loan) application agreement into the e-sign flow.

    Delegates to ``application_agreement.send_agreement_for_application``
    (forward-only, idempotent). MODE-AWARE: in SIMULATOR mode this really
    transitions the agreement to ``sent`` with a labelled simulated ref, so the
    flow can be reviewed and completed via ``simulate-signing``; in LIVE mode it
    fires the real SignNow invite (a graceful no-op if creds are absent).
    """
    application = _get_application(db, application_id)
    application = application_agreement.send_agreement_for_application(
        db, application, actor=_actor_str(user)
    )
    mode = integration_mode.resolve_mode(db, "signnow")
    return SendForSignatureResponse(
        application_id=application.id,
        agreement_status=application.agreement_status,
        agreement_ref=application.agreement_ref,
        mode=mode,
        simulated=bool(
            application.agreement_ref
            and application.agreement_ref.startswith(SIMULATED_AGREEMENT_REF_PREFIX)
        ),
    )


@router.post(
    "/{application_id}/documents/simulate-signing",
    response_model=SimulateSigningResponse,
)
def simulate_signing(
    application_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
) -> SimulateSigningResponse:
    """Complete a SIMULATED e-signature on the application (Simulate Signing).

    Simulator mode only — drives the SAME ``agreement -> signed`` transition the
    real SignNow completion webhook drives and stamps ``agreement_signed_at``.
    Rejected 409 when SignNow is in LIVE mode (the real signing flow applies
    there). The result is labelled ``simulated: true``.
    """
    application = _get_application(db, application_id)
    try:
        application = application_agreement.simulate_signing_for_application(
            db, application, actor=_actor_str(user)
        )
    except application_agreement.ESignModeError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)
        )
    return SimulateSigningResponse(
        application_id=application.id,
        agreement_status=application.agreement_status,
        agreement_ref=application.agreement_ref,
        agreement_signed_at=application.agreement_signed_at,
        mode=integration_mode.resolve_mode(db, "signnow"),
        simulated=True,
    )
