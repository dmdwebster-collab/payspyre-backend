"""``platform_business_calendar_overrides`` — admin edits to the business calendar.

Canadian statutory holidays are COMPUTED per year + province by
:mod:`app.services.business_calendar` (never stored); this table stores only the
admin's overrides:

* ``kind='closure'``      — an extra non-business date (office closure, ad-hoc
  bank holiday) the computation doesn't know about.
* ``kind='business_day'`` — force a computed holiday to count as a business day
  (e.g. a provincial holiday the payment rails actually process on).

``province`` is a two-letter code, or ``'ALL'`` for a nationwide override.
"""
from uuid import uuid4

from sqlalchemy import CheckConstraint, Column, Date, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class PlatformBusinessCalendarOverride(Base):
    __tablename__ = "platform_business_calendar_overrides"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    date = Column(Date, nullable=False)
    # Two-letter province/territory code, or 'ALL' (nationwide).
    province = Column(String(3), nullable=False, default="ALL")
    kind = Column(String(20), nullable=False)  # 'closure' | 'business_day'
    label = Column(String(200), nullable=True)

    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('closure', 'business_day')",
            name="ck_business_calendar_override_kind",
        ),
        Index(
            "uq_business_calendar_override",
            "date", "province", unique=True,
        ),
        Index("idx_business_calendar_override_date", "date"),
    )

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"<PlatformBusinessCalendarOverride(date={self.date}, "
            f"province={self.province!r}, kind={self.kind!r})>"
        )
