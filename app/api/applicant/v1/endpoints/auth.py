"""Applicant magic-link auth endpoints (P6.5). No JWT required on these two."""
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.applicant.v1.deps import get_patient_auth_service
from app.api.applicant.v1.schemas import (
    MagicLinkExchangeBody,
    MagicLinkExchangeResponse,
    MagicLinkRequestBody,
    MagicLinkRequestResponse,
)
from app.services.auth.patient_auth_service import (
    ApplicationNotFound,
    InvalidMagicLinkToken,
    PatientAuthService,
)

router = APIRouter(prefix="/auth", tags=["applicant-auth"])


@router.post(
    "/magic-link/request",
    response_model=MagicLinkRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_magic_link(
    body: MagicLinkRequestBody,
    service: PatientAuthService = Depends(get_patient_auth_service),
):
    try:
        result = service.request_magic_link(body.application_id, body.contact_method)
    except ApplicationNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return result


@router.post("/magic-link/exchange", response_model=MagicLinkExchangeResponse)
def exchange_magic_link(
    body: MagicLinkExchangeBody,
    service: PatientAuthService = Depends(get_patient_auth_service),
):
    try:
        return service.exchange_magic_link(body.application_id, body.token)
    except InvalidMagicLinkToken as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))
