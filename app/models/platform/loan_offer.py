"""Multi-offer approvals (WS-D) — the Turnkey "Create Loan Offers" model.

An underwriter approves up to N offers per application (settings
``OFFER_MAX_PER_APPLICATION``, default 3); the borrower reviews them on the
dashboard and picks EXACTLY ONE. Acceptance books the loan with the offer's
terms and voids the unpicked siblings; unaccepted offers expire after
``OFFER_EXPIRY_DAYS`` (default 30).

Status machine (forward-only):
    offered ──accept──▶ accepted           (books the loan; siblings → void)
    offered ──sibling accepted──▶ void
    offered ──expires_at passes──▶ expired (job + lazy on-read hook)
    offered ──staff withdraw──▶ withdrawn

Migration 058 enforces at most one ``accepted`` offer per application with a
partial unique index (belt to the service's row locks).
"""
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import relationship

from app.db.base import Base


class PlatformLoanOffer(Base):
    __tablename__ = "platform_loan_offers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    application_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_credit_applications.id"),
        nullable=False,
    )
    credit_product_id = Column(
        UUID(as_uuid=True), ForeignKey("platform_credit_products.id"), nullable=False
    )

    # The offered terms — each validated against the product's bounds at create.
    amount_cents = Column(BigInteger, nullable=False)
    term_months = Column(Integer, nullable=False)
    annual_rate_bps = Column(Integer, nullable=False)
    payment_frequency = Column(String, nullable=False, default="monthly")
    start_date = Column(Date, nullable=True)
    first_due_date = Column(Date, nullable=True)

    status = Column(
        ENUM(
            "offered",
            "accepted",
            "void",
            "expired",
            "withdrawn",
            name="platform_loan_offer_status",
            create_type=False,
        ),
        nullable=False,
        default="offered",
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    note = Column(Text, nullable=True)
    created_by = Column(String, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    application = relationship("PlatformCreditApplication")
    credit_product = relationship("PlatformCreditProduct")

    __table_args__ = (
        Index("ix_platform_loan_offers_application", "application_id"),
        Index("ix_platform_loan_offers_status_expires", "status", "expires_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<PlatformLoanOffer(id={self.id}, application_id={self.application_id}, "
            f"status={self.status})>"
        )
