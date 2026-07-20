"""Decision-reason directory (migration 048) — admin-editable REJECT / CANCEL reasons.

Turnkey parity (02__WP_Underwriting.md): rejection and cancellation reasons live
in a Settings directory that staff maintain (add / edit / soft-deactivate — never
hard-delete, so a code that was ever used in a decision stays resolvable for
audit). ``borrower_facing_text`` is the wording that flows into the
adverse-action notice (reject) or the cancellation notification (cancel);
``internal_label`` is what staff see in pickers.
"""
from uuid import uuid4

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ENUM, UUID

from app.db.base import Base


class PlatformDecisionReason(Base):
    __tablename__ = "platform_decision_reasons"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    kind = Column(
        ENUM("reject", "cancel", name="platform_decision_reason_kind", create_type=False),
        nullable=False,
    )
    # Stable slug (^[a-z0-9_]+$). Immutable after creation — decisions/audit events
    # reference reasons by code, so the code must never be repointed.
    code = Column(String, nullable=False)
    internal_label = Column(String, nullable=False)
    borrower_facing_text = Column(Text, nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("uq_platform_decision_reasons_kind_code", "kind", "code", unique=True),
    )

    def __repr__(self) -> str:
        return f"<PlatformDecisionReason(kind={self.kind}, code={self.code}, active={self.active})>"
