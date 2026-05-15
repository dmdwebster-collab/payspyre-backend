from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    Column, DateTime, ForeignKey, Index, JSON, Numeric, String, Text, func
)
from sqlalchemy.dialects.postgresql import UUID, ENUM
from sqlalchemy.orm import relationship

from app.db.base import Base


class CreditInquiry(Base):
    __tablename__ = "credit_inquiries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    borrower_id = Column(UUID(as_uuid=True), ForeignKey("borrowers.id", ondelete="CASCADE"), nullable=False)
    loan_application_id = Column(UUID(as_uuid=True), ForeignKey("loan_applications.id"), nullable=True)

    # Query parameters
    sin_last_3 = Column(String(3), nullable=False)
    date_of_birth = Column(DateTime, nullable=False)
    postal_code = Column(String(10), nullable=False)
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)

    # Request details
    bureaus_queried = Column(String(100), nullable=False)
    use_cache = Column(String(10), nullable=False, default="true")
    initiated_by = Column(String(100), nullable=True)

    # Response
    status = Column(
        ENUM("pending", "success", "partial", "failed", name="inquiry_status"),
        nullable=False,
        default="pending",
    )
    error_message = Column(Text, nullable=True)

    # Cached flag
    cached = Column(String(10), nullable=False, default="false")

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    borrower = relationship("Borrower", backref="credit_inquiries")
    reports = relationship("CreditReport", back_populates="inquiry", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_credit_inquiry_borrower", "borrower_id"),
        Index("idx_credit_inquiry_app", "loan_application_id"),
        Index("idx_credit_inquiry_status", "status"),
        Index("idx_credit_inquiry_created", "created_at"),
    )


class CreditReport(Base):
    __tablename__ = "credit_reports"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    inquiry_id = Column(UUID(as_uuid=True), ForeignKey("credit_inquiries.id", ondelete="CASCADE"), nullable=False)

    # Bureau info
    bureau = Column(String(50), nullable=False)

    # Scores
    score = Column(Numeric(5, 0), nullable=True)
    score_min = Column(Numeric(5, 0), nullable=True)
    score_max = Column(Numeric(5, 0), nullable=True)

    # Metrics
    utilization_percent = Column(Numeric(5, 2), nullable=True)
    delinquency_count = Column(Numeric(5, 0), nullable=True)
    credit_history_months = Column(Numeric(5, 0), nullable=True)
    trade_count = Column(Numeric(5, 0), nullable=True)
    inquiry_count_6m = Column(Numeric(5, 0), nullable=True)
    inquiry_count_12m = Column(Numeric(5, 0), nullable=True)

    # Flags
    has_bankruptcy = Column(String(10), nullable=False, default="false")
    has_collections = Column(String(10), nullable=False, default="false")

    # Raw response (for audit/debug)
    raw_response = Column(JSON, nullable=True)

    # Timestamps
    fetched_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    inquiry = relationship("CreditInquiry", back_populates="reports")

    __table_args__ = (
        Index("idx_credit_report_inquiry", "inquiry_id"),
        Index("idx_credit_report_bureau", "bureau"),
        Index("idx_credit_report_score", "score"),
    )