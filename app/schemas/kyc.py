from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class KycSessionCreate(BaseModel):
    loan_application_id: UUID
    borrower_id: UUID
    vendor: str = Field(..., pattern="^(didit|persona)$")


class KycSessionResponse(BaseModel):
    kyc_session_id: UUID
    verification_url: str
    expires_at: datetime

    class Config:
        from_attributes = True


class KycSessionStatus(BaseModel):
    id: UUID
    loan_application_id: UUID
    borrower_id: UUID
    vendor: str
    vendor_session_id: str | None
    verification_url: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None

    class Config:
        from_attributes = True


class DiditWebhookPayload(BaseModel):
    event: str
    data: dict


class PersonaWebhookPayload(BaseModel):
    event: str
    data: dict


class KycWebhookResponse(BaseModel):
    received: bool
    kyc_session_id: UUID | None = None


class KycCheck(BaseModel):
    type: str
    status: str
    details: dict | None = None


class KycResultEvent(BaseModel):
    event_id: str
    kyc_session_id: UUID
    loan_application_id: UUID
    vendor: str
    overall_status: str
    checks: list[KycCheck]
    flags: list[dict]
    timestamp: datetime


class RiskEvaluationRequest(BaseModel):
    kyc_session_id: UUID
    loan_application_id: UUID


class RiskEvaluationResponse(BaseModel):
    decision: str = Field(..., pattern="^(approve|reject|manual_review)$")
    reason: str
    risk_score: float
    flags_applied: list[str]


class CoBorrowerLinkRequest(BaseModel):
    loan_application_id: UUID
    primary_kyc_session_id: UUID
    co_borrower_kyc_session_id: UUID
    co_borrower_role: str = Field(..., pattern="^(guarantor|joint_applicant)$")


class CoBorrowerLinkResponse(BaseModel):
    id: UUID
    loan_application_id: UUID
    primary_kyc_session_id: UUID
    co_borrower_kyc_session_id: UUID
    co_borrower_role: str
    created_at: datetime

    class Config:
        from_attributes = True