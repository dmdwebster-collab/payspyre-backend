from datetime import datetime, date
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    Column, DateTime, Enum, ForeignKey, Index, Numeric, String, Text,
    func
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.base import Base


class Funding(Base):
    __tablename__ = "funding"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    application_id = Column(UUID(as_uuid=True), ForeignKey("loan_applications.id"), nullable=False)

    disbursement_amount = Column(Numeric(10, 2), nullable=False)
    disbursement_method = Column(
        Enum("etransfer", "wire", "cheque", name="disbursement_method"),
        nullable=False,
    )
    vendor_account_number = Column(String(50), nullable=True)
    vendor_institution_number = Column(String(20), nullable=True)
    vendor_transit_number = Column(String(20), nullable=True)

    disbursement_date = Column(DateTime(timezone=True), nullable=True)
    reference_number = Column(String(100), nullable=True)
    stripe_transfer_id = Column(String(255), nullable=True)

    status = Column(
        Enum("pending", "processing", "completed", "failed", name="funding_status"),
        nullable=False,
        default="pending",
    )
    failure_reason = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_funding_application", "application_id"),
        Index("idx_funding_status", "status"),
    )


class Payment(Base):
    __tablename__ = "payments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    application_id = Column(UUID(as_uuid=True), ForeignKey("loan_applications.id"), nullable=False)

    amount = Column(Numeric(10, 2), nullable=False)
    payment_method = Column(
        Enum("pre_authorized_debit", "etransfer", "cheque", name="payment_method"),
        nullable=False,
    )
    payment_date = Column(DateTime(timezone=True), nullable=False)
    transaction_id = Column(String(100), nullable=True)

    status = Column(
        Enum("pending", "processing", "completed", "failed", "refunded", name="payment_status"),
        nullable=False,
        default="pending",
    )

    principal_amount = Column(Numeric(10, 2), nullable=False, default=0)
    interest_amount = Column(Numeric(10, 2), nullable=False, default=0)
    late_fee_amount = Column(Numeric(10, 2), nullable=False, default=0)
    remaining_balance = Column(Numeric(10, 2), nullable=False)

    failure_reason = Column(Text, nullable=True)
    refunded_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_payment_application", "application_id"),
        Index("idx_payment_status", "status"),
        Index("idx_payment_date", "payment_date"),
    )


class PaymentSchedule(Base):
    __tablename__ = "payment_schedule"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    application_id = Column(UUID(as_uuid=True), ForeignKey("loan_applications.id"), nullable=False)

    payment_number = Column(Numeric(5, 0), nullable=False)
    due_date = Column(DateTime(timezone=True), nullable=False)
    payment_amount = Column(Numeric(10, 2), nullable=False)

    principal_amount = Column(Numeric(10, 2), nullable=False)
    interest_amount = Column(Numeric(10, 2), nullable=False)
    remaining_balance = Column(Numeric(10, 2), nullable=False)

    is_paid = Column(String(20), nullable=False, default="false")

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_schedule_application", "application_id"),
        Index("idx_schedule_due_date", "due_date"),
    )


class Statement(Base):
    __tablename__ = "statements"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    application_id = Column(UUID(as_uuid=True), ForeignKey("loan_applications.id"), nullable=False)

    statement_period_start = Column(DateTime(timezone=True), nullable=False)
    statement_period_end = Column(DateTime(timezone=True), nullable=False)

    total_balance = Column(Numeric(10, 2), nullable=False)
    payment_amount_due = Column(Numeric(10, 2), nullable=False)
    due_date = Column(DateTime(timezone=True), nullable=False)

    payments_received = Column(Numeric(10, 2), nullable=False, default=0)
    interest_accrued = Column(Numeric(10, 2), nullable=False, default=0)
    late_fees = Column(Numeric(10, 2), nullable=False, default=0)

    statement_url = Column(String(500), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_statement_application", "application_id"),
        Index("idx_statement_period", "statement_period_start", "statement_period_end"),
    )


class Refund(Base):
    __tablename__ = "refunds"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    payment_id = Column(UUID(as_uuid=True), ForeignKey("payments.id"), nullable=False)

    amount = Column(Numeric(10, 2), nullable=False)
    reason = Column(Text, nullable=False)
    refund_method = Column(
        Enum("etransfer", "wire", "cheque", "original_payment", name="refund_method"),
        nullable=False,
    )

    status = Column(
        Enum("pending", "processing", "completed", "failed", name="refund_status"),
        nullable=False,
        default="pending",
    )
    reference_number = Column(String(100), nullable=True)
    failure_reason = Column(Text, nullable=True)

    processed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_refund_payment", "payment_id"),
        Index("idx_refund_status", "status"),
    )