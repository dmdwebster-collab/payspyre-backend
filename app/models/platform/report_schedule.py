"""WS-H reports-depth persistence (migration 062):

* ``platform_report_definitions``   — Excel report builder v1: a saved report
  = base dataset (one of the report_exports datasets) + a column selection +
  filters, rendered on demand through the same XLSX machinery.
* ``platform_report_schedules``     — scheduled-reports engine: report (stock
  key OR saved definition), cadence, recipients; the cron-callable runner
  renders each due schedule and emails the XLSX (inert without SendGrid).
* ``platform_report_schedule_runs`` — append-only run log per schedule
  (period idempotency + ops visibility).

No PII lives here: recipients are staff/vendor operational email addresses,
params/filters hold ids and dates only, and rendered artifacts are never
stored at rest — they are regenerated per send.
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base


class PlatformReportDefinition(Base):
    __tablename__ = "platform_report_definitions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String, nullable=False)

    # One of report_exports.REPORT_KEYS — the dataset whose columns/rows feed
    # the custom report.
    base_dataset = Column(String, nullable=False)
    # Ordered list of column headers (a validated subset of the dataset's
    # columns).
    columns = Column(JSONB, nullable=False)
    # {"vendor_ids": [...], "date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD"}
    # — all optional.
    filters = Column(JSONB, nullable=False, default=dict, server_default="{}")

    active = Column(Boolean, nullable=False, default=True, server_default="true")
    created_by = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<PlatformReportDefinition(id={self.id}, name={self.name!r})>"


class PlatformReportSchedule(Base):
    __tablename__ = "platform_report_schedules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String, nullable=False)

    # Exactly one of report_key (stock report_exports dataset) / definition_id
    # (saved builder report) is set — enforced at the API layer.
    report_key = Column(String, nullable=True)
    definition_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_report_definitions.id", ondelete="SET NULL"),
        nullable=True,
    )

    # {"vendor_ids": [...]} — static scope applied to every run. The date
    # window always comes from the cadence (previous completed period).
    params = Column(JSONB, nullable=False, default=dict, server_default="{}")

    cadence = Column(String, nullable=False)  # daily | weekly | monthly
    # Static recipient emails. Ignored when per_vendor is true.
    recipients = Column(JSONB, nullable=False, default=list, server_default="[]")
    # Fan out one vendor-scoped copy per vendor with loans on the book,
    # emailed to that vendor's clinic users (fallback: vendor contact email) —
    # the "monthly vendor statement" case.
    per_vendor = Column(Boolean, nullable=False, default=False, server_default="false")

    active = Column(Boolean, nullable=False, default=True, server_default="true")
    # Idempotency cursor: the most recent period_key successfully processed.
    last_period_key = Column(String, nullable=True)

    created_by = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_platform_report_schedules_active", "active"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<PlatformReportSchedule(id={self.id}, name={self.name!r}, "
            f"cadence={self.cadence!r}, active={self.active})>"
        )


class PlatformReportScheduleRun(Base):
    __tablename__ = "platform_report_schedule_runs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    schedule_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_report_schedules.id", ondelete="CASCADE"),
        nullable=False,
    )
    period_key = Column(String, nullable=False)
    # sent      — rendered + emailed
    # rendered  — rendered; delivery skipped (no email creds / no capable sender)
    # skipped   — nothing to send (e.g. no recipients resolved)
    # failed    — exception; last_period_key not advanced, retried next run
    status = Column(String, nullable=False)
    detail = Column(Text, nullable=True)
    report_count = Column(Integer, nullable=False, default=0, server_default="0")
    recipient_count = Column(Integer, nullable=False, default=0, server_default="0")
    ran_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index(
            "ix_platform_report_schedule_runs_schedule_period",
            "schedule_id",
            "period_key",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<PlatformReportScheduleRun(schedule_id={self.schedule_id}, "
            f"period={self.period_key!r}, status={self.status!r})>"
        )
