"""Notification processor cursor (WS2).

A single-row low-water mark recording the highest ``platform_events.id`` the
notification processor has fully scanned. The processor fetches events with
``id > last_event_id``, so the cursor bounds the scan over the append-only (and
unbounded) events table instead of re-reading history every run.

Correctness does NOT depend on the cursor — the processor also dedupes each
candidate against existing ``notification_sent`` events, so a reset/missing
cursor at worst causes a one-time rescan, never a double-send. The cursor is
purely a performance bound. ``name`` is unique so multiple logical processors
(e.g. a future high-frequency transactional lane vs. a nightly dunning lane)
can each keep their own position.
"""
from sqlalchemy import BigInteger, Column, DateTime, String, func

from app.db.base import Base


class PlatformNotificationCursor(Base):
    __tablename__ = "platform_notification_cursor"

    name = Column(String, primary_key=True)
    last_event_id = Column(BigInteger, nullable=False, default=0)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<PlatformNotificationCursor name={self.name!r} last_event_id={self.last_event_id}>"
