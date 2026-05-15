from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    Column, DateTime, Enum, ForeignKey, Index, JSON, Numeric, String, Text,
    Boolean, func
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.base import Base


class PaymentMethod(Base):
    __tablename__ = "payment_methods"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    borrower_id = Column(UUID(as_uuid=True), ForeignKey("borrowers.id"), nullable=False)

    # Stripe payment method details
    stripe_payment_method_id = Column(String(255), nullable=True)
    stripe_customer_id = Column(String(255), nullable=True)
    payment_method_type = Column(
        Enum("card", "us_bank_account", "pad", name="payment_method_type"),
        nullable=False,
    )

    # Card details (if applicable)
    card_last_4 = Column(String(4), nullable=True)
    card_brand = Column(String(50), nullable=True)
    card_exp_month = Column(Numeric(2, 0), nullable=True)
    card_exp_year = Column(Numeric(4, 0), nullable=True)

    # Bank account details (if applicable)
    bank_account_last_4 = Column(String(4), nullable=True)
    bank_account_bank_name = Column(String(255), nullable=True)

    # Status
    is_default = Column(Boolean, nullable=False, default=False)
    is_verified = Column(Boolean, nullable=False, default=False)
    status = Column(
        Enum("active", "inactive", "expired", "failed", name="payment_method_status"),
        nullable=False,
        default="active",
    )

    # Metadata
    stripe_response = Column(JSON, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    borrower = relationship("Borrower", backref="payment_methods")
    transactions = relationship("StripeTransaction", back_populates="payment_method")

    __table_args__ = (
        Index("idx_payment_method_borrower", "borrower_id"),
        Index("idx_payment_method_stripe", "stripe_payment_method_id"),
        Index("idx_payment_method_status", "status"),
        Index("idx_payment_method_default", "is_default"),
    )


class StripeAccount(Base):
    __tablename__ = "stripe_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False)

    # Stripe Connect account details
    stripe_account_id = Column(String(255), nullable=False, unique=True)
    stripe_account_type = Column(
        Enum("express", "standard", "custom", name="stripe_account_type"),
        nullable=False,
        default="express",
    )

    # Onboarding status
    onboarding_status = Column(
        Enum("not_started", "pending", "completed", "rejected", name="onboarding_status"),
        nullable=False,
        default="not_started",
    )
    onboarding_completed_at = Column(DateTime(timezone=True), nullable=True)

    # Account verification status
    charges_enabled = Column(Boolean, nullable=False, default=False)
    payouts_enabled = Column(Boolean, nullable=False, default=False)
    details_submitted = Column(Boolean, nullable=False, default=False)

    # Payout settings
    default_payout_schedule = Column(
        Enum("manual", "daily", "weekly", "monthly", name="payout_schedule"),
        nullable=False,
        default="manual",
    )

    # Status
    status = Column(
        Enum("active", "restricted", "suspended", "closed", name="stripe_account_status"),
        nullable=False,
        default="active",
    )

    # Metadata
    stripe_account_data = Column(JSON, nullable=True)
    onboarding_url = Column(String(500), nullable=True)
    onboarding_url_expires_at = Column(DateTime(timezone=True), nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    vendor = relationship("Vendor", backref="stripe_accounts")
    transactions = relationship("StripeTransaction", back_populates="stripe_account")

    __table_args__ = (
        Index("idx_stripe_account_vendor", "vendor_id"),
        Index("idx_stripe_account_stripe_id", "stripe_account_id"),
        Index("idx_stripe_account_status", "status"),
    )


class StripeTransaction(Base):
    __tablename__ = "stripe_transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    # Links to our models
    application_id = Column(UUID(as_uuid=True), ForeignKey("loan_applications.id"), nullable=True)
    payment_method_id = Column(UUID(as_uuid=True), ForeignKey("payment_methods.id"), nullable=True)
    stripe_account_id = Column(UUID(as_uuid=True), ForeignKey("stripe_accounts.id"), nullable=True)
    payment_id = Column(UUID(as_uuid=True), ForeignKey("payments.id"), nullable=True)
    refund_id = Column(UUID(as_uuid=True), ForeignKey("refunds.id"), nullable=True)

    # Stripe identifiers
    stripe_payment_intent_id = Column(String(255), nullable=True)
    stripe_transfer_id = Column(String(255), nullable=True)
    stripe_payout_id = Column(String(255), nullable=True)
    stripe_charge_id = Column(String(255), nullable=True)
    stripe_refund_id = Column(String(255), nullable=True)

    # Transaction details
    transaction_type = Column(
        Enum(
            "payment",
            "disbursement",
            "payout",
            "refund",
            "transfer",
            "fee",
            name="transaction_type",
        ),
        nullable=False,
    )
    amount = Column(Numeric(10, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="cad")
    status = Column(
        Enum(
            "pending",
            "succeeded",
            "failed",
            "canceled",
            "processing",
            name="stripe_transaction_status",
        ),
        nullable=False,
        default="pending",
    )

    # Fees
    stripe_fee = Column(Numeric(10, 2), nullable=True)
    application_fee = Column(Numeric(10, 2), nullable=True)

    # Transfer group (for related transactions)
    transfer_group = Column(String(100), nullable=True)

    # Failure handling
    failure_code = Column(String(50), nullable=True)
    failure_message = Column(Text, nullable=True)

    # Metadata
    stripe_response = Column(JSON, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    processed_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    payment_method = relationship("PaymentMethod", back_populates="transactions")
    stripe_account = relationship("StripeAccount", back_populates="transactions")

    __table_args__ = (
        Index("idx_stripe_transaction_application", "application_id"),
        Index("idx_stripe_transaction_type", "transaction_type"),
        Index("idx_stripe_transaction_status", "status"),
        Index("idx_stripe_transaction_payment_intent", "stripe_payment_intent_id"),
        Index("idx_stripe_transaction_transfer", "stripe_transfer_id"),
        Index("idx_stripe_transaction_group", "transfer_group"),
    )


class StripeWebhookEvent(Base):
    __tablename__ = "stripe_webhook_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    # Stripe event details
    stripe_event_id = Column(String(255), nullable=False, unique=True)
    event_type = Column(String(100), nullable=False)
    api_version = Column(String(50), nullable=True)

    # Event data
    event_data = Column(JSON, nullable=False)

    # Processing status
    processed = Column(Boolean, nullable=False, default=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    processing_error = Column(Text, nullable=True)

    # Related entities (if applicable)
    related_transaction_id = Column(UUID(as_uuid=True), ForeignKey("stripe_transactions.id"), nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    related_transaction = relationship("StripeTransaction", backref="webhook_events")

    __table_args__ = (
        Index("idx_stripe_webhook_event_id", "stripe_event_id"),
        Index("idx_stripe_webhook_type", "event_type"),
        Index("idx_stripe_webhook_processed", "processed"),
        Index("idx_stripe_webhook_created", "created_at"),
    )


class StripePayout(Base):
    __tablename__ = "stripe_payouts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    stripe_account_id = Column(UUID(as_uuid=True), ForeignKey("stripe_accounts.id"), nullable=False)

    # Stripe payout details
    stripe_payout_id = Column(String(255), nullable=False, unique=True)
    amount = Column(Numeric(10, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="cad")
    arrival_date = Column(DateTime(timezone=True), nullable=True)

    # Status
    status = Column(
        Enum("pending", "in_transit", "paid", "failed", "canceled", name="payout_status"),
        nullable=False,
        default="pending",
    )

    # Failure handling
    failure_code = Column(String(50), nullable=True)
    failure_message = Column(Text, nullable=True)

    # Destination details
    destination_bank_name = Column(String(255), nullable=True)
    destination_last_4 = Column(String(4), nullable=True)

    # Metadata
    stripe_response = Column(JSON, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    stripe_account = relationship("StripeAccount", backref="payouts")

    __table_args__ = (
        Index("idx_stripe_payout_account", "stripe_account_id"),
        Index("idx_stripe_payout_stripe_id", "stripe_payout_id"),
        Index("idx_stripe_payout_status", "status"),
    )