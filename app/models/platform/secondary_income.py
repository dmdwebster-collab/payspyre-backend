"""Secondary income lines for a credit application.

Dave's canonical credit application allows an applicant to declare zero or more
*secondary* income sources in addition to their single primary income (which
lives as real columns on ``platform_credit_applications``). Because the count is
variable and repeatable, secondary incomes are modelled as their own child table
rather than JSONB — one row per declared source, with the same structured shape
as the primary-income columns so the underwriting layer can treat all income
lines uniformly.

Money is stored as integer cents (repo convention). All fields are nullable so a
partially-completed line still round-trips; the underwriting layer decides which
are mandatory (a business decision left configurable, not hard-coded here).
"""
from uuid import uuid4

from sqlalchemy import Column, DateTime, String, BigInteger, Date, func, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, ENUM
from sqlalchemy.orm import relationship

from app.db.base import Base


class PlatformApplicationSecondaryIncome(Base):
    __tablename__ = "platform_application_secondary_incomes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    application_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_credit_applications.id"),
        nullable=False,
    )

    # Same enum as the primary-income column so every income line is comparable.
    income_type = Column(
        ENUM(name="platform_income_type", create_type=False),
        nullable=True,
    )
    net_monthly_income_cents = Column(BigInteger, nullable=True)
    pay_frequency = Column(String, nullable=True)
    next_pay_date = Column(Date, nullable=True)
    employer_name = Column(String, nullable=True)
    job_title = Column(String, nullable=True)
    hire_date = Column(Date, nullable=True)
    work_phone = Column(String, nullable=True)
    work_phone_ext = Column(String, nullable=True)
    description = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    application = relationship(
        "PlatformCreditApplication", back_populates="secondary_incomes"
    )

    def __repr__(self) -> str:
        return (
            f"<PlatformApplicationSecondaryIncome(id={self.id}, "
            f"application_id={self.application_id}, income_type={self.income_type})>"
        )
