from datetime import datetime
from uuid import UUID, uuid4
from typing import Optional

from sqlalchemy import Column, DateTime, String, Integer, func, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, ENUM
from sqlalchemy.orm import relationship

from app.db.base import Base


class PlatformVerification(Base):
    __tablename__ = "platform_verifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("platform_patients.id"), nullable=False)
    application_id = Column(UUID(as_uuid=True), ForeignKey("platform_credit_applications.id"), nullable=True)

    verification_type = Column(
        ENUM("kyc_id", "bank_link", "bureau_soft", "bureau_hard", "income_attestation", "address_proof",
             name="platform_verification_type", create_type=False),
        nullable=False
    )

    status = Column(
        ENUM("pending", "in_progress", "passed", "failed", "expired",
             name="platform_verification_status", create_type=False),
        nullable=False,
        default="pending"
    )

    vendor = Column(String, nullable=True)
    vendor_session_ref = Column(String, nullable=True)
    cost_cents = Column(Integer, nullable=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    consent_id = Column(UUID(as_uuid=True), ForeignKey("platform_consents.id"), nullable=True)

    # Relationships
    patient = relationship("PlatformPatient", back_populates="verifications")
    application = relationship("PlatformCreditApplication", back_populates="verifications")
    consent = relationship("PlatformConsent")

    def __repr__(self) -> str:
        return f"<PlatformVerification(id={self.id}, type={self.verification_type}, status={self.status})>"
