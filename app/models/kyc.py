from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    Column, DateTime, Enum, ForeignKey, Index, Numeric, String, Text,
    func, text
)
from sqlalchemy.dialects.postgresql import JSONB, UUID, ARRAY
from sqlalchemy.orm import relationship

from app.db.base import Base


class KycSession(Base):
    __tablename__ = "kyc_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    loan_application_id = Column(UUID(as_uuid=True), ForeignKey("loan_applications.id"), nullable=False)
    borrower_id = Column(UUID(as_uuid=True), ForeignKey("borrowers.id"), nullable=False)
    vendor = Column(String(50), nullable=False)
    vendor_session_id = Column(String(255), nullable=True)
    verification_url = Column(Text, nullable=True)
    status = Column(
        Enum("pending", "in_progress", "completed", "expired", "failed", name="kyc_session_status"),
        nullable=False,
        default="pending",
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)
    meta = Column("metadata", JSONB, nullable=True)

    results = relationship("KycResult", back_populates="session", cascade="all, delete-orphan")
    events = relationship("KycEvent", back_populates="session", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_kyc_sessions_loan_app", "loan_application_id"),
        Index("idx_kyc_sessions_status", "status"),
        Index("idx_kyc_sessions_vendor", "vendor"),
    )


class KycResult(Base):
    __tablename__ = "kyc_results"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    kyc_session_id = Column(UUID(as_uuid=True), ForeignKey("kyc_sessions.id"), nullable=False)
    vendor = Column(String(50), nullable=False)
    overall_status = Column(
        Enum("pass", "fail", "review_required", name="kyc_overall_status"),
        nullable=False,
    )
    check_type = Column(
        Enum("identity", "liveness", "aml", "sanctions", name="kyc_check_type"),
        nullable=False,
    )
    check_status = Column(
        Enum("pass", "fail", "pending", "skip", name="kyc_check_status"),
        nullable=False,
    )
    check_details = Column(JSONB, nullable=True)
    score = Column(Numeric(5, 4), nullable=True)
    flags = Column(ARRAY(JSONB), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    session = relationship("KycSession", back_populates="results")

    __table_args__ = (
        Index("idx_kyc_results_session", "kyc_session_id"),
        Index("idx_kcy_results_status", "overall_status"),
    )


class KycEvent(Base):
    __tablename__ = "kyc_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    kyc_session_id = Column(UUID(as_uuid=True), ForeignKey("kyc_sessions.id"), nullable=False)
    event_type = Column(String(100), nullable=False)
    vendor_event_id = Column(String(255), nullable=True)
    payload = Column(JSONB, nullable=False)
    processed_at = Column(DateTime(timezone=True), server_default=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    session = relationship("KycSession", back_populates="events")

    __table_args__ = (
        Index("idx_kyc_events_session", "kyc_session_id"),
        Index("idx_kyc_events_type", "event_type"),
    )


class KycCoBorrowerLink(Base):
    __tablename__ = "kyc_co_borrower_links"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    loan_application_id = Column(UUID(as_uuid=True), ForeignKey("loan_applications.id"), nullable=False)
    primary_kyc_session_id = Column(UUID(as_uuid=True), ForeignKey("kyc_sessions.id"), nullable=False)
    co_borrower_kyc_session_id = Column(UUID(as_uuid=True), ForeignKey("kyc_sessions.id"), nullable=False)
    co_borrower_role = Column(
        Enum("guarantor", "joint_applicant", name="co_borrower_role"),
        nullable=False,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_kyc_co_borrower_loan", "loan_application_id"),
    )


class ManualKybReview(Base):
    __tablename__ = "manual_kyb_reviews"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False)
    business_name = Column(String(255), nullable=False)
    business_structure = Column(
        Enum("sole_proprietor", "partnership", "corporation", "other", name="business_structure"),
        nullable=False,
    )
    business_registration_number = Column(String(100), nullable=True)
    status = Column(
        Enum("pending_submission", "under_review", "approved", "rejected", name="kyb_review_status"),
        nullable=False,
        default="pending_submission",
    )
    submitted_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    reviewed_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    documents = Column(ARRAY(JSONB), nullable=True)
    beneficial_owners = Column(ARRAY(JSONB), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_manual_kyb_vendor", "vendor_id"),
        Index("idx_manual_kyb_status", "status"),
    )