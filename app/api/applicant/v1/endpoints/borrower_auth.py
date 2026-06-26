"""Borrower portal login — email-based magic link for a returning borrower.

Phase 0 of the borrower portal (docs/borrower_portal_spec.md §2). Distinct from
the per-application magic link (``auth.py``): the borrower has no application in
hand, so they sign in by EMAIL and receive a patient-level JWT (sub=patient_id)
covering all their loans. No JWT required on these two endpoints.

Enumeration-safe: ``/request`` always returns the same generic message; the
service only sends a link if the email matches a patient.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.applicant.v1.deps import get_patient_auth_service
from app.api.applicant.v1.schemas import MagicLinkExchangeResponse
from app.services.auth.patient_auth_service import (
    InvalidMagicLinkToken,
    MagicLinkLocked,
    PatientAuthService,
)

router = APIRouter(prefix="/auth/borrower-login", tags=["borrower-auth"])


class BorrowerLoginRequestBody(BaseModel):
    # Plain str (not EmailStr) to avoid the email-validator dependency; the
    # service normalizes (trim + lowercase) and matches case-insensitively.
    email: str = Field(..., min_length=3, max_length=320)


class BorrowerLoginRequestResponse(BaseModel):
    # Borrower login is email-only and enumeration-safe: the service ALWAYS returns
    # the same generic message regardless of whether the account exists, so this
    # response carries only that message (no contact_method — that would leak shape).
    message: str


class BorrowerLoginExchangeBody(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    token: str = Field(..., min_length=4, max_length=12)


@router.post(
    "/request",
    response_model=BorrowerLoginRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_borrower_login(
    body: BorrowerLoginRequestBody,
    service: PatientAuthService = Depends(get_patient_auth_service),
):
    return service.request_borrower_login(body.email)


@router.post("/exchange", response_model=MagicLinkExchangeResponse)
def exchange_borrower_login(
    body: BorrowerLoginExchangeBody,
    service: PatientAuthService = Depends(get_patient_auth_service),
):
    try:
        return service.exchange_borrower_login(body.email, body.token)
    except MagicLinkLocked as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))
    except InvalidMagicLinkToken as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))
