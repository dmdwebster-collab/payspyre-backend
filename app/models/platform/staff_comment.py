"""Internal staff comments — Dave's **Comments** tab on the application and
loan surfaces.

    *"This is the communications tab to record and discuss the loan account.
    These communications are internal only."*

STAFF-ONLY, AND DELIBERATELY A SEPARATE SURFACE FROM THE TWO IT RESEMBLES:

* ``platform_communication_logs`` records messages actually SENT to a borrower
  (email/SMS bodies, delivery state). Those are borrower-facing artifacts and
  are discoverable as such. A staff comment is never sent to anyone.
* ``platform_application_messages`` is the vendor⇄PaySpyre chat — a clinic
  staffer is a participant. A staff comment is invisible to vendors.

Conflating any of the three would leak an internal underwriting note onto a
surface the borrower or the clinic can read, so they stay three tables and
three APIs. This one has no clinic route and no borrower route at all.

APPEND-ONLY BY CONSTRUCTION
---------------------------
There is no ``updated_at`` and no ``deleted_at``: a row, once written, is the
permanent record of what an operator said about a credit file at a point in
time. That is the property that makes the tab usable as evidence in a lending
dispute or a regulator/consumer-complaint file review — an editable note has no
evidentiary value because it cannot be distinguished from one rewritten after
the fact. Corrections are made by posting a follow-up comment, exactly as an
append-only ledger corrects an entry with a reversing one.

SUBJECT
-------
Exactly one of ``application_id`` / ``loan_id`` is set (DB check constraint), so
the two tabs are distinct threads. The loan tab can additionally REPLAY the
originating application's thread (``?include_application=true``) — reading
across is fine; writing across is not.
"""
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base

#: Max body length, enforced at the schema layer (mirrors messaging).
COMMENT_MAX_LEN = 10_000


class PlatformStaffComment(Base):
    """One internal note by a PaySpyre operator about an application or loan."""

    __tablename__ = "platform_staff_comments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    application_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_credit_applications.id", ondelete="CASCADE"),
        nullable=True,
    )
    loan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_loans.id", ondelete="CASCADE"),
        nullable=True,
    )
    author_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    body = Column(Text, nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "(application_id IS NOT NULL) <> (loan_id IS NOT NULL)",
            name="ck_platform_staff_comments_one_subject",
        ),
        Index(
            "ix_platform_staff_comments_application",
            "application_id",
            "created_at",
        ),
        Index("ix_platform_staff_comments_loan", "loan_id", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        subject = (
            f"application={self.application_id}"
            if self.application_id
            else f"loan={self.loan_id}"
        )
        return f"<PlatformStaffComment(id={self.id}, {subject})>"
