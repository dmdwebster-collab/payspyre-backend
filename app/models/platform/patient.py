from datetime import datetime, date
from uuid import UUID, uuid4
from typing import Optional

from sqlalchemy import Column, DateTime, Date, String, Boolean, func
from sqlalchemy.dialects.postgresql import UUID, ENUM
from sqlalchemy.orm import relationship

from app.db.base import Base


class PlatformPatient(Base):
    __tablename__ = "platform_patients"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Identity (canonical, verified where possible — source-tagged in platform_patient_fields)
    legal_first_name = Column(String, nullable=True)
    legal_last_name = Column(String, nullable=True)
    dob = Column(Date, nullable=True)
    email = Column(String, nullable=True)
    phone_e164 = Column(String, nullable=True)

    # SIN — encrypted at rest, NEVER logged, NEVER returned by any API (only
    # sin_last3 is ever exposed). Stores an app-layer Fernet token (string) under
    # the dedicated SIN_ENCRYPTION_KEY — see app/core/sin_crypto.py. The column is
    # TEXT (migration 030 reconciled the original BYTEA drift, audit C-3); String
    # maps to TEXT in Postgres so model + DB now agree.
    sin_encrypted = Column(String, nullable=True)
    sin_last3 = Column(String(3), nullable=True)
    sin_collected_at = Column(DateTime(timezone=True), nullable=True)
    sin_declined = Column(Boolean, nullable=False, default=False)
    sin_declined_at = Column(DateTime(timezone=True), nullable=True)

    # Disbursement: the borrower's payee id at the payments provider (Zumrails).
    # Populated out-of-band by ops/onboarding; loan_lifecycle reads it to disburse.
    zumrails_recipient_id = Column(String, nullable=True)

    # Verification depth (denormalized for fast filtering)
    verification_depth = Column(
        ENUM("none", "email_verified", "phone_verified", "id_verified", "id_bank_verified", "id_bank_cb_verified",
             name="platform_verification_depth", create_type=False),
        nullable=False,
        default="none"
    )

    # Lead state (denormalized for marketplace queries)
    lead_state = Column(
        ENUM("unqualified", "pre_qualified", "pre_approved", "approved", "declined",
             name="platform_lead_state", create_type=False),
        nullable=False,
        default="unqualified"
    )
    lead_state_updated_at = Column(DateTime(timezone=True), nullable=True)

    # Marketing & monetization flags
    marketing_consent_at = Column(DateTime(timezone=True), nullable=True)
    cross_sell_eligible = Column(Boolean, nullable=False, default=False)
    marketplace_listed = Column(Boolean, nullable=False, default=False)

    # Soft-delete only (PIPEDA retention obligations)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    fields = relationship("PlatformPatientField", back_populates="patient", cascade="all, delete-orphan")
    applications = relationship("PlatformCreditApplication", back_populates="patient", cascade="all, delete-orphan")
    verifications = relationship("PlatformVerification", back_populates="patient", cascade="all, delete-orphan")
    consents = relationship("PlatformConsent", back_populates="patient", cascade="all, delete-orphan")
    events = relationship("PlatformEvent", back_populates="patient", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        # No PII (e.g. email) in repr — it lands in tracebacks/logs/error trackers.
        return f"<PlatformPatient(id={self.id}, verification_depth={self.verification_depth})>"
