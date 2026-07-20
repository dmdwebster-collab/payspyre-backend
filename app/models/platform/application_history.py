"""3-year address + employment history lines for a credit application.

Dave's stability mandate: "Ideally we want three years of both address and
employment information to look at stability factors." Modelled as child tables
(the migration-043 idiom for repeatable structured data — see
``platform_application_secondary_incomes``), one row per declared entry.

The CURRENT address/employment remain the existing scalar columns on
``platform_credit_applications`` (back-compat) — history rows EXTEND them.
When the current scalar fields are edited, the previous values are versioned
into a history row (``entry_source='versioned_edit'``) rather than silently
overwritten, so the file keeps its full audit picture.

Money is integer cents (repo convention). All data fields are nullable so a
partially-completed entry still round-trips; which entries are mandatory for a
decision is a business rule owned by the (configurable) intake validation and
underwriting layers — not hard-coded here. No SIN anywhere in these tables.
"""
from uuid import uuid4

from sqlalchemy import Column, DateTime, String, BigInteger, Boolean, Date, func, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, ENUM
from sqlalchemy.orm import relationship

from app.db.base import Base

# Dave's employment classification: "Employed, really we are looking at
# full-time, part-time, seasonally, self-employed" (+ retired/unemployed/other).
EMPLOYMENT_TYPES = (
    "full_time", "part_time", "seasonal", "self_employed",
    "retired", "unemployed", "other",
)


class PlatformApplicationAddressHistory(Base):
    __tablename__ = "platform_application_address_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    application_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_credit_applications.id"),
        nullable=False,
    )

    # Address fields (mirror the current residence_* scalar columns)
    street = Column(String, nullable=True)
    unit = Column(String, nullable=True)
    city = Column(String, nullable=True)
    province = Column(String, nullable=True)
    postal_code = Column(String, nullable=True)
    residential_status = Column(String, nullable=True)
    monthly_housing_payment_cents = Column(BigInteger, nullable=True)

    # Range + current flag. from_date nullable so a versioned snapshot of a
    # pre-history current entry (exact start unknown) still round-trips.
    from_date = Column(Date, nullable=True)
    to_date = Column(Date, nullable=True)
    is_current = Column(Boolean, nullable=False, default=False)
    # 'applicant' | 'versioned_edit' (auto snapshot on current-entry edit)
    entry_source = Column(String, nullable=False, default="applicant")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    application = relationship(
        "PlatformCreditApplication", back_populates="address_history"
    )

    def __repr__(self) -> str:
        return (
            f"<PlatformApplicationAddressHistory(id={self.id}, "
            f"application_id={self.application_id}, is_current={self.is_current})>"
        )


class PlatformApplicationEmploymentHistory(Base):
    __tablename__ = "platform_application_employment_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    application_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_credit_applications.id"),
        nullable=False,
    )

    employer_name = Column(String, nullable=True)
    job_title = Column(String, nullable=True)
    employment_type = Column(
        ENUM(*EMPLOYMENT_TYPES, name="platform_employment_type", create_type=False),
        nullable=True,
    )

    # Income fields — same enum/shape as primary & secondary income lines so the
    # underwriting layer can treat every income line uniformly.
    income_type = Column(
        ENUM(
            "employed_full_time", "employed_part_time", "employed_seasonal",
            "self_employed", "retirement_pension", "disability",
            "employment_insurance", "other",
            name="platform_income_type", create_type=False,
        ),
        nullable=True,
    )
    net_monthly_income_cents = Column(BigInteger, nullable=True)
    pay_frequency = Column(String, nullable=True)

    from_date = Column(Date, nullable=True)
    to_date = Column(Date, nullable=True)
    is_current = Column(Boolean, nullable=False, default=False)
    entry_source = Column(String, nullable=False, default="applicant")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    application = relationship(
        "PlatformCreditApplication", back_populates="employment_history"
    )

    def __repr__(self) -> str:
        return (
            f"<PlatformApplicationEmploymentHistory(id={self.id}, "
            f"application_id={self.application_id}, is_current={self.is_current})>"
        )
