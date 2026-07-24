"""Borrower-facing application agreement (activation rework Wave 4a).

The borrower half of Dave's pre-loan lifecycle. Once the underwriter/borrower
has accepted an offer under the activation rework (flag ``ACTIVATION_BOOKS_LOAN``
ON), the agreement is sent + signed on the APPLICATION — BEFORE any loan is
booked (a loan is booked later, at activation). Wave 1 built the service
(``application_agreement``) and the ADMIN e-sign surface; this router exposes the
SAME two operations to the authenticated borrower so they can review + e-sign
their agreement from their own dashboard:

* ``GET  /applications/{id}/agreement``               — the borrower's agreement
  state + the generated agreement reference to review.
* ``POST /applications/{id}/agreement/simulate-sign`` — Simulator-mode e-sign:
  advances ``sent`` -> ``signed`` and stamps ``agreement_signed_at``. LIVE mode
  409s (the real SignNow embedded/redirect flow governs there — out of scope
  here). The agreement must have been ``sent`` to the borrower first.

Both DELEGATE to the Wave 1 ``application_agreement`` service (never fork its
logic) and emit the SAME audit events. Auth + scope mirror ``offers.py``: patient
JWT + per-application scope (``require_app_scope``) — a borrower can only touch
THEIR application.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import (
    ApplicantClaims,
    get_current_applicant,
    require_app_scope,
)
from app.core.logging import get_logger
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.services import application_agreement, integration_mode
from app.services.loan_lifecycle import SIMULATED_AGREEMENT_REF_PREFIX

logger = get_logger(__name__)
router = APIRouter(prefix="/applications", tags=["applicant-agreement"])

_SIGNNOW_PROVIDER = "signnow"


class BorrowerAgreementState(BaseModel):
    application_id: UUID
    agreement_status: str
    agreement_ref: Optional[str] = None
    agreement_signed_at: Optional[datetime] = None
    # SIMULATOR / LIVE — so the borrower UI shows the "Simulate Signing" control
    # only in simulator mode and redirects to real SignNow in live mode.
    mode: str = "simulator"
    # The agreement document to review. ``send_agreement_for_application``
    # produces a REFERENCE (a simulated ``SIMULATED-<id>`` label in simulator
    # mode, or the real SignNow document id in live) rather than an inline HTML
    # snapshot, so this carries that reference; NULL until an agreement is sent.
    document_html_or_ref: Optional[str] = None


class BorrowerSimulateSignResponse(BaseModel):
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
            status_code=status.HTTP_404_NOT_FOUND, detail="Application not found"
        )
    return application


@router.get("/{application_id}/agreement", response_model=BorrowerAgreementState)
def get_agreement(
    application_id: UUID,
    claims: ApplicantClaims = Depends(get_current_applicant),
    db: Session = Depends(get_db),
) -> BorrowerAgreementState:
    """The borrower's application-level agreement state + document to review."""
    require_app_scope(application_id, claims)
    application = _get_application(db, application_id)
    return BorrowerAgreementState(
        application_id=application.id,
        agreement_status=str(application.agreement_status),
        agreement_ref=application.agreement_ref,
        agreement_signed_at=application.agreement_signed_at,
        mode=integration_mode.resolve_mode(db, _SIGNNOW_PROVIDER),
        document_html_or_ref=application.agreement_ref,
    )


@router.post(
    "/{application_id}/agreement/simulate-sign",
    response_model=BorrowerSimulateSignResponse,
)
def simulate_sign(
    application_id: UUID,
    claims: ApplicantClaims = Depends(get_current_applicant),
    db: Session = Depends(get_db),
) -> BorrowerSimulateSignResponse:
    """Borrower Simulate-Signing (SIMULATOR mode only) on their application.

    Advances the application agreement ``sent`` -> ``signed`` and stamps
    ``agreement_signed_at`` by DELEGATING to
    ``application_agreement.simulate_signing_for_application`` (the SAME service
    the admin surface calls — the logic is never forked). LIVE mode 409s: the
    real SignNow embedded/redirect flow governs signing there.

    Precondition: the agreement must have been ``sent`` to the borrower first —
    a borrower cannot sign an agreement they were never sent. If it is still
    ``not_sent`` this 409s with a clear message (already-``signed`` is an
    idempotent no-op; ``declined`` is rejected by the service as a 409).
    """
    require_app_scope(application_id, claims)
    application = _get_application(db, application_id)

    if application.agreement_status == "not_sent":
        # A borrower can only sign what was actually sent to them. (The service
        # tolerates out-of-order not_sent for the real webhook; the borrower
        # surface deliberately does not — there is nothing for them to sign yet.)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Your agreement has not been sent for signature yet.",
        )

    try:
        application = application_agreement.simulate_signing_for_application(
            db, application, actor=f"patient:{claims.patient_id}"
        )
    except application_agreement.ESignModeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    return BorrowerSimulateSignResponse(
        application_id=application.id,
        agreement_status=str(application.agreement_status),
        agreement_ref=application.agreement_ref,
        agreement_signed_at=application.agreement_signed_at,
        mode=integration_mode.resolve_mode(db, _SIGNNOW_PROVIDER),
        simulated=bool(
            application.agreement_ref
            and application.agreement_ref.startswith(SIMULATED_AGREEMENT_REF_PREFIX)
        ),
    )
