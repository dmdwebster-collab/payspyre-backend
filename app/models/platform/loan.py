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
    text,
)
from sqlalchemy.dialects.postgresql import UUID, ENUM
from sqlalchemy.orm import relationship

from app.db.base import Base


class PlatformLoan(Base):
    """A funded loan booked from an approved credit application (LMS spine).

    Money is stored in integer cents. ``annual_rate_bps`` is the annual interest
    rate in basis points (1% = 100 bps). The amortization schedule is derived
    from (principal, rate, term) and stored as PlatformLoanScheduleItem rows.
    """

    __tablename__ = "platform_loans"
    # One loan per application (enforced in the DB by migration 032). Makes
    # book_loan's idempotency race-safe — duplicate booking → IntegrityError.
    # (application_id is NULL only for migrated loans, source='turnkey_migration';
    # Postgres treats NULLs as distinct so the unique constraint still allows many.)
    __table_args__ = (
        UniqueConstraint("application_id", name="uq_platform_loans_application"),
        # A normally-originated loan MUST have an application; only a migrated loan
        # may have a NULL application_id (migration 035).
        CheckConstraint(
            "source = 'turnkey_migration' OR application_id IS NOT NULL",
            name="ck_platform_loans_application_or_migration",
        ),
        # Idempotent re-import: a legacy account maps to at most one loan.
        Index(
            "uq_platform_loans_legacy_acct",
            "legacy_account_number",
            unique=True,
            postgresql_where=text("legacy_account_number IS NOT NULL"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    # NULL only for loans migrated from a legacy LMS (no PaySpyre application exists).
    application_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_credit_applications.id"),
        nullable=True,
    )

    # Provenance: 'application' (default — originated through the PaySpyre flow) or
    # 'turnkey_migration' (imported from the legacy Turnkey book).
    source = Column(String, nullable=False, server_default="application")
    # The legacy Turnkey account number, for tracing + idempotent re-import. NULL for
    # natively-originated loans; uniquely indexed when present (migration 035).
    legacy_account_number = Column(String, nullable=True)

    principal_cents = Column(BigInteger, nullable=False)
    annual_rate_bps = Column(Integer, nullable=False)
    term_months = Column(Integer, nullable=False)

    status = Column(
        ENUM(
            "pending_disbursement",
            "active",
            "paid_off",
            "delinquent",
            "charged_off",
            "cancelled",
            name="platform_loan_status",
            create_type=False,
        ),
        nullable=False,
        default="pending_disbursement",
    )

    disbursed_at = Column(DateTime(timezone=True), nullable=True)

    # ---- Lifecycle: e-signature (SignNow) --------------------------------
    # Where the loan-agreement signature is at. Advances forward only:
    # not_sent -> sent -> signed (or -> declined).
    agreement_status = Column(
        ENUM(
            "not_sent",
            "sent",
            "signed",
            "declined",
            name="platform_loan_agreement_status",
            create_type=False,
        ),
        nullable=False,
        default="not_sent",
    )
    # SignNow document id — durable vendor ref. NULL until an agreement is sent.
    agreement_ref = Column(String, nullable=True)

    # ---- Lifecycle: disbursement (Zumrails) ------------------------------
    # Where the funding payout is at. Advances forward only:
    # not_started -> in_progress -> completed (or -> failed).
    disbursement_status = Column(
        ENUM(
            "not_started",
            "in_progress",
            "completed",
            "failed",
            name="platform_loan_disbursement_status",
            create_type=False,
        ),
        nullable=False,
        default="not_started",
    )
    # Zumrails transaction id — durable vendor ref. NULL until disbursement starts.
    disbursement_ref = Column(String, nullable=True)

    # Outstanding principal — initialized to principal_cents, reduced by payments.
    principal_balance_cents = Column(BigInteger, nullable=False)

    currency = Column(String, nullable=False, default="CAD")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Relationships
    schedule = relationship(
        "PlatformLoanScheduleItem",
        back_populates="loan",
        cascade="all, delete-orphan",
        order_by="PlatformLoanScheduleItem.installment_number",
    )
    payments = relationship(
        "PlatformLoanPayment",
        back_populates="loan",
        cascade="all, delete-orphan",
        order_by="PlatformLoanPayment.received_at",
    )
    statements = relationship(
        "PlatformLoanStatement",
        back_populates="loan",
        cascade="all, delete-orphan",
        order_by="PlatformLoanStatement.period_start",
    )

    def __repr__(self) -> str:
        return (
            f"<PlatformLoan(id={self.id}, status={self.status}, "
            f"principal_cents={self.principal_cents}, "
            f"balance_cents={self.principal_balance_cents})>"
        )


class PlatformLoanScheduleItem(Base):
    """One installment row of a loan's amortization schedule."""

    __tablename__ = "platform_loan_schedule"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    loan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_loans.id", ondelete="CASCADE"),
        nullable=False,
    )

    installment_number = Column(Integer, nullable=False)
    due_date = Column(Date, nullable=False)

    principal_cents = Column(BigInteger, nullable=False)
    interest_cents = Column(BigInteger, nullable=False)
    total_cents = Column(BigInteger, nullable=False)

    status = Column(
        ENUM(
            "scheduled",
            "paid",
            "partial",
            "late",
            "waived",
            name="platform_loan_schedule_status",
            create_type=False,
        ),
        nullable=False,
        default="scheduled",
    )
    paid_cents = Column(BigInteger, nullable=False, default=0)

    loan = relationship("PlatformLoan", back_populates="schedule")

    def __repr__(self) -> str:
        return (
            f"<PlatformLoanScheduleItem(loan_id={self.loan_id}, "
            f"n={self.installment_number}, total_cents={self.total_cents}, "
            f"status={self.status})>"
        )


class PlatformLoanPayment(Base):
    """A payment received against a loan (e.g. a Zumrails collection)."""

    __tablename__ = "platform_loan_payments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    loan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_loans.id", ondelete="CASCADE"),
        nullable=False,
    )

    amount_cents = Column(BigInteger, nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=False)
    method = Column(String, nullable=False)
    # External rail reference, e.g. a Zumrails transaction id. Nullable for
    # manual / adjustment entries.
    external_ref = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    loan = relationship("PlatformLoan", back_populates="payments")

    def __repr__(self) -> str:
        return (
            f"<PlatformLoanPayment(loan_id={self.loan_id}, "
            f"amount_cents={self.amount_cents}, method={self.method})>"
        )


class PlatformLoanStatement(Base):
    """A periodic billing statement for a loan.

    One row per billing window. Snapshots the principal balance at the start
    (``opening_balance_cents``) and end (``closing_balance_cents``) of the
    period, plus the principal/interest actually paid within it. Money is
    integer cents throughout, matching the rest of the LMS spine.
    """

    __tablename__ = "platform_loan_statements"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    loan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_loans.id", ondelete="CASCADE"),
        nullable=False,
    )

    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)

    opening_balance_cents = Column(BigInteger, nullable=False)
    principal_paid_cents = Column(BigInteger, nullable=False, default=0)
    interest_paid_cents = Column(BigInteger, nullable=False, default=0)
    closing_balance_cents = Column(BigInteger, nullable=False)

    generated_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    loan = relationship("PlatformLoan", back_populates="statements")

    def __repr__(self) -> str:
        return (
            f"<PlatformLoanStatement(loan_id={self.loan_id}, "
            f"period={self.period_start}..{self.period_end}, "
            f"closing_balance_cents={self.closing_balance_cents})>"
        )
