"""Customer/loan flag directory + assignments (WS-E originations admin).

Turnkey parity, video 01: staff can raise named flags against a CUSTOMER or a
LOAN (e.g. "Do not contact", "Fraud watch", "VIP") with a free-text reason.

* ``PlatformFlagDefinition`` — the admin-managed directory. Definitions are
  soft-deactivated (``is_active=False``), never deleted, mirroring the
  decision-reason directory idiom. ``suppress_notifications=True`` definitions
  are honored by the notification processor: any vendor (email/SMS) send to a
  flagged customer / about a flagged loan is SKIPPED but still audited as a
  ``notification_skipped`` platform_event ("suppress sends, still log").
* ``PlatformFlagAssignment`` — one raised flag on one subject (patient XOR/OR
  loan; the DB CHECK requires at least one). Clearing stamps ``cleared_at`` /
  ``cleared_by`` — the row stays as file history. Partial unique indexes
  (migration 059) stop the same flag being active twice on one subject.

All raises/clears are additionally audited to ``platform_events``.
"""
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.base import Base

# Valid subjects a definition may apply to.
FLAG_APPLIES_TO = ("customer", "loan", "both")


class PlatformFlagDefinition(Base):
    __tablename__ = "platform_flag_definitions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    code = Column(String, nullable=False, unique=True)
    label = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    applies_to = Column(String, nullable=False, default="both")  # customer|loan|both
    suppress_notifications = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    assignments = relationship("PlatformFlagAssignment", back_populates="flag")

    def __repr__(self) -> str:
        return f"<PlatformFlagDefinition(code={self.code!r}, active={self.is_active})>"


class PlatformFlagAssignment(Base):
    __tablename__ = "platform_flag_assignments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    flag_id = Column(
        UUID(as_uuid=True), ForeignKey("platform_flag_definitions.id"), nullable=False, index=True
    )
    patient_id = Column(
        UUID(as_uuid=True), ForeignKey("platform_patients.id"), nullable=True, index=True
    )
    loan_id = Column(UUID(as_uuid=True), ForeignKey("platform_loans.id"), nullable=True, index=True)
    reason = Column(Text, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    cleared_at = Column(DateTime(timezone=True), nullable=True)
    cleared_by = Column(String, nullable=True)
    cleared_reason = Column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "(patient_id IS NOT NULL) OR (loan_id IS NOT NULL)",
            name="ck_platform_flag_assignments_subject",
        ),
    )

    flag = relationship("PlatformFlagDefinition", back_populates="assignments")

    def __repr__(self) -> str:
        subject = f"patient={self.patient_id}" if self.patient_id else f"loan={self.loan_id}"
        return f"<PlatformFlagAssignment(flag_id={self.flag_id}, {subject}, cleared={self.cleared_at is not None})>"
