"""Borrower 2FA endpoints (WS-J item 1 — bank-level step-up security).

Enrollment + step-up verification for the second factor layered on top of the
magic-link login. All routes require the patient JWT; the step-up token these
routes mint is what the SENSITIVE endpoints (profile edits, bank default,
payments) demand via ``require_step_up``.

Re-enrolling while ACTIVE (e.g. lost authenticator) is itself a sensitive
action and is gated behind a fresh step-up.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import (
    ApplicantClaims,
    get_current_applicant,
    require_step_up,
)
from app.db.base import get_db
from app.services.borrower_security import (
    TwoFactorInvalidCode,
    TwoFactorLocked,
    TwoFactorService,
    TwoFactorStateError,
)

router = APIRouter(prefix="/security/2fa", tags=["borrower-security"])


class TwoFactorStatus(BaseModel):
    enrolled: bool
    method: Optional[str] = None
    status: Optional[str] = None
    enforced: bool = False
    step_up_required: bool


class EnrollBody(BaseModel):
    method: str = Field(..., pattern="^(totp|sms)$")
    sms_phone_e164: Optional[str] = Field(None, max_length=20)


class EnrollResponse(BaseModel):
    method: str
    status: str
    # TOTP only — the single time the secret is ever surfaced.
    totp_secret: Optional[str] = None
    otpauth_uri: Optional[str] = None
    message: Optional[str] = None


class CodeBody(BaseModel):
    code: str = Field(..., min_length=4, max_length=10)


class ActivateResponse(BaseModel):
    method: str
    status: str


class ChallengeResponse(BaseModel):
    method: str
    message: str


class StepUpResponse(BaseModel):
    step_up_token: str
    expires_at: str


def _map_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, TwoFactorLocked):
        return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))
    if isinstance(exc, TwoFactorInvalidCode):
        return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))
    if isinstance(exc, TwoFactorStateError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    raise exc


@router.get("", response_model=TwoFactorStatus)
def get_status(
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    service = TwoFactorService(db)
    row = service.get_state(claims.patient_id)
    return TwoFactorStatus(
        enrolled=row is not None and row.status == "active",
        method=row.method if row else None,
        status=row.status if row else None,
        enforced=bool(row.enforced) if row else False,
        step_up_required=service.step_up_required(claims.patient_id),
    )


@router.post("/enroll", response_model=EnrollResponse)
def enroll(
    body: EnrollBody,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
    step_up_claims: ApplicantClaims = Depends(require_step_up),
):
    """Start enrollment. For a patient whose 2FA is already ACTIVE this is a
    re-key and ``require_step_up`` demands a fresh step-up token first; for a
    first-time (or pending) enrollment the step-up gate passes through."""
    service = TwoFactorService(db)
    try:
        result = service.enroll(
            claims.patient_id,
            body.method,
            sms_phone_e164=body.sms_phone_e164,
            account_label=str(claims.patient_id),
        )
    except (TwoFactorStateError, TwoFactorInvalidCode, TwoFactorLocked) as exc:
        raise _map_errors(exc)
    return EnrollResponse(**result)


@router.post("/enroll/verify", response_model=ActivateResponse)
def verify_enrollment(
    body: CodeBody,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    service = TwoFactorService(db)
    try:
        result = service.activate(claims.patient_id, body.code)
    except (TwoFactorStateError, TwoFactorInvalidCode, TwoFactorLocked) as exc:
        raise _map_errors(exc)
    return ActivateResponse(**result)


@router.post("/challenge", response_model=ChallengeResponse)
def challenge(
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    service = TwoFactorService(db)
    try:
        result = service.challenge(claims.patient_id)
    except (TwoFactorStateError, TwoFactorInvalidCode, TwoFactorLocked) as exc:
        raise _map_errors(exc)
    return ChallengeResponse(**result)


@router.post("/verify", response_model=StepUpResponse)
def verify(
    body: CodeBody,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    """Verify a fresh code → a 10-minute step-up token for sensitive actions."""
    service = TwoFactorService(db)
    try:
        result = service.verify_step_up(claims.patient_id, body.code)
    except (TwoFactorStateError, TwoFactorInvalidCode, TwoFactorLocked) as exc:
        raise _map_errors(exc)
    return StepUpResponse(**result)
