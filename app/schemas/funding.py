from datetime import datetime, date
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class FundingRequest(BaseModel):
    disbursement_amount: Decimal = Field(..., gt=0)
    disbursement_method: str = Field(..., pattern="^(etransfer|wire|cheque)$")
    vendor_account_number: str | None = None
    vendor_institution_number: str | None = None
    vendor_transit_number: str | None = None


class FundingResponse(BaseModel):
    id: UUID
    application_id: UUID
    disbursement_amount: Decimal
    disbursement_method: str
    disbursement_date: datetime
    reference_number: str
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


class FundingStatusResponse(BaseModel):
    application_id: UUID
    funding_status: str
    disbursement_amount: Decimal | None
    disbursement_date: datetime | None
    reference_number: str | None
    funded_at: datetime | None


class PaymentCreate(BaseModel):
    application_id: UUID
    amount: Decimal = Field(..., gt=0)
    payment_method: str = Field(..., pattern="^(pre_authorized_debit|etransfer|cheque)$")
    payment_date: date | None = None
    transaction_id: str | None = None


class PaymentResponse(BaseModel):
    id: UUID
    application_id: UUID
    amount: Decimal
    payment_method: str
    payment_date: date
    status: str
    transaction_id: str | None
    principal_amount: Decimal
    interest_amount: Decimal
    late_fee_amount: Decimal
    remaining_balance: Decimal
    created_at: datetime

    class Config:
        from_attributes = True


class PaymentScheduleItem(BaseModel):
    payment_number: int
    due_date: date
    payment_amount: Decimal
    principal_amount: Decimal
    interest_amount: Decimal
    remaining_balance: Decimal
    is_paid: bool


class StatementResponse(BaseModel):
    id: UUID
    application_id: UUID
    statement_period_start: date
    statement_period_end: date
    total_balance: Decimal
    payment_amount_due: Decimal
    due_date: date
    payments_received: Decimal
    interest_accrued: Decimal
    late_fees: Decimal
    statement_url: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class RefundRequest(BaseModel):
    amount: Decimal = Field(..., gt=0)
    reason: str
    refund_method: str | None = None


class RefundResponse(BaseModel):
    id: UUID
    payment_id: UUID
    amount: Decimal
    reason: str
    refund_method: str
    status: str
    reference_number: str
    processed_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True