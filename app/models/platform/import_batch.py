from uuid import uuid4

from sqlalchemy import Column, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID

from app.db.base import Base


class PlatformImportBatch(Base):
    """One uploaded cutover-import file (Turnkey -> PaySpyre migration, WS-D).

    Lifecycle: ``uploaded`` -> ``validated`` (preview report built, NOTHING
    written to domain tables) -> ``confirmed`` (an admin explicitly applied it)
    -> ``applied`` (rows written idempotently; per-row failures recorded in
    ``apply_report`` with ``error_count`` bumped). ``failed`` marks a batch
    whose file could not be parsed at all or whose apply blew up wholesale;
    ``cancelled`` is an admin discard.

    PII: ``rows`` holds the normalized row payloads (names/emails/DOB for the
    customers entity) so the confirm step applies exactly what was previewed.
    That is DB-resident PII on an admin-only surface — same trust domain as
    ``platform_patients`` itself. ``preview`` and ``apply_report`` reference
    rows by NUMBER + field NAME only, never PII values, so they are safe to
    return over the API and to log.
    """

    __tablename__ = "platform_import_batches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    entity_type = Column(
        ENUM(
            "customers",
            "loans",
            "payments",
            "disbursements",
            name="platform_import_entity_type",
            create_type=False,
        ),
        nullable=False,
    )
    filename = Column(String, nullable=False)
    status = Column(
        ENUM(
            "uploaded",
            "validated",
            "confirmed",
            "applied",
            "failed",
            "cancelled",
            name="platform_import_batch_status",
            create_type=False,
        ),
        nullable=False,
        default="uploaded",
    )

    row_count = Column(Integer, nullable=False, default=0)
    error_count = Column(Integer, nullable=False, default=0)

    preview = Column(JSONB, nullable=True)       # validation report (PII-safe)
    rows = Column(JSONB, nullable=True)          # normalized row payloads (PII)
    apply_report = Column(JSONB, nullable=True)  # confirm-step outcome (PII-safe)

    created_by = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    applied_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        # No PII in repr — it lands in tracebacks/logs.
        return (
            f"<PlatformImportBatch(id={self.id}, entity_type={self.entity_type}, "
            f"status={self.status}, rows={self.row_count}, errors={self.error_count})>"
        )
