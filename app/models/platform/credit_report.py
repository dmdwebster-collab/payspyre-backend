"""``platform_credit_report_pulls`` — stored credit-bureau pulls (migration 069).

Dave's Originations "Credit Report" tab: *"this tab is where the results of a
credit bureau pull is stored, along with 2 buttons: Hard Pull and Soft Pull."*

The honesty column
------------------
``simulated`` is persisted, not inferred. The Equifax subscriber agreement is
NOT signed, so ``VerificationDispatcher`` routes ``bureau_soft``/``bureau_hard``
through the MOCK adapter unconditionally (see its module docstring). A stored
mock report is indistinguishable from a real one by shape alone, so the flag
rides on the row forever and every API that returns a pull echoes it. The UI
labels it; nobody makes a lending decision believing a synthetic score is a
bureau file.

``report`` holds the adapter's structured output (score band, fraud signals,
vendor). It carries NO SIN, DOB, or address — the mock does not produce them
and the real adapter is explicitly documented as PII-light in its result.
"""
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base

#: Which party a pull was run against (co-borrower files are separate
#: applications, but the pull is recorded against the file it was fired from).
PULL_SUBJECTS = ("borrower", "co_borrower")
PULL_TYPES = ("soft", "hard")


class PlatformCreditReportPull(Base):
    __tablename__ = "platform_credit_report_pulls"
    __table_args__ = (
        CheckConstraint(
            "pull_type IN ('soft', 'hard')", name="ck_platform_credit_report_pull_type"
        ),
        CheckConstraint(
            "subject IN ('borrower', 'co_borrower')",
            name="ck_platform_credit_report_pull_subject",
        ),
        Index("ix_platform_credit_report_pulls_application", "application_id"),
        Index("ix_platform_credit_report_pulls_patient", "patient_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    application_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_credit_applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    patient_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_patients.id", ondelete="CASCADE"),
        nullable=False,
    )

    subject = Column(String, nullable=False, default="borrower", server_default="borrower")
    pull_type = Column(String, nullable=False)
    bureau = Column(String, nullable=False)

    score = Column(Integer, nullable=True)
    result = Column(String, nullable=False)
    bankruptcy = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    report = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))

    #: TRUE whenever the report did not come from a live bureau connection.
    simulated = Column(Boolean, nullable=False, default=True, server_default=text("true"))

    requested_by = Column(String, nullable=False)
    verification_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_verifications.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<PlatformCreditReportPull(application_id={self.application_id}, "
            f"type={self.pull_type}, bureau={self.bureau}, "
            f"simulated={self.simulated})>"
        )
