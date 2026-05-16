from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List
from uuid import UUID

from pydantic import BaseModel, Field


class PaymentMethodCreate(BaseModel):
    payment_method_type: str = Field(..., pattern="^(card|us_bank_account|pad)$")
    stripe_payment_method_id: Optional[str] = None
    stripe_customer_id: Optional[str] = None
    card_last_4: Optional[str] = None
    card_brand: Optional[str] = None
    card_exp_month: Optional[int] = None
    card_exp_year: Optional[int] = None
    bank_account_last_4: Optional[str] = None
    bank_account_bank_name: Optional[str] = None
    is_default: bool = False


class PaymentMethodResponse(BaseModel):
    id: UUID
    borrower_id: UUID
    stripe_payment_method_id: Optional[str]
    stripe_customer_id: Optional[str]
    payment_method_type: str
    card_last_4: Optional[str]
    card_brand: Optional[str]
    card_exp_month: Optional[int]
    card_exp_year: Optional[int]
    bank_account_last_4: Optional[str]
    bank_account_bank_name: Optional[str]
    is_default: bool
    is_verified: bool
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


class StripeAccountCreate(BaseModel):
    vendor_id: UUID
    stripe_account_type: str = Field(default="express", pattern="^(express|standard|custom)$")
    default_payout_schedule: str = Field(default="manual", pattern="^(manual|daily|weekly|monthly)$")


class StripeAccountOnboardingRequest(BaseModel):
    refresh_url: str
    return_url: str


class StripeAccountOnboardingResponse(BaseModel):
    url: str
    expires_at: int


class StripeAccountResponse(BaseModel):
    id: UUID
    vendor_id: UUID
    stripe_account_id: str
    stripe_account_type: str
    onboarding_status: str
    onboarding_completed_at: Optional[datetime]
    charges_enabled: bool
    payouts_enabled: bool
    details_submitted: bool
    default_payout_schedule: str
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


class StripeAccountDetailResponse(StripeAccountResponse):
    onboarding_url: Optional[str]
    onboarding_url_expires_at: Optional[datetime]
    stripe_account_data: Optional[dict]
    updated_at: datetime


class StripeTransactionCreate(BaseModel):
    application_id: Optional[UUID] = None
    payment_method_id: Optional[UUID] = None
    stripe_account_id: Optional[UUID] = None
    payment_id: Optional[UUID] = None
    refund_id: Optional[UUID] = None
    transaction_type: str = Field(..., pattern="^(payment|disbursement|payout|refund|transfer|fee)$")
    amount: Decimal = Field(..., gt=0)
    currency: str = Field(default="cad", min_length=3, max_length=3)
    stripe_payment_intent_id: Optional[str] = None
    stripe_transfer_id: Optional[str] = None
    stripe_payout_id: Optional[str] = None
    stripe_charge_id: Optional[str] = None
    stripe_refund_id: Optional[str] = None
    transfer_group: Optional[str] = None


class StripeTransactionResponse(BaseModel):
    id: UUID
    application_id: Optional[UUID]
    payment_method_id: Optional[UUID]
    stripe_account_id: Optional[UUID]
    payment_id: Optional[UUID]
    refund_id: Optional[UUID]
    stripe_payment_intent_id: Optional[str]
    stripe_transfer_id: Optional[str]
    stripe_payout_id: Optional[str]
    stripe_charge_id: Optional[str]
    stripe_refund_id: Optional[str]
    transaction_type: str
    amount: Decimal
    currency: str
    status: str
    stripe_fee: Optional[Decimal]
    application_fee: Optional[Decimal]
    transfer_group: Optional[str]
    failure_code: Optional[str]
    failure_message: Optional[str]
    created_at: datetime
    processed_at: Optional[datetime]

    class Config:
        from_attributes = True


class StripeWebhookEventCreate(BaseModel):
    stripe_event_id: str
    event_type: str
    api_version: Optional[str] = None
    event_data: dict


class StripeWebhookEventResponse(BaseModel):
    id: UUID
    stripe_event_id: str
    event_type: str
    api_version: Optional[str]
    processed: bool
    processed_at: Optional[datetime]
    processing_error: Optional[str]
    related_transaction_id: Optional[UUID]
    created_at: datetime

    class Config:
        from_attributes = True


class StripePayoutCreate(BaseModel):
    stripe_account_id: UUID
    amount: Optional[Decimal] = None
    currency: str = Field(default="cad", min_length=3, max_length=3)


class StripePayoutResponse(BaseModel):
    id: UUID
    stripe_account_id: UUID
    stripe_payout_id: str
    amount: Decimal
    currency: str
    arrival_date: Optional[datetime]
    status: str
    failure_code: Optional[str]
    failure_message: Optional[str]
    destination_bank_name: Optional[str]
    destination_last_4: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class CustomerCreate(BaseModel):
    borrower_id: UUID
    email: str
    name: str
    phone: Optional[str] = None


class CustomerResponse(BaseModel):
    customer_id: str
    borrower_id: UUID


class PaymentIntentCreate(BaseModel):
    amount: Decimal = Field(..., gt=0)
    currency: str = Field(default="cad", min_length=3, max_length=3)
    customer_id: Optional[str] = None
    payment_method_id: Optional[str] = None
    application_id: Optional[UUID] = None
    metadata: Optional[dict] = None
    confirm: bool = False
    off_session: bool = False


class PaymentIntentResponse(BaseModel):
    payment_intent_id: str
    client_secret: Optional[str]
    amount: Decimal
    currency: str
    status: str
    application_id: Optional[UUID]


class PaymentIntentConfirmRequest(BaseModel):
    payment_method_id: Optional[str] = None


class DisbursementCreate(BaseModel):
    amount: Decimal = Field(..., gt=0)
    vendor_stripe_account_id: str
    application_id: Optional[UUID] = None
    reference_number: Optional[str] = None


class DisbursementResponse(BaseModel):
    success: bool
    transfer_id: Optional[str]
    transfer_group: Optional[str]
    error: Optional[str]


class PaymentMethodRegistrationRequest(BaseModel):
    payment_method_type: str = Field(..., pattern="^(card|us_bank_account|pad)$")
    token: Optional[str] = None
    is_default: bool = False


class PaymentMethodRegistrationResponse(BaseModel):
    success: bool
    payment_method_id: Optional[str] = None
    customer_id: Optional[str] = None
    error: Optional[str] = None


class SetupIntentCreate(BaseModel):
    customer_id: str
    payment_method_types: Optional[List[str]] = None


class SetupIntentResponse(BaseModel):
    setup_intent_id: str
    client_secret: str


class RefundCreate(BaseModel):
    payment_intent_id: str
    amount: Optional[Decimal] = None
    reason: str = "requested_by_customer"
    metadata: Optional[dict] = None


class RefundResponse(BaseModel):
    success: bool
    refund_id: Optional[str]
    error: Optional[str]


class BalanceResponse(BaseModel):
    available: List[dict]
    pending: List[dict]


class VendorAccountBalanceResponse(BaseModel):
    available: List[dict]
    pending: List[dict]
    vendor_id: UUID