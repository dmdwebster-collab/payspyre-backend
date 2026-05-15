from datetime import datetime
from uuid import UUID
from typing import Any

from pydantic import BaseModel, Field


class UnderwritingDecisionResponse(BaseModel):
    application_id: UUID
    decision: str = Field(..., pattern="^(approve|reject|manual_review)$")
    reason: str
    risk_score: float
    flags_applied: list[str]
    status: str
    decision_at: datetime | None


class UnderwritingManualReviewRequest(BaseModel):
    application_id: UUID
    approved: bool
    notes: str | None = None


class UnderwritingStatusResponse(BaseModel):
    application_id: UUID
    status: str
    decision: str | None
    decision_reason: str | None
    decision_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UnderwritingEvaluationRequest(BaseModel):
    application_id: UUID