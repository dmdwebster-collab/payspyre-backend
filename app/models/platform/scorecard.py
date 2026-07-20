"""Editable 5-band verified-data scorecards (WS-D, Dave mandate #3).

``platform_scorecards`` stores the editable definition: additive bin-based
scoring attributes (per-bin points, natural min/max — NO clamping) and 5 bands
(Excellent/Good/Average/Weak/Poor-Fail), each band carrying a decision outcome
plus a per-band credit limit and rate. The JSONB payloads are validated by
``app.services.scorecards.ScorecardDefinition`` on every write.

``platform_vendor_scorecards`` assigns a scorecard per vendor (override). With
no assignment the platform default (``is_default=true``, at most one — partial
unique index) applies; with NO scorecard configured at all, the flow engine
keeps the legacy 680/600 two-cut exactly, so a fresh install decides exactly as
before this table existed.
"""
from uuid import uuid4

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import relationship

from app.db.base import Base


class PlatformScorecard(Base):
    __tablename__ = "platform_scorecards"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String, nullable=False, unique=True)
    description = Column(Text, nullable=True)
    status = Column(
        ENUM("draft", "active", "archived", name="platform_scorecard_status", create_type=False),
        nullable=False,
        default="draft",
    )
    is_default = Column(Boolean, nullable=False, default=False)

    # Validated by app.services.scorecards.ScorecardDefinition.
    attributes = Column(JSONB, nullable=False)
    bands = Column(JSONB, nullable=False)

    version = Column(Integer, nullable=False, default=1)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<PlatformScorecard(name={self.name}, status={self.status})>"


class PlatformVendorScorecard(Base):
    __tablename__ = "platform_vendor_scorecards"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    vendor_id = Column(
        UUID(as_uuid=True),
        ForeignKey("vendors.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    scorecard_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_scorecards.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    scorecard = relationship("PlatformScorecard")

    def __repr__(self) -> str:
        return (
            f"<PlatformVendorScorecard(vendor_id={self.vendor_id}, "
            f"scorecard_id={self.scorecard_id})>"
        )
