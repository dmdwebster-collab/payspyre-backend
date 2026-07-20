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

    # The borrower this loan belongs to. Natively-originated loans reach their
    # borrower via application_id -> patient; MIGRATED loans (application_id NULL)
    # need this direct link, populated by the cutover import from the loans CSV's
    # customer legacy id (migration 047). Nullable — pre-047 rows and loans whose
    # customer wasn't imported have no value.
    patient_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_patients.id"),
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
    # The immutable money ledger (WS-A). Ordered exactly as the actuals engine
    # replays it: effective date first, then per-loan sequence for same-day rows.
    transactions = relationship(
        "PlatformLoanTransaction",
        back_populates="loan",
        cascade="all, delete-orphan",
        order_by="PlatformLoanTransaction.effective_date, PlatformLoanTransaction.seq",
    )
    # Staff-added custom scheduled transactions (WS-F schedule surgery) —
    # one-off future payment instructions layered on top of the plan.
    custom_transactions = relationship(
        "PlatformLoanCustomTransaction",
        back_populates="loan",
        cascade="all, delete-orphan",
        order_by="PlatformLoanCustomTransaction.scheduled_date",
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

    # ``suspended`` (WS-F schedule surgery, migration 050): a staff-parked
    # installment. The delinquency-aging and dunning jobs SKIP it (that is the
    # point of suspending), and it never drags the loan into ``delinquent``.
    # The money is still owed — unsuspending restores ``partial``/``scheduled``
    # (derived from paid_cents) and aging re-derives ``late`` if overdue.
    status = Column(
        ENUM(
            "scheduled",
            "paid",
            "partial",
            "late",
            "waived",
            "suspended",
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


class PlatformLoanTransaction(Base):
    """One IMMUTABLE row of the loan money ledger (Dave's "ledger, not
    transactions" mandate — WS-A, migration 049).

    Rows are never updated or deleted (DB WORM trigger enforces it) —
    corrections are compensating ``reversal`` rows referencing the original.

    * ``reference`` — Dave's auto-generated ``{vendor_id}-{loan_id}-{seq}``
      (each component independently filterable; ``seq`` is per-loan, 1-based).
    * ``effective_date`` vs ``processing_date`` — the dual-date mandate: the
      date money is TREATED as applied vs the date it was recorded. Equal by
      default; permission-bounded backdating is a later workstream.
    * Allocation columns split ``amount_cents`` across the ledger's category
      buckets (principal / interest / fees / non-accruing add-on).
    * Money is integer cents; no PII beyond the acting staff id.
    """

    __tablename__ = "platform_loan_transactions"
    __table_args__ = (
        UniqueConstraint("loan_id", "seq", name="uq_platform_loan_txn_loan_seq"),
        CheckConstraint("amount_cents >= 0", name="ck_platform_loan_txn_amount_nonneg"),
        # A reversal must point at the row it reverses.
        CheckConstraint(
            "txn_type != 'reversal' OR reverses_transaction_id IS NOT NULL",
            name="ck_platform_loan_txn_reversal_ref",
        ),
        Index("ix_platform_loan_txn_loan_effective", "loan_id", "effective_date"),
        Index("ix_platform_loan_txn_reference", "reference"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    loan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_loans.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Per-loan monotonically increasing sequence (1-based); unique per loan.
    seq = Column(Integer, nullable=False)
    # `{vendor_id}-{loan_id}-{seq}` ("none" when the loan has no vendor).
    reference = Column(String, nullable=False)

    txn_type = Column(
        ENUM(
            "payment",
            "disbursement",
            "fee",
            "adjustment",
            "reversal",
            name="platform_loan_txn_type",
            create_type=False,
        ),
        nullable=False,
    )
    # Reconciliation dimension (Dave: payment-mix metrics). NULL for non-cash rows.
    payment_type = Column(
        ENUM(
            "cash",
            "check",
            "eft",
            "credit_card",
            "adjustment",
            name="platform_loan_payment_type",
            create_type=False,
        ),
        nullable=True,
    )
    # Repayment modes are wired in a later workstream; the column exists now so
    # the ledger never needs a money-table migration to add them.
    repayment_mode = Column(
        ENUM(
            "regular",
            "add_on",
            "special",
            "payoff",
            name="platform_loan_repayment_mode",
            create_type=False,
        ),
        nullable=True,
    )

    amount_cents = Column(BigInteger, nullable=False)
    # Allocation buckets — how amount_cents splits across the ledger categories.
    principal_cents = Column(BigInteger, nullable=False, default=0, server_default="0")
    interest_cents = Column(BigInteger, nullable=False, default=0, server_default="0")
    fees_cents = Column(BigInteger, nullable=False, default=0, server_default="0")
    add_on_cents = Column(BigInteger, nullable=False, default=0, server_default="0")

    effective_date = Column(Date, nullable=False)
    processing_date = Column(Date, nullable=False)

    # The row this reversal compensates (NULL for non-reversal rows).
    reverses_transaction_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_loan_transactions.id"),
        nullable=True,
    )

    created_by = Column(String, nullable=False)
    comment = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    loan = relationship("PlatformLoan", back_populates="transactions")

    def __repr__(self) -> str:
        return (
            f"<PlatformLoanTransaction(reference={self.reference}, "
            f"txn_type={self.txn_type}, amount_cents={self.amount_cents})>"
        )


class PlatformLoanCustomTransaction(Base):
    """A staff-added CUSTOM scheduled transaction (WS-F schedule surgery,
    migration 050) — Turnkey's "Add transaction" on the Scheduled-transactions
    tab (03__WP_Servicing f0077, Dave: "a very, very important section").

    A custom transaction is a one-off FUTURE payment instruction (date, amount,
    repayment mode) layered ON TOP of the amortization plan — the plan itself
    is never altered (Dave's borrower-protection rule). Typical use: borrower
    misses June 26, promises July 10 → suspend the June 26 installment, add a
    custom transaction on July 10.

    Deliberately a DEDICATED table, not a flagged ``platform_loan_schedule``
    row: schedule rows carry amortization semantics (installment_number
    ordering, principal/interest split, oldest-first cash filling in
    record_payment, DPD derivation) that a custom transaction must NOT
    participate in.

    Rows are never hard-deleted: removing one flips ``status`` to
    ``cancelled`` (auditable). When the auto-collection job (WS-G) or a staff
    manual payment executes it, ``status`` → ``processed`` and
    ``processed_transaction_id`` links the resulting immutable ledger row.
    ``comment`` is MANDATORY (Dave). Money is integer cents.
    """

    __tablename__ = "platform_loan_custom_transactions"
    __table_args__ = (
        CheckConstraint(
            "amount_cents > 0", name="ck_platform_loan_custom_txn_amount_positive"
        ),
        Index("ix_platform_loan_custom_txn_loan", "loan_id", "scheduled_date"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    loan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_loans.id", ondelete="CASCADE"),
        nullable=False,
    )

    scheduled_date = Column(Date, nullable=False)
    amount_cents = Column(BigInteger, nullable=False)
    # How the cash will be allocated when it executes (reuses the ledger enum).
    repayment_mode = Column(
        ENUM(
            "regular",
            "add_on",
            "special",
            "payoff",
            name="platform_loan_repayment_mode",
            create_type=False,
        ),
        nullable=False,
        default="regular",
    )

    status = Column(
        ENUM(
            "scheduled",
            "processed",
            "cancelled",
            name="platform_loan_custom_txn_status",
            create_type=False,
        ),
        nullable=False,
        default="scheduled",
    )
    # The ledger row this instruction produced, once executed.
    processed_transaction_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_loan_transactions.id"),
        nullable=True,
    )

    # MANDATORY (Dave): why this transaction exists, e.g. "Borrower request,
    # authorization obtained".
    comment = Column(String, nullable=False)
    created_by = Column(String, nullable=False)
    cancelled_by = Column(String, nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    loan = relationship("PlatformLoan", back_populates="custom_transactions")

    def __repr__(self) -> str:
        return (
            f"<PlatformLoanCustomTransaction(loan_id={self.loan_id}, "
            f"date={self.scheduled_date}, amount_cents={self.amount_cents}, "
            f"mode={self.repayment_mode}, status={self.status})>"
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
