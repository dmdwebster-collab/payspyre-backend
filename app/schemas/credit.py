from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class CreditInquiryRequest(BaseModel):
    sin_last_3: str = Field(..., pattern=r"^\d{3}$")
    date_of_birth: datetime
    postal_code: str
    first_name: str
    last_name: str
    bureaus: list[str] | None = Field(None, description="List of bureaus to query (equifax, transunion)")
    use_cache: bool = True
    loan_application_id: UUID | None = None


class CreditReportResponse(BaseModel):
    id: UUID
    bureau: str
    score: int | None
    score_min: int | None
    score_max: int | None
    utilization_percent: float | None
    delinquency_count: int | None
    credit_history_months: int | None
    trade_count: int | None
    inquiry_count_6m: int | None
    inquiry_count_12m: int | None
    has_bankruptcy: bool
    has_collections: bool
    fetched_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class CreditInquiryResponse(BaseModel):
    id: UUID
    borrower_id: UUID
    loan_application_id: UUID | None
    sin_last_3: str
    first_name: str
    last_name: str
    bureaus_queried: str
    status: str
    error_message: str | None
    cached: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CreditPullResponse(BaseModel):
    inquiry_id: UUID
    status: str
    reports: list[CreditReportResponse]
    aggregated: dict[str, Any]
    errors: dict[str, str] | None
    fetched_at: str


class CreditMetrics(BaseModel):
    average_score: int | None
    min_score: int | None
    max_score: int | None
    average_utilization: float
    total_delinquencies: int
    average_history_months: int
    has_any_bankruptcy: bool
    has_any_collections: bool
    bureau_count: int