"""Vendor disbursement ledger (W2-DISB, Turnkey parity video 10).

One IMMUTABLE-ish row per payout of collected funds from PaySpyre to a vendor
(clinic). The vendor "wallet" (MTD collected / due / available) is NOT stored —
it is DERIVED on read from the money ledger (``platform_loan_transactions``)
minus this table's settled + in-flight payouts and the clearing holdback (see
``app.services.vendor_disbursements``). This table records only the money-OUT
events.

Lifecycle mirrors the auto-collection attempt spine:
``pending`` (claimed / in flight) → ``completed`` | ``failed`` | ``cancelled``
via the Zumrails webhook (or a synchronous terminal ack from the simulator).

IDEMPOTENCY (MONEY-OUT double-pay safety):
* ``client_transaction_id`` is UNIQUE and deterministic for the free monthly
  auto-payout — ``vdisb-auto-{vendor_id}-{YYYYMM}`` — so a duplicate cron run or
  crash-restart can never issue two monthly payouts for the same vendor+month.
  Extra (on-demand) payouts use ``vdisb-extra-{uuid}``.
* The row is CLAIMED (committed) BEFORE the Zumrails push, exactly like
  ``platform_collection_attempts`` — a crash between claim and push leaves a
  ``pending`` row (staff-resolvable) rather than risking a double push.

``kind``    — ``auto_monthly`` (the free monthly sweep) | ``extra`` (on-demand,
              fee-charged).
``amount_cents`` — NET amount pushed to the vendor.
``fee_cents``    — PaySpyre's per-extra-payout fee retained (0 for auto_monthly).
The wallet draws ``amount_cents + fee_cents`` from the vendor's available
balance. Money is integer cents throughout.
"""
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class PlatformVendorDisbursement(Base):
    __tablename__ = "platform_vendor_disbursements"
    __table_args__ = (
        UniqueConstraint(
            "client_transaction_id",
            name="uq_platform_vendor_disbursement_client_txn",
        ),
        CheckConstraint(
            "amount_cents > 0", name="ck_platform_vendor_disbursement_amount_pos"
        ),
        CheckConstraint(
            "fee_cents >= 0", name="ck_platform_vendor_disbursement_fee_nonneg"
        ),
        CheckConstraint(
            "kind IN ('auto_monthly', 'extra')",
            name="ck_platform_vendor_disbursement_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed', 'cancelled')",
            name="ck_platform_vendor_disbursement_status",
        ),
        Index(
            "ix_platform_vendor_disbursement_vendor_status",
            "vendor_id",
            "status",
        ),
        Index(
            "ix_platform_vendor_disbursement_external_ref",
            "external_ref",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    vendor_id = Column(
        UUID(as_uuid=True),
        ForeignKey("vendors.id", ondelete="CASCADE"),
        nullable=False,
    )

    kind = Column(String, nullable=False)
    status = Column(String, nullable=False, server_default="pending")

    # NET amount pushed to the vendor + PaySpyre's retained extra-payout fee.
    amount_cents = Column(BigInteger, nullable=False)
    fee_cents = Column(BigInteger, nullable=False, default=0, server_default="0")

    # The cleared-through date this payout was computed against (as_of minus the
    # business-day holdback) — kept for audit / reconciliation.
    holdback_cutoff = Column(Date, nullable=False)

    # Monthly auto-payout accounting period (NULL for extra payouts). Used only
    # for reporting; the hard idempotency spine is client_transaction_id.
    period_year = Column(Integer, nullable=True)
    period_month = Column(Integer, nullable=True)

    # Idempotency handle handed to the payment rail; deterministic for the
    # monthly sweep (see module docstring).
    client_transaction_id = Column(String, nullable=False)
    # Zumrails' own transaction id, set once the push is accepted.
    external_ref = Column(String, nullable=True)
    return_code = Column(String, nullable=True)
    error = Column(String, nullable=True)

    # 'system:vendor_disbursement' for the auto sweep, else the clinic user id.
    requested_by = Column(String, nullable=False)

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<PlatformVendorDisbursement(vendor_id={self.vendor_id}, "
            f"kind={self.kind}, status={self.status}, "
            f"amount_cents={self.amount_cents}, fee_cents={self.fee_cents})>"
        )
