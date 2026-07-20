"""``platform_hardship_requests`` model — Hardship v1 (WS-J, migration 054).

Dave (03__WP_Servicing §5): Turnkey's unused "Rescheduling" tab must become
**Hardship** — installment deferments, due-date changes (and later: frequency
change, refinance-balance-only, temporary Adjustment of Terms with auto
snap-back) — and "we can't unilaterally change those things without the
borrower first signing legal documentation."

A hardship request is the LEGAL PAPER TRAIL of one borrower-consented schedule
change:

  * ``kind`` + ``params`` — what is being changed (deferment installment ids,
    or a due-day / per-item due-date shift).
  * ``preview`` — the exact schedule effect computed at draft time (which items
    suspend/shift, the new dates, the estimated extra interest — interest keeps
    accruing on outstanding principal during a deferment). This is what the
    amendment summary shows the borrower.
  * ``esign_document_ref`` / ``signed_at`` — the SignNow amendment. The
    HARD RULE (pinned by tests): NO schedule mutation before ``signed_at``.
  * ``snap_back`` — the original schedule state captured immediately before
    mutation (prior item statuses/due dates + appended custom-transaction ids).
    v1 completion keeps the applied changes (deferred items stay suspended,
    the equivalent amounts live as end-of-contract custom transactions); the
    full "temporary Adjustment of Terms with automatic snap-back at expiry"
    machine is the P1 follow-up this column scaffolds.

Status flow (forward-only): draft → awaiting_signature → active → completed;
awaiting_signature → declined | expired; draft/awaiting_signature → cancelled.
Money is integer cents everywhere.
"""
from uuid import uuid4

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import relationship

from app.db.base import Base

HARDSHIP_KINDS = ("deferment", "due_date_change")

HARDSHIP_STATUSES = (
    "draft",
    "awaiting_signature",
    "active",
    "declined",
    "cancelled",
    "expired",
    "completed",
)


class PlatformHardshipRequest(Base):
    __tablename__ = "platform_hardship_requests"
    __table_args__ = (
        Index("ix_platform_hardship_requests_loan", "loan_id", "created_at"),
        Index(
            "ix_platform_hardship_requests_esign_ref",
            "esign_document_ref",
            unique=True,
            postgresql_where=text("esign_document_ref IS NOT NULL"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    loan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_loans.id", ondelete="CASCADE"),
        nullable=False,
    )

    kind = Column(
        ENUM(*HARDSHIP_KINDS, name="platform_hardship_kind", create_type=False),
        nullable=False,
    )
    # deferment:       {"installment_ids": [uuid, ...]}
    # due_date_change: {"new_day_of_month": 15} OR
    #                  {"item_shifts": [{"item_id": uuid, "new_due_date": "YYYY-MM-DD"}]}
    params = Column(JSONB, nullable=False)

    status = Column(
        ENUM(*HARDSHIP_STATUSES, name="platform_hardship_status", create_type=False),
        nullable=False,
        default="draft",
    )

    # MANDATORY (Dave): the hardship reason + the staff working comment.
    reason = Column(String, nullable=False)
    comment = Column(String, nullable=False)
    created_by = Column(String, nullable=False)

    # Draft-time schedule-effect preview (items, dates, estimated interest).
    preview = Column(JSONB, nullable=True)

    # E-signature (SignNow amendment document).
    esign_document_ref = Column(String, nullable=True)
    signature_requested_at = Column(DateTime(timezone=True), nullable=True)
    signature_expires_at = Column(DateTime(timezone=True), nullable=True)
    signed_at = Column(DateTime(timezone=True), nullable=True)

    # Application (only ever after signed_at — the money/legal-path rule).
    applied_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    # Original schedule state captured immediately before mutation.
    snap_back = Column(JSONB, nullable=True)

    cancelled_by = Column(String, nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    loan = relationship("PlatformLoan")

    def __repr__(self) -> str:
        return (
            f"<PlatformHardshipRequest(id={self.id}, loan_id={self.loan_id}, "
            f"kind={self.kind}, status={self.status})>"
        )
