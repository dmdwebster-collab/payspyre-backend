from datetime import datetime
from uuid import UUID, uuid4
from typing import Optional, Any

from sqlalchemy import Column, DateTime, String, Integer, BigInteger, Boolean, func, Text, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB, ENUM
from sqlalchemy.orm import relationship

from app.db.base import Base


class PlatformCreditApplication(Base):
    __tablename__ = "platform_credit_applications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("platform_patients.id"), nullable=False)
    credit_product_id = Column(UUID(as_uuid=True), ForeignKey("platform_credit_products.id"), nullable=False)
    credit_product_version = Column(Integer, nullable=False)
    # Immutable snapshot of the product verification_matrix at creation (migration
    # 026, security finding #6 / Hard Rule #7-8). The decision is made against this,
    # not the live product row. NULL only for rows created before migration 026.
    product_config_snapshot = Column(JSONB, nullable=True)

    # Co-applicant linkage
    co_applicant_of_application_id = Column(UUID(as_uuid=True), ForeignKey("platform_credit_applications.id"), nullable=True)
    applicant_role = Column(
        ENUM("primary", "co_applicant", name="platform_applicant_role", create_type=False),
        nullable=False,
        default="primary"
    )

    # Requested amount: source tagging
    requested_amount_cents = Column(BigInteger, nullable=False)
    requested_amount_source = Column(
        ENUM("clinic", "patient", "clinic_then_patient_adjusted", name="platform_amount_source", create_type=False),
        nullable=False
    )
    clinic_proposed_amount_cents = Column(BigInteger, nullable=True)
    patient_proposed_amount_cents = Column(BigInteger, nullable=True)

    # Origination context
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=True)
    treatment_plan_ref = Column(String, nullable=True)

    # State
    status = Column(
        ENUM("started", "verifying", "pre_qualified", "awaiting_hard_pull", "under_review",
             "approved", "declined", "withdrawn", "expired",
             name="platform_application_status", create_type=False),
        nullable=False,
        default="started"
    )
    status_updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Flow state
    flow_state = Column(JSONB, nullable=False, default=lambda: {})

    # Outcome
    decision = Column(JSONB, nullable=True)
    decision_at = Column(DateTime(timezone=True), nullable=True)
    decision_by = Column(String, nullable=True)

    # Self-reported overrides
    self_reported = Column(JSONB, nullable=False, default=lambda: {})

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    patient = relationship("PlatformPatient", back_populates="applications")
    credit_product = relationship("PlatformCreditProduct", back_populates="applications")
    co_applicant = relationship("PlatformCreditApplication", remote_side=[id], post_update=True)
    verifications = relationship("PlatformVerification", back_populates="application", cascade="all, delete-orphan")
    consents = relationship("PlatformConsent", back_populates="application", cascade="all, delete-orphan")
    events = relationship("PlatformEvent", back_populates="application", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<PlatformCreditApplication(id={self.id}, status={self.status}, patient_id={self.patient_id})>"
