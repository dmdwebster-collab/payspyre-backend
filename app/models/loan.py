from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    Column, DateTime, Enum, ForeignKey, Index, Numeric, String, Text,
    func
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.base import Base


class LoanApplication(Base):
    __tablename__ = "loan_applications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    borrower_id = Column(UUID(as_uuid=True), ForeignKey("borrowers.id"), nullable=False)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False)
    co_borrower_id = Column(UUID(as_uuid=True), ForeignKey("borrowers.id"), nullable=True)

    # Application details
    requested_amount = Column(Numeric(10, 2), nullable=False)
    purpose = Column(String(255), nullable=True)
    treatment_description = Column(Text, nullable=True)

    # Status
    status = Column(
        Enum(
            "draft",
            "pending_documents",
            "underwriting",
            "approved",
            "rejected",
            "funded",
            "active",
            "closed",
            name="application_status",
        ),
        nullable=False,
        default="draft",
    )

    # Credit product
    credit_product_code = Column(String(50), nullable=True)
    term_months = Column(Numeric(5, 0), nullable=True)
    interest_rate = Column(Numeric(5, 4), nullable=True)
    payment_frequency = Column(
        Enum("weekly", "bi_weekly", "semi_monthly", "monthly", name="payment_frequency"),
        nullable=True,
    )

    # Decision
    decision = Column(
        Enum("approved", "rejected", "manual_review", name="decision_type"),
        nullable=True,
    )
    decision_reason = Column(Text, nullable=True)
    decision_at = Column(DateTime(timezone=True), nullable=True)

    # Timestamps
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    funded_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    borrower = relationship("Borrower", foreign_keys=[borrower_id])
    co_borrower = relationship("Borrower", foreign_keys=[co_borrower_id])
    vendor = relationship("Vendor", back_populates="applications")

    __table_args__ = (
        Index("idx_loan_app_borrower", "borrower_id"),
        Index("idx_loan_app_vendor", "vendor_id"),
        Index("idx_loan_app_status", "status"),
    )


class Borrower(Base):
    __tablename__ = "borrowers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    # Personal info
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    email = Column(String(255), nullable=False)
    phone = Column(String(20), nullable=False)
    date_of_birth = Column(DateTime, nullable=False)

    # Address
    address_line1 = Column(String(255), nullable=False)
    address_line2 = Column(String(255), nullable=True)
    city = Column(String(100), nullable=False)
    province = Column(String(50), nullable=False)
    postal_code = Column(String(10), nullable=False)
    country = Column(String(2), nullable=False, default="CA")

    # Employment
    employment_status = Column(
        Enum("employed", "self_employed", "unemployed", "retired", "student", name="employment_status"),
        nullable=True,
    )
    employer_name = Column(String(255), nullable=True)
    employment_income = Column(Numeric(10, 2), nullable=True)

    # Credit
    sin_last_3 = Column(String(3), nullable=True)
    credit_score = Column(Numeric(5, 0), nullable=True)
    credit_history_months = Column(Numeric(5, 0), nullable=True)
    credit_utilization = Column(Numeric(5, 2), nullable=True)

    # Bank verification
    bank_account_verified = Column(String(50), nullable=True)  # Flinks session ID
    bank_account_last_4 = Column(String(4), nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Vendor(Base):
    __tablename__ = "vendors"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    # Business info
    business_name = Column(String(255), nullable=False)
    dba_name = Column(String(255), nullable=True)
    business_type = Column(
        Enum("corporation", "partnership", "sole_proprietor", name="business_type"),
        nullable=False,
    )

    # Contact
    contact_name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False)
    phone = Column(String(20), nullable=False)

    # Address
    address_line1 = Column(String(255), nullable=False)
    address_line2 = Column(String(255), nullable=True)
    city = Column(String(100), nullable=False)
    province = Column(String(50), nullable=False)
    postal_code = Column(String(10), nullable=False)

    # Licensing
    license_number = Column(String(100), nullable=True)
    license_expiry = Column(DateTime, nullable=True)

    # Status
    status = Column(
        Enum("pending", "active", "suspended", "terminated", name="vendor_status"),
        nullable=False,
        default="pending",
    )

    # Compliance tracking
    compliance_score = Column(Numeric(5, 2), nullable=True, default=Decimal("0.00"))
    last_reviewed_at = Column(DateTime(timezone=True), nullable=True)
    next_review_due = Column(DateTime(timezone=True), nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    applications = relationship("LoanApplication", back_populates="vendor")