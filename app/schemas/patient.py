from datetime import datetime, date
from typing import Optional, Any, Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator


class FieldSource:
    SELF_REPORTED = "self_reported"
    PRACTICE_PREFILL = "practice_prefill"
    ID_DOC = "id_doc"
    BUREAU = "bureau"


class PatientQuickstartRequest(BaseModel):
    legal_first_name: str = Field(..., min_length=1, max_length=100)
    legal_last_name: str = Field(..., min_length=1, max_length=100)
    email: EmailStr
    phone_e164: Optional[str] = Field(None, max_length=20)
    dob: Optional[date] = None


class PatientFieldUpdateRequest(BaseModel):
    field_key: str = Field(..., min_length=1, max_length=50)
    value: Any
    source: Literal["self_reported", "practice_prefill", "id_doc", "bureau"]
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)


class PatientFieldValue(BaseModel):
    field_key: str
    field_value: Any
    source: str
    confidence: Optional[float]
    verified_at: datetime


class FieldDiscrepancy(BaseModel):
    field_key: str
    values: dict[str, Any]  # source -> value mapping
    detected_at: datetime


class PatientProfileResponse(BaseModel):
    id: UUID
    legal_first_name: Optional[str]
    legal_last_name: Optional[str]
    dob: Optional[date]
    email: Optional[str]
    phone_e164: Optional[str]
    verification_depth: str
    lead_state: str
    created_at: datetime
    updated_at: datetime

    # Source-tagged fields (current values only)
    fields: list[PatientFieldValue]

    class Config:
        from_attributes = True


class DiscrepancyResponse(BaseModel):
    patient_id: UUID
    discrepancies: list[FieldDiscrepancy]
    total_count: int


class PatientFieldHistoryResponse(BaseModel):
    field_key: str
    history: list[PatientFieldValue]


class MessageResponse(BaseModel):
    message: str
