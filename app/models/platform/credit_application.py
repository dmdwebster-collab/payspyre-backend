from datetime import datetime
from uuid import UUID, uuid4
from typing import Optional, Any

from sqlalchemy import Column, DateTime, Date, String, Integer, BigInteger, Boolean, func, Text, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB, ENUM
from sqlalchemy.orm import relationship

from app.db.base import Base


class PlatformCreditApplication(Base):
    __tablename__ = "platform_credit_applications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("platform_patients.id"), nullable=False)
    credit_product_id = Column(UUID(as_uuid=True), ForeignKey("platform_credit_products.id"), nullable=False)
    credit_product_version = Column(Integer, nullable=False)
    # Immutable snapshot of the product verification_matrix at creation (migration
    # 026, security finding #6 / Hard Rule #7-8). The decision is made against this,
    # not the live product row. NULL only for rows created before migration 026.
    product_config_snapshot = Column(JSONB, nullable=True)

    # Co-applicant linkage — each co-borrower is a fully separate application
    # file (own documents / verifications / history) linked to the primary via
    # co_applicant_of_application_id + applicant_role (Dave: "a completely
    # separate file for each individual"). relationship_to_primary carries the
    # declared relationship (spouse, parent, …) per the co-borrower dialog
    # (migration 046; indexed for primary→co-borrower file enumeration).
    co_applicant_of_application_id = Column(UUID(as_uuid=True), ForeignKey("platform_credit_applications.id"), nullable=True, index=True)
    applicant_role = Column(
        ENUM("primary", "co_applicant", name="platform_applicant_role", create_type=False),
        nullable=False,
        default="primary"
    )
    relationship_to_primary = Column(String, nullable=True)

    # Requested amount: source tagging
    requested_amount_cents = Column(BigInteger, nullable=False)
    requested_amount_source = Column(
        ENUM("clinic", "patient", "clinic_then_patient_adjusted", name="platform_amount_source", create_type=False),
        nullable=False
    )
    clinic_proposed_amount_cents = Column(BigInteger, nullable=True)
    patient_proposed_amount_cents = Column(BigInteger, nullable=True)

    # Origination context
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=True)
    treatment_plan_ref = Column(String, nullable=True)

    # State — Dave's canonical status workflow maps onto this enum as:
    #   pre-origination -> started
    #   origination     -> origination   (application is being filled out / originated)
    #   verification    -> verifying
    #   underwriting     -> underwriting  (human/automated adjudication in progress)
    #   approved/rejected-> approved / declined
    # The original nine values are retained (additive-only, migration 043) so no
    # existing row or code path breaks. ``origination`` and ``underwriting`` are
    # the two new named states. ``under_review`` remains as the automated-core's
    # manual-review sink (flow_engine DECISION_TO_STATE) and is kept distinct from
    # the explicit ``underwriting`` workflow state.
    status = Column(
        ENUM("started", "origination", "verifying", "pre_qualified", "awaiting_hard_pull",
             "underwriting", "under_review",
             "approved", "declined", "withdrawn", "expired",
             name="platform_application_status", create_type=False),
        nullable=False,
        default="started"
    )
    status_updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Flow state
    flow_state = Column(JSONB, nullable=False, default=lambda: {})

    # Outcome
    decision = Column(JSONB, nullable=True)
    decision_at = Column(DateTime(timezone=True), nullable=True)
    decision_by = Column(String, nullable=True)

    # Underwriting queue assignment (migration 048). Nullable: unassigned is the
    # default. SET NULL on user delete — the assignment history lives in
    # platform_events, not here.
    assigned_to_user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    assigned_at = Column(DateTime(timezone=True), nullable=True)

    # Self-reported overrides
    self_reported = Column(JSONB, nullable=False, default=lambda: {})

    # =====================================================================
    # CANONICAL CREDIT-APPLICATION FIELD SET (Dave's spec, migration 043)
    # ---------------------------------------------------------------------
    # Structured, real columns for the scored core of the application. ALL are
    # nullable + additive: an in-flight application populated field-by-field (by
    # the applicant flow, an integration, or the mock/test-fill helper) still
    # round-trips. Which of these are *mandatory for a decision* is a business
    # decision left to the configurable underwriting layer — NOT hard-coded here.
    #
    # SIN: NOT stored here. The full SIN is the most sensitive PII in the system;
    # it lives ONLY as an encrypted Fernet token on platform_patients.sin_encrypted
    # (with sin_last3 retained). See app/core/sin_crypto.py. This model deliberately
    # carries no raw-SIN column.
    # =====================================================================

    # --- Personal ---------------------------------------------------------
    first_name = Column(String, nullable=True)
    middle_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    date_of_birth = Column(Date, nullable=True)
    marital_status = Column(String, nullable=True)
    number_of_dependents = Column(Integer, nullable=True)
    citizenship = Column(String, nullable=True)
    education = Column(String, nullable=True)
    main_phone = Column(String, nullable=True)
    alternative_phone = Column(String, nullable=True)
    email = Column(String, nullable=True)

    # --- ID verification --------------------------------------------------
    id_type = Column(String, nullable=True)
    id_number = Column(String, nullable=True)
    id_province_of_issue = Column(String, nullable=True)
    id_expiry = Column(Date, nullable=True)

    # --- Residence --------------------------------------------------------
    residence_street = Column(String, nullable=True)
    residence_unit = Column(String, nullable=True)
    residence_city = Column(String, nullable=True)
    residence_province = Column(String, nullable=True)
    residence_postal_code = Column(String, nullable=True)
    time_at_address_years = Column(Integer, nullable=True)
    time_at_address_months = Column(Integer, nullable=True)
    residential_status = Column(String, nullable=True)
    monthly_housing_payment_cents = Column(BigInteger, nullable=True)  # rent OR mortgage

    # --- Primary income ---------------------------------------------------
    # income_type: employed (full/part/seasonal), self-employed, retirement/
    # pension, disability, EI, other — a shared enum reused by secondary incomes.
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
    next_pay_date = Column(Date, nullable=True)
    pay_frequency = Column(String, nullable=True)
    employer_name = Column(String, nullable=True)
    hire_date = Column(Date, nullable=True)
    job_title = Column(String, nullable=True)
    work_phone = Column(String, nullable=True)
    work_phone_ext = Column(String, nullable=True)
    ok_to_contact_at_work = Column(Boolean, nullable=True)

    # --- Financial --------------------------------------------------------
    number_of_credit_accounts = Column(Integer, nullable=True)
    car_ownership = Column(
        ENUM(
            "fully_paid", "financing", "leasing", "none",
            name="platform_car_ownership", create_type=False,
        ),
        nullable=True,
    )
    monthly_car_payment_cents = Column(BigInteger, nullable=True)  # when financing/leasing
    non_discretionary_expenses_cents = Column(BigInteger, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    patient = relationship("PlatformPatient", back_populates="applications")
    credit_product = relationship("PlatformCreditProduct", back_populates="applications")
    co_applicant = relationship("PlatformCreditApplication", remote_side=[id], post_update=True)
    verifications = relationship("PlatformVerification", back_populates="application", cascade="all, delete-orphan")
    consents = relationship("PlatformConsent", back_populates="application", cascade="all, delete-orphan")
    events = relationship("PlatformEvent", back_populates="application", cascade="all, delete-orphan")
    secondary_incomes = relationship(
        "PlatformApplicationSecondaryIncome",
        back_populates="application",
        cascade="all, delete-orphan",
    )
    address_history = relationship(
        "PlatformApplicationAddressHistory",
        back_populates="application",
        cascade="all, delete-orphan",
    )
    employment_history = relationship(
        "PlatformApplicationEmploymentHistory",
        back_populates="application",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<PlatformCreditApplication(id={self.id}, status={self.status}, patient_id={self.patient_id})>"
