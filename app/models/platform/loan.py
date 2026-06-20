from uuid import uuid4

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    String,
    Integer,
    BigInteger,
    ForeignKey,
    func,
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

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    application_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_credit_applications.id"),
        nullable=False,
    )

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
