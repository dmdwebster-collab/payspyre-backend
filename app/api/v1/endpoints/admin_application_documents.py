"""Admin application-document e-sign surface (activation rework Wave 1).

Mounted under ``/admin/applications``. Mirrors the loan-level e-sign hand-off in
``admin_loan_documents.py`` (send-for-signature + simulate-signing), but targets
the PRE-LOAN agreement state that now lives on ``PlatformCreditApplication``
(migration 078) — the groundwork for signing an agreement BEFORE a loan is
booked.

ADDITIVE this wave: nothing in the current approve/accept path calls these yet.
Same ``admin``/``staff`` RBAC as the loan-document routes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status as http_status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.services import application_agreement, integration_mode
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
