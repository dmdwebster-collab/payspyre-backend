from datetime import datetime, date
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class VendorCreate(BaseModel):
    business_name: str = Field(..., min_length=1, max_length=255)
    dba_name: Optional[str] = Field(None, max_length=255)
    business_type: str = Field(..., pattern="^(corporation|partnership|sole_proprietor)$")
    contact_name: str = Field(..., min_length=1, max_length=255)
    email: EmailStr
    phone: str = Field(..., min_length=10, max_length=20)
    address_line1: str = Field(..., min_length=1, max_length=255)
    address_line2: Optional[str] = Field(None, max_length=255)
    city: str = Field(..., min_length=1, max_length=100)
    province: str = Field(..., min_length=2, max_length=50)
    postal_code: str = Field(..., min_length=5, max_length=10)
    license_number: Optional[str] = Field(None, max_length=100)
    license_expiry: Optional[date] = None


class VendorUpdate(BaseModel):
    dba_name: Optional[str] = Field(None, max_length=255)
    contact_name: Optional[str] = Field(None, max_length=255)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, min_length=10, max_length=20)
    address_line1: Optional[str] = Field(None, max_length=255)
    address_line2: Optional[str] = Field(None, max_length=255)
    city: Optional[str] = Field(None, max_length=100)
    province: Optional[str] = Field(None, max_length=50)
    postal_code: Optional[str] = Field(None, max_length=10)
    license_number: Optional[str] = Field(None, max_length=100)
    license_expiry: Optional[date] = None


class VendorResponse(BaseModel):
    id: UUID
    business_name: str
    dba_name: Optional[str]
    business_type: str
    contact_name: str
    email: str
    phone: str
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


class VendorDetailResponse(VendorResponse):
    address_line1: str
    address_line2: Optional[str]
    city: str
    province: str
    postal_code: str
    license_number: Optional[str]
    license_expiry: Optional[date]
    updated_at: datetime


class OnboardingDocumentCreate(BaseModel):
    document_type: str = Field(..., pattern="^(business_license|incorporation|tax_id|insurance|w9|other)$")
    document_url: str = Field(..., min_length=1)
    description: Optional[str] = None


class OnboardingDocumentResponse(BaseModel):
    id: UUID
    vendor_id: UUID
    document_type: str
    document_url: str
    description: Optional[str]
    status: str
    reviewed_at: Optional[datetime]
    review_notes: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class ComplianceStatusResponse(BaseModel):
    vendor_id: UUID
    business_name: str
    status: str
    license_verified: bool
    license_expiry_valid: bool
    documents_required: int
    documents_submitted: int
    documents_approved: int
    compliance_score: Decimal
    last_reviewed_at: Optional[datetime]
    next_review_due: Optional[datetime]


class ComplianceReviewCreate(BaseModel):
    license_verified: bool
    notes: Optional[str] = None
    next_review_due: Optional[date] = None


class VendorMetricsResponse(BaseModel):
    vendor_id: UUID
    business_name: str
    total_applications: int
    approved_applications: int
    rejected_applications: int
    funded_applications: int
    total_funded_amount: Decimal
    average_funding_amount: Decimal
    approval_rate: Decimal
    average_decision_time_hours: Optional[Decimal]
    current_month_volume: Decimal
    current_month_count: int
    last_activity_at: Optional[datetime]


class VendorListResponse(BaseModel):
    vendors: List[VendorResponse]
    total: int
    skip: int
    limit: int