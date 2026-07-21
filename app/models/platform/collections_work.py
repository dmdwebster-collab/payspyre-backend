"""Collections work-surface models (WS-C full parity, migration 057).

Dave's collections modernization (04__WP_Collections):

* ``PlatformCollectorAssignment`` — who is working a delinquent loan. Bulk
  assignment (by vendor / borrower alpha / bucket) replaces Turnkey's
  one-at-a-time "Assign to me" ("that's just not viable"). ``tier`` carries the
  junior/senior collector split — a junior may only work shallow buckets
  (current-month-late / pot-30); anything deeper needs a senior.
* ``PlatformCollectionActionType`` — the settings-definable directory of
  action-plan types ("those action types need to be in a settings area that we
  can define and add and remove to"). Soft-deactivate only, like decision
  reasons.
* ``PlatformCollectionAction`` — one per-loan action-plan entry (type, due
  date, assignee, mandatory comment, outcome on completion). Full history:
  rows are never deleted; cancel flips status.
* ``PlatformPromiseToPay`` — amount / promised date / mandatory comment /
  optional no-late-fee flag; status lifecycle (open → kept | broken, or
  cancelled) evaluated against ACTUAL payments in the ledger.
* ``PlatformInsolvencyMaintenanceFee`` — idempotency marker for the monthly
  insolvency-portfolio maintenance fee (one per loan per month); the money
  itself is an immutable add-on-bucket ledger ``fee`` row.

Money is integer cents throughout (repo convention). No PII beyond staff ids.
"""
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
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
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import relationship

from app.db.base import Base


