from datetime import datetime
from uuid import UUID, uuid4
from typing import Optional

from sqlalchemy import Column, DateTime, String, Boolean, Text, func, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, INET, ENUM
from sqlalchemy.orm import relationship

from app.db.base import Base


class PlatformConsent(Base):
    __tablename__ = "platform_consents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("platform_patients.id"), nullable=False)

    purpose = Column(
        ENUM("id_verification", "bank_verification", "soft_bureau_pull", "hard_bureau_pull",
             "automated_decision_making", "marketing_email", "marketplace_listing",
             "cross_sell_eligibility", "aggregate_data_use",
             name="platform_consent_purpose", create_type=False),
        nullable=False
    )

    consent_granted = Column(Boolean, nullable=False)
    consent_text_shown = Column(Text, nullable=False)  # IMMUTABLE
    consent_text_version = Column(String, nullable=False)  # IMMUTABLE
    granted_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    ip_address = Column(INET(), nullable=True)
    user_agent = Column(String, nullable=True)
    application_id = Column(UUID(as_uuid=True), ForeignKey("platform_credit_applications.id"), nullable=True)

    # Relationships
    patient = relationship("PlatformPatient", back_populates="consents")
    application = relationship("PlatformCreditApplication", back_populates="consents")
    verifications = relationship("PlatformVerification", back_populates="consent")

    def __repr__(self) -> str:
        return f"<PlatformConsent(id={self.id}, purpose={self.purpose}, granted={self.consent_granted})>"
