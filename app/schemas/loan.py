from datetime import datetime, date
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class BorrowerCreate(BaseModel):
    first_name: str
    last_name: str
    email: EmailStr
    phone: str
    date_of_birth: date
    address_line1: str
    address_line2: str | None = None
    city: str
    province: str = Field(..., pattern="^(AB|BC)$")
    postal_code: str
    country: str = "CA"
    employment_status: str | None = None
    employer_name: str | None = None
    employment_income: Decimal | None = None
    sin_last_3: str | None = Field(None, pattern="^\\d{3}$")
    credit_score: int | None = None


class BorrowerResponse(BaseModel):
    id: UUID
    first_name: str
    last_name: str
    email: str
    phone: str
    date_of_birth: date
    address_line1: str
    city: str
    province: str
    postal_code: str
    bank_account_verified: str | None
    bank_account_last_4: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class LoanApplicationCreate(BaseModel):
    borrower_id: UUID
    vendor_id: UUID
    co_borrower_id: UUID | None = None
    requested_amount: Decimal = Field(..., gt=0, le=50000)
    purpose: str | None = None
    treatment_description: str | None = None
    credit_product_code: str | None = None
    term_months: int | None = Field(None, ge=3, le=120)
    interest_rate: Decimal | None = Field(None, ge=0, le=0.30)
    payment_frequency: str | None = Field(None, pattern="^(weekly|bi_weekly|semi_monthly|monthly)$")


class LoanApplicationUpdate(BaseModel):
    status: str | None = None
    decision: str | None = None
    decision_reason: str | None = None


class LoanApplicationResponse(BaseModel):
    id: UUID
    borrower_id: UUID
    vendor_id: UUID
    co_borrower_id: UUID | None
    requested_amount: Decimal
    purpose: str | None
    treatment_description: str | None
    status: str
    credit_product_code: str | None
    term_months: int | None
    interest_rate: Decimal | None
    payment_frequency: str | None
    decision: str | None
    decision_reason: str | None
    decision_at: datetime | None
    submitted_at: datetime | None
    approved_at: datetime | None
    funded_at: datetime | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class VendorCreate(BaseModel):
    business_name: str
    dba_name: str | None = None
    business_type: str
    contact_name: str
    email: EmailStr
    phone: str
    address_line1: str
    address_line2: str | None = None
    city: str
    province: str
    postal_code: str
    license_number: str | None = None
    license_expiry: date | None = None


class VendorResponse(BaseModel):
    id: UUID
    business_name: str
    dba_name: str | None
    business_type: str
    contact_name: str
    email: str
    phone: str
    status: str
    created_at: datetime

    class Config:
        from_attributes = True