class PlatformCollectorAssignment(Base):
    """One collector's assignment to one delinquent loan.

    At most ONE ACTIVE assignment per loan (partial unique index); history is
    kept by deactivating rows (``active=false`` + unassigned_by/at) instead of
    deleting them. ``method`` records HOW the assignment happened (manual /
    bulk_vendor / bulk_alpha / bulk_bucket) for Dave's queue-distribution
    audit.
    """

    __tablename__ = "platform_collector_assignments"
    __table_args__ = (
        Index(
            "uq_platform_collector_assignment_active_loan",
            "loan_id",
            unique=True,
            postgresql_where=text("active"),
        ),
        Index(
            "ix_platform_collector_assignments_collector_active",
            "collector_user_id",
            "active",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    loan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_loans.id", ondelete="CASCADE"),
        nullable=False,
    )
    collector_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Junior collectors work current-month-late / pot-30 only; seniors work
    # everything (hardship powers live in the separate hardship permission).
    tier = Column(
        ENUM("junior", "senior", name="platform_collector_tier", create_type=False),
        nullable=False,
    )
    # How this assignment was made: manual | bulk_vendor | bulk_alpha | bulk_bucket.
    method = Column(String, nullable=False, default="manual")

    active = Column(Boolean, nullable=False, default=True, server_default=text("true"))

    assigned_by = Column(String, nullable=False)
    assigned_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    unassigned_by = Column(String, nullable=True)
    unassigned_at = Column(DateTime(timezone=True), nullable=True)

    loan = relationship("PlatformLoan")

    def __repr__(self) -> str:
        return (
            f"<PlatformCollectorAssignment(loan_id={self.loan_id}, "
            f"collector={self.collector_user_id}, tier={self.tier}, "
            f"active={self.active})>"
        )


class PlatformCollectionActionType(Base):
    """Settings directory of action-plan types (admin CRUD, soft-deactivate).

    Same rules as the decision-reason directory: ``code`` is immutable after
    creation (actions reference the row by FK, but the code is the stable
    audit handle) and rows are never hard-deleted — deactivating hides the
    type from pickers while historical actions stay resolvable.
    """

    __tablename__ = "platform_collection_action_types"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    code = Column(String, nullable=False, unique=True)
    label = Column(String, nullable=False)
    description = Column(String, nullable=True)
    active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    sort_order = Column(Integer, nullable=False, default=0, server_default="0")

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<PlatformCollectionActionType(code={self.code}, "
            f"active={self.active})>"
        )


class PlatformCollectionAction(Base):
    """One action-plan entry on one loan (04__WP_Collections f0026-f0030).

    Senior staff create dated actions (type + mandatory comment + optional
    assignee) → back-end staff execute and record the ``outcome`` on
    completion. Rows are never deleted (full history); cancelling flips
    ``status``.
    """

    __tablename__ = "platform_collection_actions"
    __table_args__ = (
        Index("ix_platform_collection_actions_loan_due", "loan_id", "due_date"),
        Index(
            "ix_platform_collection_actions_assignee_status",
            "assignee_user_id",
            "status",
            "due_date",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    loan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_loans.id", ondelete="CASCADE"),
        nullable=False,
    )
    action_type_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_collection_action_types.id"),
        nullable=False,
    )

    due_date = Column(Date, nullable=False)
    assignee_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    status = Column(
        ENUM(
            "planned",
            "completed",
            "cancelled",
            name="platform_collection_action_status",
            create_type=False,
        ),
        nullable=False,
        default="planned",
    )
    # MANDATORY (Dave): the instruction/context for the action.
    comment = Column(String, nullable=False)
    # What happened, recorded at completion (mandatory then).
    outcome = Column(String, nullable=True)

    created_by = Column(String, nullable=False)
    completed_by = Column(String, nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_by = Column(String, nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    action_type = relationship("PlatformCollectionActionType")
    loan = relationship("PlatformLoan")

    def __repr__(self) -> str:
        return (
            f"<PlatformCollectionAction(loan_id={self.loan_id}, "
            f"due={self.due_date}, status={self.status})>"
        )


class PlatformPromiseToPay(Base):
    """One promise-to-pay on one loan (04__WP_Collections f0039).

    Amount / promised date / MANDATORY comment / optional "do not accrue late
    fees" flag. Status lifecycle:

        open → kept    (ledger payments in [created, promised_date] cover the amount)
             → broken  (promised_date passed without cover)
        open → cancelled (staff withdraw, audited)

    Evaluation reads the ACTUAL payment ledger (``platform_loan_payments``) —
    never a manual tick-box. ``no_late_fees`` is a recorded flag; late-fee
    waiver enforcement plugs into the fee engine when that lands (documented).
    """

    __tablename__ = "platform_promises_to_pay"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_platform_ptp_amount_positive"),
        Index("ix_platform_ptp_loan_status", "loan_id", "status"),
        Index("ix_platform_ptp_status_date", "status", "promised_date"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    loan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_loans.id", ondelete="CASCADE"),
        nullable=False,
    )

    amount_cents = Column(BigInteger, nullable=False)
    promised_date = Column(Date, nullable=False)
    # MANDATORY (Dave / Turnkey dialog parity).
    comment = Column(String, nullable=False)
    no_late_fees = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    status = Column(
        ENUM(
            "open",
            "kept",
            "broken",
            "cancelled",
            name="platform_ptp_status",
            create_type=False,
        ),
        nullable=False,
        default="open",
    )
    # When the status was last derived from the ledger (kept/broken only).
    evaluated_at = Column(DateTime(timezone=True), nullable=True)
    # How much of the promise the qualifying payments covered at evaluation.
    covered_cents = Column(BigInteger, nullable=True)

    created_by = Column(String, nullable=False)
    cancelled_by = Column(String, nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    loan = relationship("PlatformLoan")

    def __repr__(self) -> str:
        return (
            f"<PlatformPromiseToPay(loan_id={self.loan_id}, "
            f"amount_cents={self.amount_cents}, date={self.promised_date}, "
            f"status={self.status})>"
        )


class PlatformInsolvencyMaintenanceFee(Base):
    """Idempotency marker for the monthly insolvency maintenance fee.

    Dave: "we would need to come up with a maintenance fee to follow up and
    maintain bankrupt accounts". One row per (loan, month) — UNIQUE — so the
    monthly hook can never double-charge; ``transaction_id`` links the
    immutable add-on-bucket ledger ``fee`` row that carries the money.
    """

    __tablename__ = "platform_insolvency_maintenance_fees"
    __table_args__ = (
        UniqueConstraint(
            "loan_id", "fee_month", name="uq_platform_insolvency_fee_loan_month"
        ),
        CheckConstraint(
            "amount_cents > 0", name="ck_platform_insolvency_fee_amount_positive"
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    loan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_loans.id", ondelete="CASCADE"),
        nullable=False,
    )
    # First day of the month the fee covers.
    fee_month = Column(Date, nullable=False)
    amount_cents = Column(BigInteger, nullable=False)
    transaction_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_loan_transactions.id"),
        nullable=False,
    )

    created_by = Column(String, nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<PlatformInsolvencyMaintenanceFee(loan_id={self.loan_id}, "
            f"month={self.fee_month}, amount_cents={self.amount_cents})>"
        )
