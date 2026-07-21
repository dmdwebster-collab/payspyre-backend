"""Equifax month-end reporting batches (WS-I, migration 063) — TEST MODE ONLY.

One row per generated month-end batch: the Metro2-STYLE flat file produced for
all bureau-reportable accounts (Pot-60 and deeper, per the month-end
delinquency snapshots, WS-H). The file content is stored HERE (``content``) —
NOTHING is transmitted to Equifax; the subscriber agreement + the certified
Metro2 layout are pending (Dave item). When the agreement lands, transmission
becomes a follow-up job reading these rows.

Idempotent per month: re-generating a month UPDATES the existing row (the
derivation is deterministic from the month's snapshots), bumping
``generation_count`` so re-runs stay visible.
"""
from uuid import uuid4

from sqlalchemy import BigInteger, Column, Date, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class PlatformBureauBatch(Base):
    __tablename__ = "platform_bureau_batches"
    __table_args__ = (
        UniqueConstraint("batch_month", name="uq_platform_bureau_batch_month"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    # First day of the reported month (matches delinquency snapshot_month).
    batch_month = Column(Date, nullable=False)

    # 'test' until the Equifax agreement lands — pinned by the service; there
    # is deliberately no 'transmitted' state yet.
    mode = Column(String, nullable=False, default="test", server_default="test")

    file_name = Column(String, nullable=False)
    # The generated Metro2-style flat file (fixed-width text). Stored in the DB
    # row — the batch IS the record; object storage is optional later.
    content = Column(Text, nullable=False)

    account_count = Column(Integer, nullable=False, default=0)
    total_balance_cents = Column(BigInteger, nullable=False, default=0)

    generation_count = Column(Integer, nullable=False, default=1, server_default="1")
    generated_by = Column(String, nullable=False)
    generated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<PlatformBureauBatch(month={self.batch_month}, "
            f"accounts={self.account_count}, mode={self.mode})>"
        )
