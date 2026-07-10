"""Serialization for the canonical credit application (migration 043).

A single, shared projection of a ``PlatformCreditApplication`` (+ its patient +
secondary-income child rows) into the full canonical field set the new frontend
consumes — grouped into the sections of the applications workspace: Personal, ID,
Residence, Primary income, Secondary incomes, Financial.

Used by BOTH the admin detail endpoint and the applicant review/detail endpoint,
so the two views can never drift apart.

SIN RULE (hard): the raw/encrypted SIN is NEVER read or returned here. SIN
presence is surfaced only as ``sin_last3`` (+ collected/declined flags), which
live on the patient. This module deliberately has no path to the encrypted token.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel

from app.models.platform.credit_application import PlatformCreditApplication


# ---------------------------------------------------------------------------
# Section schemas
# ---------------------------------------------------------------------------


class PersonalSection(BaseModel):
    first_name: Optional[str] = None
    middle_name: Optional[str] = None
    last_name: Optional[str] = None
    date_of_birth: Optional[date] = None
    marital_status: Optional[str] = None
    number_of_dependents: Optional[int] = None
    citizenship: Optional[str] = None
    education: Optional[str] = None
    main_phone: Optional[str] = None
    alternative_phone: Optional[str] = None
    email: Optional[str] = None


class IdSection(BaseModel):
    id_type: Optional[str] = None
    id_number: Optional[str] = None
    id_province_of_issue: Optional[str] = None
    id_expiry: Optional[date] = None
    # SIN is surfaced ONLY as the masked last-3 (from the patient) — never raw.
    sin_last3: Optional[str] = None
    sin_collected: bool = False
    sin_declined: bool = False


class ResidenceSection(BaseModel):
    residence_street: Optional[str] = None
    residence_unit: Optional[str] = None
    residence_city: Optional[str] = None
    residence_province: Optional[str] = None
    residence_postal_code: Optional[str] = None
    time_at_address_years: Optional[int] = None
    time_at_address_months: Optional[int] = None
    residential_status: Optional[str] = None
    monthly_housing_payment_cents: Optional[int] = None


class PrimaryIncomeSection(BaseModel):
    income_type: Optional[str] = None
    net_monthly_income_cents: Optional[int] = None
    next_pay_date: Optional[date] = None
    pay_frequency: Optional[str] = None
    employer_name: Optional[str] = None
    hire_date: Optional[date] = None
    job_title: Optional[str] = None
    work_phone: Optional[str] = None
    work_phone_ext: Optional[str] = None
    ok_to_contact_at_work: Optional[bool] = None


class SecondaryIncomeItem(BaseModel):
    id: UUID
    income_type: Optional[str] = None
    net_monthly_income_cents: Optional[int] = None
    pay_frequency: Optional[str] = None
    next_pay_date: Optional[date] = None
    employer_name: Optional[str] = None
    job_title: Optional[str] = None
    hire_date: Optional[date] = None
    work_phone: Optional[str] = None
    work_phone_ext: Optional[str] = None
    description: Optional[str] = None


class FinancialSection(BaseModel):
    number_of_credit_accounts: Optional[int] = None
    car_ownership: Optional[str] = None
    monthly_car_payment_cents: Optional[int] = None
    non_discretionary_expenses_cents: Optional[int] = None


class AddressHistoryItem(BaseModel):
    id: UUID
    street: Optional[str] = None
    unit: Optional[str] = None
    city: Optional[str] = None
    province: Optional[str] = None
    postal_code: Optional[str] = None
    residential_status: Optional[str] = None
    monthly_housing_payment_cents: Optional[int] = None
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    is_current: bool = False
    entry_source: str = "applicant"


class EmploymentHistoryItem(BaseModel):
    id: UUID
    employer_name: Optional[str] = None
    job_title: Optional[str] = None
    employment_type: Optional[str] = None
    income_type: Optional[str] = None
    net_monthly_income_cents: Optional[int] = None
    pay_frequency: Optional[str] = None
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    is_current: bool = False
    entry_source: str = "applicant"


class CoBorrowerSection(BaseModel):
    """Separate-file co-borrower linkage (Dave: each co-applicant is a fully
    separate application file, linked to the primary)."""

    applicant_role: str = "primary"
    # Set on a CO-BORROWER file: the primary application it belongs to.
    co_applicant_of_application_id: Optional[UUID] = None
    relationship_to_primary: Optional[str] = None
    # Set on a PRIMARY file: its linked co-borrower application files.
    linked_application_ids: list[UUID] = []


class CanonicalApplicationDetail(BaseModel):
    """The full canonical field set for the applications workspace."""

    id: UUID
    status: str
    created_at: datetime
    status_updated_at: datetime
    requested_amount_cents: int
    requested_amount_source: str
    applicant_role: str

    personal: PersonalSection
    identification: IdSection
    residence: ResidenceSection
    primary_income: PrimaryIncomeSection
    secondary_incomes: list[SecondaryIncomeItem]
    financial: FinancialSection
    # 3-year histories (migration 046) — sorted current-first, then most recent.
    address_history: list[AddressHistoryItem] = []
    employment_history: list[EmploymentHistoryItem] = []
    co_borrower: CoBorrowerSection = CoBorrowerSection()


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def _copy(model_cls: type[BaseModel], source: Any, fields: tuple[str, ...]) -> BaseModel:
    return model_cls(**{f: getattr(source, f, None) for f in fields})


_PERSONAL = (
    "first_name", "middle_name", "last_name", "date_of_birth", "marital_status",
    "number_of_dependents", "citizenship", "education", "main_phone",
    "alternative_phone", "email",
)
_ID = ("id_type", "id_number", "id_province_of_issue", "id_expiry")
_RESIDENCE = (
    "residence_street", "residence_unit", "residence_city", "residence_province",
    "residence_postal_code", "time_at_address_years", "time_at_address_months",
    "residential_status", "monthly_housing_payment_cents",
)
_PRIMARY_INCOME = (
    "income_type", "net_monthly_income_cents", "next_pay_date", "pay_frequency",
    "employer_name", "hire_date", "job_title", "work_phone", "work_phone_ext",
    "ok_to_contact_at_work",
)
_SECONDARY = (
    "income_type", "net_monthly_income_cents", "pay_frequency", "next_pay_date",
    "employer_name", "job_title", "hire_date", "work_phone", "work_phone_ext",
    "description",
)
_FINANCIAL = (
    "number_of_credit_accounts", "car_ownership", "monthly_car_payment_cents",
    "non_discretionary_expenses_cents",
)
_ADDRESS_HISTORY = (
    "street", "unit", "city", "province", "postal_code", "residential_status",
    "monthly_housing_payment_cents", "from_date", "to_date", "is_current",
)
_EMPLOYMENT_HISTORY = (
    "employer_name", "job_title", "employment_type", "income_type",
    "net_monthly_income_cents", "pay_frequency", "from_date", "to_date",
    "is_current",
)
# String-typed fields that may come back enum-ish from the DB — coerce to str.
_HISTORY_ENUMISH = ("employment_type", "income_type")


def _history_sort_key(row: Any) -> tuple:
    """Current entries first, then most recent from_date (None last)."""
    from_date = getattr(row, "from_date", None)
    return (
        0 if getattr(row, "is_current", False) else 1,
        -(from_date.toordinal()) if from_date is not None else 0,
    )


def _history_items(
    rows: Any, item_cls: type[BaseModel], fields: tuple[str, ...]
) -> list[BaseModel]:
    items = []
    for row in sorted(rows or [], key=_history_sort_key):
        kwargs = {f: getattr(row, f, None) for f in fields}
        for enumish in _HISTORY_ENUMISH:
            if kwargs.get(enumish) is not None:
                kwargs[enumish] = str(kwargs[enumish])
        items.append(
            item_cls(
                id=row.id,
                entry_source=str(getattr(row, "entry_source", None) or "applicant"),
                **kwargs,
            )
        )
    return items


def build_canonical_detail(
    application: PlatformCreditApplication,
    patient: Any,
    linked_application_ids: Optional[list[UUID]] = None,
) -> CanonicalApplicationDetail:
    """Project an application (+ its patient + secondary-income + history rows)
    into the full canonical detail. ``patient`` may be None (SIN section then
    reports no SIN). Enum columns come back as their string value.

    ``linked_application_ids``: co-borrower application files linked to this
    (primary) file — computed by the caller (a query), since the serializer is
    session-agnostic. Defaults to none.

    SIN: only ``patient.sin_last3`` + the collected/declined flags are read — the
    encrypted token is never touched.
    """
    identification = _copy(IdSection, application, _ID)
    if patient is not None:
        identification.sin_last3 = getattr(patient, "sin_last3", None)
        identification.sin_collected = getattr(patient, "sin_collected_at", None) is not None
        identification.sin_declined = bool(getattr(patient, "sin_declined", False))

    secondary = [
        SecondaryIncomeItem(id=s.id, **{f: getattr(s, f, None) for f in _SECONDARY})
        for s in sorted(
            application.secondary_incomes or [],
            key=lambda s: getattr(s, "created_at", None) or datetime.min,
        )
    ]

    return CanonicalApplicationDetail(
        id=application.id,
        status=str(application.status),
        created_at=application.created_at,
        status_updated_at=application.status_updated_at,
        requested_amount_cents=application.requested_amount_cents,
        requested_amount_source=str(application.requested_amount_source),
        applicant_role=str(application.applicant_role),
        personal=_copy(PersonalSection, application, _PERSONAL),
        identification=identification,
        residence=_copy(ResidenceSection, application, _RESIDENCE),
        primary_income=_copy(PrimaryIncomeSection, application, _PRIMARY_INCOME),
        secondary_incomes=secondary,
        financial=_copy(FinancialSection, application, _FINANCIAL),
        address_history=_history_items(
            getattr(application, "address_history", None), AddressHistoryItem, _ADDRESS_HISTORY
        ),
        employment_history=_history_items(
            getattr(application, "employment_history", None),
            EmploymentHistoryItem,
            _EMPLOYMENT_HISTORY,
        ),
        co_borrower=CoBorrowerSection(
            applicant_role=str(application.applicant_role),
            co_applicant_of_application_id=getattr(
                application, "co_applicant_of_application_id", None
            ),
            relationship_to_primary=getattr(application, "relationship_to_primary", None),
            linked_application_ids=linked_application_ids or [],
        ),
    )
