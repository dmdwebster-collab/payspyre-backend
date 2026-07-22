"""Dave's **Credit Application v1.0** field spec, as machine-readable data.

Source of truth: ``docs/dave_review_2026-07-21/PaySpyre - Credit Application
v1.0.xlsx`` (219 rows) plus his 2026-07-21 email:

    "the standard credit application gathers all the required information. This
    becomes the users profile information. This user profile information is then
    attached to the requested finance terms and scored to produce an actual
    credit application."

So the **Customer Profile** is the persistent, reusable entity and a credit
application is *profile + finance terms + score*. This module is the ONE
definition of what a profile contains.

WHY DATA AND NOT CODE
---------------------
Every row of Dave's sheet carries eight attributes — Category, Sub-Category,
**Visibility Trigger**, Mandatory, Field Type, Field Options, Format, Character
Limit. Those are a validation spec, not documentation. Encoded here once, they
are read by:

* ``GET /api/v1/admin/profile-schema`` — the manual back-office form and the
  applicant journey render off the registry (no hard-coded form);
* :mod:`app.services.customer_profile_validation` — server-side validation;
* :mod:`app.services.customer_profile` — storage keys, masking and versioning.

Hard-coding the same field list in three places guarantees drift. Same pattern
as :mod:`app.services.application_status`.

BLOCKS, NOT DUPLICATED COLUMNS
------------------------------
Dave's sheet repeats whole blocks: ``Previous Address 1`` restates every
``Current Address`` row, and ``Additional Income 1``/``2`` restate every
``Primary Income`` row verbatim. Those are the SAME shape used more than once,
so the registry declares two reusable shapes (``_address_fields`` and
``_income_fields``) and instantiates them per block. Storage follows: one row
per (profile, block, block_index, field_key) — never 3x duplicated columns.

VISIBILITY TRIGGERS ARE EVALUABLE
---------------------------------
``"If Citizenship is not Canadian"`` becomes
``{"kind": "not_equals", "field": "citizenship", "value": "canadian"}``. A field
that is not visible is never required — that rule lives in the validator, so no
caller can get it wrong.

FIDELITY NOTES (differences from the sheet, all deliberate, all flagged)
-----------------------------------------------------------------------
* ``Hire date`` is typed *Textbox / Alphanumeric / 75* in the sheet while every
  other date is a Date field. Modelled as a DATE with
  ``sheet_discrepancy`` set — open question for Dave.
* ``Income start date`` / ``Income source`` appear TWICE in the sheet (once for
  *Pension / Investment*, once for *Other*). Collapsed to one field whose
  trigger is ``in [pension_investment, other]``.
* ``Email`` is *Alphanumeric + Special Characters* in the sheet; we additionally
  enforce email syntax (``FieldFormat.EMAIL``) because a profile email that is
  not an email breaks every notification.
* The dropdown OPTIONS are stored as stable snake_case codes with Dave's exact
  wording as the label. The wire format is the code; the sheet's text is the
  ``label``.
* Bank Details rows have an empty Field Type and carry
  *"Completed by Bank Verification process or Backend staff"* in the Field
  Options column (NOT a shifted Visibility Trigger). Re-derived from the sheet
  by column position: Mandatory=Yes, Format and Character Limit as given. The
  two "Hidden apart from last N numbers" cells sit in the Visibility Trigger
  column but are a MASKING instruction, not a visibility trigger — encoded as
  ``masking`` and the field stays always-visible.
* Bank Details is **read-through**, not stored here: ``platform_patient_bank_accounts``
  (migration 064, extended by 069) already owns borrower bank accounts and does
  it better than a profile copy could — the full account number is Fernet-
  encrypted, exactly one default payment source is enforced per patient, and
  Flinks-vs-manual provenance is recorded. The registry keeps the block so the
  form renders and validates it; the values are read from that table and writes
  go to its own API. ``institution_number`` is therefore 3 characters (the
  owning column is ``String(3)``), not the sheet's 4.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence


SCHEMA_VERSION = "1.0"
SCHEMA_SOURCE = "PaySpyre - Credit Application v1.0.xlsx (Dave, 2026-07-21)"


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class ProfileBlock(str, Enum):
    """Dave's ten Categories. Order is the order of the form."""

    PERSONAL = "personal"
    CONTACT = "contact"
    CURRENT_ADDRESS = "current_address"
    PREVIOUS_ADDRESS_1 = "previous_address_1"
    IDENTIFICATION = "identification"
    FINANCIAL = "financial"
    PRIMARY_INCOME = "primary_income"
    ADDITIONAL_INCOME_1 = "additional_income_1"
    ADDITIONAL_INCOME_2 = "additional_income_2"
    BANK_DETAILS = "bank_details"


class FieldType(str, Enum):
    """Dave's Field Type column."""

    TEXTBOX = "textbox"
    DATE = "date"
    DROPDOWN = "dropdown"
    SPINNER = "spinner"
    PHONE = "phone"
    POSTAL_CODE = "postal_code"
    SIN = "sin"


class FieldFormat(str, Enum):
    """Dave's Format column (the validation rule)."""

    ALPHA = "alpha"                              # "Alpha Only"
    ALPHANUMERIC = "alphanumeric"                # "Alphanumeric"
    ALPHANUMERIC_SPECIAL = "alphanumeric_special"  # "Alphanumeric + Special Characters"
    EMAIL = "email"                              # ours: alphanumeric+special AND email syntax
    NUMERIC = "numeric"                          # "Numeric"
    CURRENCY = "currency"                        # "Currency"
    DATE = "date"                                # "Date: MM-DD-YYYY" (stored ISO-8601)
    POSTAL = "postal"                            # Canadian postal code
    SIN = "sin"                                  # "SIN#"


class FilledBy(str, Enum):
    """Who populates a field. Dave: automation changes WHO fills, not WHAT."""

    APPLICANT = "applicant"
    STAFF_OR_BANK_VERIFICATION = "staff_or_bank_verification"


#: Presentation format Dave specifies. Values are STORED as ISO-8601 (YYYY-MM-DD);
#: MM-DD-YYYY is a display concern and is surfaced to the UI, not enforced on the wire.
DATE_DISPLAY_FORMAT = "MM-DD-YYYY"


# ---------------------------------------------------------------------------
# Visibility triggers — evaluable data
# ---------------------------------------------------------------------------


class RuleKind(str, Enum):
    ALWAYS = "always"
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    IN = "in"
    NOT_IN = "not_in"
    #: the referenced date is less than ``value`` years ago
    TENURE_LT_YEARS = "tenure_lt_years"
    #: the block instance exists at all (Dave's "Add Additional Income Source" button)
    BLOCK_PRESENT = "block_present"
    ANY_OF = "any_of"
    ALL_OF = "all_of"


@dataclass(frozen=True)
class VisibilityRule:
    """One Visibility Trigger cell, made evaluable.

    ``block`` is the block the referenced ``field`` lives in; ``None`` means "the
    same block instance as the field being evaluated" (the common case).
    """

    kind: RuleKind
    field: Optional[str] = None
    block: Optional[ProfileBlock] = None
    value: Any = None
    rules: tuple["VisibilityRule", ...] = ()
    #: Dave's wording, verbatim, so the UI can show his language
    trigger_text: str = "Always Visable"

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"kind": self.kind.value, "trigger_text": self.trigger_text}
        if self.field is not None:
            out["field"] = self.field
        if self.block is not None:
            out["block"] = self.block.value
        if self.value is not None:
            out["value"] = self.value
        if self.rules:
            out["rules"] = [r.to_dict() for r in self.rules]
        return out


ALWAYS = VisibilityRule(kind=RuleKind.ALWAYS)


def when_equals(field: str, value: str, text: str) -> VisibilityRule:
    return VisibilityRule(kind=RuleKind.EQUALS, field=field, value=value, trigger_text=text)


def when_not_equals(field: str, value: str, text: str) -> VisibilityRule:
    return VisibilityRule(kind=RuleKind.NOT_EQUALS, field=field, value=value, trigger_text=text)


def when_in(field: str, values: Sequence[str], text: str) -> VisibilityRule:
    return VisibilityRule(kind=RuleKind.IN, field=field, value=list(values), trigger_text=text)


def when_tenure_lt(block: ProfileBlock, field: str, years: int, text: str) -> VisibilityRule:
    return VisibilityRule(
        kind=RuleKind.TENURE_LT_YEARS, field=field, block=block, value=years, trigger_text=text
    )


def when_block_present(text: str) -> VisibilityRule:
    return VisibilityRule(kind=RuleKind.BLOCK_PRESENT, trigger_text=text)


# ---------------------------------------------------------------------------
# Field + block specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Option:
    """One Dropdown List row. ``value`` is the stable wire code."""

    value: str
    label: str

    def to_dict(self) -> dict:
        return {"value": self.value, "label": self.label}


@dataclass(frozen=True)
class Masking:
    """"Hidden apart from last N numbers for security" — Dave, Bank Details.

    ``visible_suffix`` is how many trailing characters an unprivileged caller may
    see. The full value is NEVER returned to a caller without the
    ``profile:view_sensitive`` capability (see
    :func:`app.services.customer_profile.mask_value`).
    """

    visible_suffix: int
    reason: str

    def to_dict(self) -> dict:
        return {"visible_suffix": self.visible_suffix, "reason": self.reason}


@dataclass(frozen=True)
class FieldSpec:
    """One row of Dave's sheet (dropdown rows collapsed into ``options``)."""

    key: str
    block: ProfileBlock
    label: str
    field_type: FieldType
    format: FieldFormat
    mandatory: bool
    char_limit: Optional[int] = None
    options: tuple[Option, ...] = ()
    #: for dropdowns whose options are an external list (e.g. countries)
    options_ref: Optional[str] = None
    visible_when: VisibilityRule = ALWAYS
    filled_by: FilledBy = FilledBy.APPLICANT
    masking: Optional[Masking] = None
    #: value is derived/displayed from another field rather than entered
    display_from: Optional[str] = None
    #: value never lands in profile field storage (SIN -> encrypted on the patient)
    external_storage: Optional[str] = None
    note: Optional[str] = None
    #: set where this deviates from the literal sheet cell
    sheet_discrepancy: Optional[str] = None

    @property
    def full_key(self) -> str:
        return f"{self.block.value}.{self.key}"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "full_key": self.full_key,
            "block": self.block.value,
            "label": self.label,
            "field_type": self.field_type.value,
            "format": self.format.value,
            "mandatory": self.mandatory,
            "char_limit": self.char_limit,
            "options": [o.to_dict() for o in self.options],
            "options_ref": self.options_ref,
            "visible_when": self.visible_when.to_dict(),
            "filled_by": self.filled_by.value,
            "masking": self.masking.to_dict() if self.masking else None,
            "display_from": self.display_from,
            "date_display_format": (
                DATE_DISPLAY_FORMAT if self.field_type is FieldType.DATE else None
            ),
            "sensitive": self.masking is not None or self.external_storage is not None,
            "note": self.note,
            "sheet_discrepancy": self.sheet_discrepancy,
        }


@dataclass(frozen=True)
class BlockSpec:
    """A Category of Dave's sheet."""

    block: ProfileBlock
    label: str
    order: int
    fields: tuple[FieldSpec, ...]
    #: gate for the whole block (Previous Address, Additional Income)
    visible_when: VisibilityRule = ALWAYS
    #: bank accounts repeat in the Originations "Bank Accounts" tab
    repeatable: bool = False
    filled_by: FilledBy = FilledBy.APPLICANT
    #: table that OWNS this block's values when they do not live in
    #: ``platform_customer_profile_fields`` (Bank Details -> the borrower
    #: bank-accounts table). Such a block is READ-THROUGH: the profile renders
    #: and validates it but never stores a parallel copy, and profile writes to
    #: it are rejected in favour of the owning surface.
    external_table: Optional[str] = None
    #: the API that owns writes for an ``external_table`` block
    owned_by: Optional[str] = None
    note: Optional[str] = None

    @property
    def is_read_through(self) -> bool:
        return self.external_table is not None

    def field_map(self) -> dict[str, FieldSpec]:
        return {f.key: f for f in self.fields}

    def to_dict(self) -> dict:
        return {
            "block": self.block.value,
            "label": self.label,
            "order": self.order,
            "visible_when": self.visible_when.to_dict(),
            "repeatable": self.repeatable,
            "filled_by": self.filled_by.value,
            "external_table": self.external_table,
            "owned_by": self.owned_by,
            "read_through": self.is_read_through,
            "note": self.note,
            "fields": [f.to_dict() for f in self.fields],
        }


# ---------------------------------------------------------------------------
# Shared option sets (verbatim labels from the sheet)
# ---------------------------------------------------------------------------

PROVINCES: tuple[Option, ...] = tuple(
    Option(code, code)
    for code in ("AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT")
)

SEX_OPTIONS = (
    Option("male", "Male"),
    Option("female", "Female"),
    Option("prefer_not_to_say", "Prefer Not to Say"),
)

CITIZENSHIP_OPTIONS = (
    Option("canadian", "Canadian"),
    Option("resident", "Resident"),
    Option("non_resident", "Non-Resident"),
)

EDUCATION_OPTIONS = (
    Option("none", "None"),
    Option("high_school", "High School"),
    Option("college_university", "College / University"),
    Option("masters_phd", "Masters / PHD"),
    Option("other", "Other"),
)

MARITAL_OPTIONS = (
    Option("single", "Single"),
    Option("married", "Married"),
    Option("divorced", "Divorced"),
    Option("widowed", "Widow(er)"),
    Option("civil_marriage", "Civil Marriage"),
)

ALT_PHONE_TYPE_OPTIONS = (
    Option("alt_phone_number", "Alt. Phone Number"),
    Option("family_member", "Family Member"),
    Option("friend", "Friend"),
    Option("work", "Work"),
)

RESIDENTIAL_STATUS_OPTIONS = (
    Option("rent", "Rent"),
    Option("own_detached", "Own - Detached"),
    Option("own_townhouse_condo", "Own - Townhouse / Condo"),
    Option("own_mobile_owns_land", "Own - Mobile Home Owns Land"),
    Option("own_mobile_rents_land", "Own - Mobile Home Rents Land"),
    Option("living_with_parents", "Living with Parent(s) or Guardian(s)"),
)

#: the "any of the Own options" set Dave's Monthly Mortgage trigger refers to
OWN_RESIDENTIAL_STATUSES = (
    "own_detached",
    "own_townhouse_condo",
    "own_mobile_owns_land",
    "own_mobile_rents_land",
)

ID_TYPE_OPTIONS = (
    Option("drivers_license", "Driver's License"),
    Option("government_photo_id", "Government Photo ID"),
    Option("permanent_residence_card", "Permanent Residence Card"),
    Option("passport", "Passport"),
)

CAR_OWNER_OPTIONS = (
    Option("yes_paid_in_full", "Yes - Paid in Full"),
    Option("yes_financing_leasing", "Yes - Financing / Leasing"),
    Option("no", "No"),
)

INCOME_TYPE_OPTIONS = (
    Option("employed_full_time", "Employed - Full-Time"),
    Option("employed_part_time", "Employed - Part-Time"),
    Option("employed_seasonal", "Employed - Seasonal"),
    Option("self_employed", "Self Employed"),
    Option("pension_investment", "Pension / Investment"),
    Option("disability_insurance", "Disability / Insurance"),
    Option("other", "Other"),
)

EMPLOYED_INCOME_TYPES = ("employed_full_time", "employed_part_time", "employed_seasonal")

PAY_FREQUENCY_OPTIONS = (
    Option("monthly", "Monthly"),
    Option("semi_monthly", "Semi-Monthly"),
    Option("bi_weekly", "Bi-Weekly"),
    Option("weekly", "Weekly"),
)

YES_NO_OPTIONS = (Option("yes", "Yes"), Option("no", "No"))

#: Dave's ``platform_income_type`` engine enum has no ``pension_investment`` /
#: ``disability_insurance`` labels; map his codes onto the existing enum so the
#: scoring layer keeps working unchanged. ``employment_insurance`` exists in the
#: engine enum but NOT in Dave's list — see the PR's open questions.
INCOME_TYPE_TO_ENGINE_ENUM: dict[str, str] = {
    "employed_full_time": "employed_full_time",
    "employed_part_time": "employed_part_time",
    "employed_seasonal": "employed_seasonal",
    "self_employed": "self_employed",
    "pension_investment": "retirement_pension",
    "disability_insurance": "disability",
    "other": "other",
}


# ---------------------------------------------------------------------------
# Reusable block shapes
# ---------------------------------------------------------------------------


def _address_fields(block: ProfileBlock, *, with_payments: bool, with_resided_to: bool) -> tuple[FieldSpec, ...]:
    """Dave's address shape, used by Current Address and Previous Address 1.

    ``with_payments`` — only Current Address carries Monthly Mortgage / Monthly
    Rent (the Previous Address rows in the sheet stop at "Resided at address to").
    """
    fields: list[FieldSpec] = [
        FieldSpec(key="street_address", block=block, label="Street Address",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHANUMERIC,
                  mandatory=True, char_limit=100),
        FieldSpec(key="apartment_unit", block=block, label="Apartment / Unit",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHANUMERIC,
                  mandatory=True, char_limit=10),
        FieldSpec(key="city", block=block, label="City",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHA,
                  mandatory=True, char_limit=100),
        FieldSpec(key="province", block=block, label="Province",
                  field_type=FieldType.DROPDOWN, format=FieldFormat.ALPHA,
                  mandatory=True, options=PROVINCES),
        FieldSpec(key="postal_code", block=block, label="Postal Code",
                  field_type=FieldType.POSTAL_CODE, format=FieldFormat.POSTAL,
                  mandatory=True, char_limit=6),
        FieldSpec(key="residential_status", block=block, label="Residential status",
                  field_type=FieldType.DROPDOWN, format=FieldFormat.ALPHA,
                  mandatory=True, options=RESIDENTIAL_STATUS_OPTIONS),
        FieldSpec(key="resided_since", block=block, label="Resided at address since",
                  field_type=FieldType.DATE, format=FieldFormat.DATE, mandatory=True),
    ]
    if with_resided_to:
        fields.append(
            FieldSpec(
                key="resided_to", block=block, label="Resided at address to",
                field_type=FieldType.DATE, format=FieldFormat.DATE, mandatory=True,
                display_from=f"{ProfileBlock.CURRENT_ADDRESS.value}.resided_since",
                note="Sheet Field Options: 'Display: Current Address Resided at address since'.",
            )
        )
    if with_payments:
        fields += [
            FieldSpec(
                key="monthly_mortgage_payment", block=block, label="Monthly Mortgage Payment",
                field_type=FieldType.TEXTBOX, format=FieldFormat.CURRENCY,
                mandatory=True, char_limit=10,
                visible_when=when_in(
                    "residential_status", OWN_RESIDENTIAL_STATUSES,
                    'If Residential status = any of the "Own" options',
                ),
            ),
            FieldSpec(
                key="monthly_rent", block=block, label="Monthly Rent",
                field_type=FieldType.TEXTBOX, format=FieldFormat.CURRENCY,
                mandatory=True, char_limit=10,
                visible_when=when_equals(
                    "residential_status", "rent", "If Residential status = Rent"
                ),
            ),
        ]
    return tuple(fields)


def _income_fields(block: ProfileBlock) -> tuple[FieldSpec, ...]:
    """Dave's income shape — Primary Income and both Additional Income blocks.

    The sheet restates all 20 rows per block verbatim; they are identical apart
    from the block-level gate, which lives on :class:`BlockSpec`.
    """
    employed = when_in("income_type", EMPLOYED_INCOME_TYPES,
                       'If Income Type = any of the "Employed" options')
    self_emp = when_equals("income_type", "self_employed", 'If Income Type = "Self-Employed"')
    disability = when_equals("income_type", "disability_insurance",
                             'If Income Type = "Disability / Insurance"')
    other = when_equals("income_type", "other", 'If Income Type = "Other"')
    # Sheet lists "Income start date"/"Income source" twice — once for
    # Pension / Investment and once for Other. One field, two triggers.
    pension_or_other = when_in(
        "income_type", ("pension_investment", "other"),
        'If Income Type = "Pension / Investment" or "Other"',
    )
    return (
        FieldSpec(key="income_type", block=block, label="Income Type",
                  field_type=FieldType.DROPDOWN, format=FieldFormat.ALPHA,
                  mandatory=True, options=INCOME_TYPE_OPTIONS),
        FieldSpec(key="net_monthly_income", block=block, label="Net Monthly Income",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.CURRENCY,
                  mandatory=True, char_limit=10),
        FieldSpec(key="next_pay_date", block=block, label="Next pay date",
                  field_type=FieldType.DATE, format=FieldFormat.DATE, mandatory=True),
        FieldSpec(key="pay_frequency", block=block, label="How Often Are You Paid?",
                  field_type=FieldType.DROPDOWN, format=FieldFormat.ALPHA,
                  mandatory=True, options=PAY_FREQUENCY_OPTIONS),
        # --- Employed ---
        FieldSpec(key="employer_name", block=block, label="Employer name",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHANUMERIC,
                  mandatory=True, char_limit=75, visible_when=employed),
        FieldSpec(key="job_title", block=block, label="Job title",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHANUMERIC,
                  mandatory=True, char_limit=75, visible_when=employed),
        FieldSpec(
            key="hire_date", block=block, label="Hire date",
            field_type=FieldType.DATE, format=FieldFormat.DATE,
            mandatory=True, visible_when=employed,
            sheet_discrepancy=(
                "Sheet types this Textbox / Alphanumeric / 75 while every other "
                "date is a Date field. Modelled as a DATE — confirm with Dave."
            ),
        ),
        FieldSpec(key="work_phone", block=block, label="Work phone",
                  field_type=FieldType.PHONE, format=FieldFormat.NUMERIC,
                  mandatory=True, char_limit=10, visible_when=employed),
        FieldSpec(key="work_phone_extension", block=block, label="Work phone extension",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.NUMERIC,
                  mandatory=False, char_limit=10, visible_when=employed),
        # --- Self-employed ---
        FieldSpec(key="company_name", block=block, label="Company name",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHANUMERIC,
                  mandatory=True, char_limit=75, visible_when=self_emp),
        FieldSpec(key="company_founded_date", block=block, label="Company's date of foundation",
                  field_type=FieldType.DATE, format=FieldFormat.DATE,
                  mandatory=True, visible_when=self_emp),
        FieldSpec(key="company_phone", block=block, label="Company phone",
                  field_type=FieldType.PHONE, format=FieldFormat.NUMERIC,
                  mandatory=True, char_limit=10, visible_when=self_emp),
        FieldSpec(key="company_phone_extension", block=block, label="Company phone extension",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.NUMERIC,
                  mandatory=False, char_limit=10, visible_when=self_emp),
        FieldSpec(key="noa_last_2_years_filed", block=block,
                  label="Last 2 Years Notice of Assessments Filed?",
                  field_type=FieldType.DROPDOWN, format=FieldFormat.ALPHA,
                  mandatory=True, options=YES_NO_OPTIONS, visible_when=self_emp),
        # --- Pension / Investment and Other ---
        FieldSpec(key="income_start_date", block=block, label="Income start date",
                  field_type=FieldType.DATE, format=FieldFormat.DATE,
                  mandatory=True, visible_when=pension_or_other),
        FieldSpec(key="income_source", block=block, label="Income source",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHANUMERIC,
                  mandatory=True, char_limit=100, visible_when=pension_or_other),
        # --- Disability / Insurance ---
        FieldSpec(key="benefit_start_date", block=block, label="Benefit start date",
                  field_type=FieldType.DATE, format=FieldFormat.DATE,
                  mandatory=True, visible_when=disability),
        FieldSpec(key="benefit_source", block=block, label="Benefit source",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHANUMERIC,
                  mandatory=True, char_limit=100, visible_when=disability),
        # --- Other ---
        FieldSpec(key="income_verification_phone", block=block,
                  label="Income verification phone",
                  field_type=FieldType.PHONE, format=FieldFormat.NUMERIC,
                  mandatory=True, char_limit=10, visible_when=other),
        FieldSpec(key="income_verification_extension", block=block,
                  label="Income verification extension",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.NUMERIC,
                  mandatory=False, char_limit=10, visible_when=other),
    )


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

_PERSONAL = BlockSpec(
    block=ProfileBlock.PERSONAL,
    label="Personal Information",
    order=1,
    fields=(
        FieldSpec(key="first_name", block=ProfileBlock.PERSONAL, label="First name",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHA,
                  mandatory=True, char_limit=50),
        FieldSpec(key="middle_name", block=ProfileBlock.PERSONAL, label="Middle name",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHA,
                  mandatory=True, char_limit=50,
                  note="Sheet marks this Mandatory=Yes. Flagged for Dave: not every "
                       "person has a middle name."),
        FieldSpec(key="last_name", block=ProfileBlock.PERSONAL, label="Last name",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHA,
                  mandatory=True, char_limit=50),
        FieldSpec(key="date_of_birth", block=ProfileBlock.PERSONAL, label="Date of birth",
                  field_type=FieldType.DATE, format=FieldFormat.DATE, mandatory=True),
        FieldSpec(key="sex", block=ProfileBlock.PERSONAL, label="Sex",
                  field_type=FieldType.DROPDOWN, format=FieldFormat.ALPHA,
                  mandatory=True, options=SEX_OPTIONS),
        FieldSpec(key="citizenship", block=ProfileBlock.PERSONAL, label="Citizenship",
                  field_type=FieldType.DROPDOWN, format=FieldFormat.ALPHA,
                  mandatory=True, options=CITIZENSHIP_OPTIONS),
        FieldSpec(key="country_of_citizenship", block=ProfileBlock.PERSONAL,
                  label="Country of Citizenship",
                  field_type=FieldType.DROPDOWN, format=FieldFormat.ALPHA,
                  mandatory=True, options_ref="country_list",
                  visible_when=when_not_equals("citizenship", "canadian",
                                               "If Citizenship is not Canadian"),
                  note="Sheet Field Options: 'List of Countries'. Which list "
                       "(ISO 3166-1 alpha-2?) is an open question for Dave — the "
                       "registry does not enum-check this field yet."),
        FieldSpec(key="education", block=ProfileBlock.PERSONAL, label="Education",
                  field_type=FieldType.DROPDOWN, format=FieldFormat.ALPHA,
                  mandatory=True, options=EDUCATION_OPTIONS),
        FieldSpec(key="education_details", block=ProfileBlock.PERSONAL,
                  label="Education Details",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHANUMERIC,
                  mandatory=True, char_limit=75,
                  visible_when=when_equals("education", "other", "If Education = Other")),
        FieldSpec(key="marital_status", block=ProfileBlock.PERSONAL, label="Marital status",
                  field_type=FieldType.DROPDOWN, format=FieldFormat.ALPHA,
                  mandatory=True, options=MARITAL_OPTIONS),
        FieldSpec(key="number_of_dependents", block=ProfileBlock.PERSONAL,
                  label="Number of dependents",
                  field_type=FieldType.SPINNER, format=FieldFormat.NUMERIC,
                  mandatory=True, char_limit=2),
    ),
)

_CONTACT = BlockSpec(
    block=ProfileBlock.CONTACT,
    label="Contact Information",
    order=2,
    fields=(
        FieldSpec(key="email", block=ProfileBlock.CONTACT, label="Email",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.EMAIL,
                  mandatory=True, char_limit=75,
                  note="Sheet format is 'Alphanumeric + Special Characters'; we "
                       "additionally enforce email syntax."),
        FieldSpec(key="main_phone", block=ProfileBlock.CONTACT, label="Main phone",
                  field_type=FieldType.PHONE, format=FieldFormat.NUMERIC,
                  mandatory=True, char_limit=10),
        FieldSpec(key="alternative_phone", block=ProfileBlock.CONTACT,
                  label="Alternative phone",
                  field_type=FieldType.PHONE, format=FieldFormat.NUMERIC,
                  mandatory=True, char_limit=10),
        FieldSpec(key="alternative_phone_type", block=ProfileBlock.CONTACT,
                  label="Alternative Phone Type",
                  field_type=FieldType.DROPDOWN, format=FieldFormat.ALPHA,
                  mandatory=True, options=ALT_PHONE_TYPE_OPTIONS),
        FieldSpec(key="alternative_phone_name", block=ProfileBlock.CONTACT,
                  label="Alternative Phone Name",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHA,
                  mandatory=True, char_limit=50,
                  visible_when=when_in(
                      "alternative_phone_type", ("family_member", "friend"),
                      "If Alternative Phone Type = Family Member or Friend")),
    ),
)

_CURRENT_ADDRESS = BlockSpec(
    block=ProfileBlock.CURRENT_ADDRESS,
    label="Current Address",
    order=3,
    fields=_address_fields(ProfileBlock.CURRENT_ADDRESS, with_payments=True, with_resided_to=False),
)

_PREVIOUS_ADDRESS_1 = BlockSpec(
    block=ProfileBlock.PREVIOUS_ADDRESS_1,
    label="Previous Address 1",
    order=4,
    visible_when=when_tenure_lt(
        ProfileBlock.CURRENT_ADDRESS, "resided_since", 3,
        "If Current Address Resided at address since < 3 years",
    ),
    fields=_address_fields(
        ProfileBlock.PREVIOUS_ADDRESS_1, with_payments=False, with_resided_to=True
    ),
    note=(
        "Dave specifies exactly ONE previous-address block. If tenure at the "
        "PREVIOUS address is also < 3 years there is no Previous Address 2 in "
        "his spec — open question (the earlier mandate was 3 years of address "
        "history, which one block cannot always satisfy)."
    ),
)

_IDENTIFICATION = BlockSpec(
    block=ProfileBlock.IDENTIFICATION,
    label="Identification",
    order=5,
    fields=(
        FieldSpec(
            key="social_insurance_number", block=ProfileBlock.IDENTIFICATION,
            label="Social Insurance Number",
            field_type=FieldType.SIN, format=FieldFormat.SIN,
            mandatory=False, char_limit=9,
            external_storage="platform_patients.sin_encrypted",
            masking=Masking(visible_suffix=3, reason="SIN is never returned in full"),
            note=(
                "Mandatory=No in the sheet and it stays that way — Dave: a SIN "
                "legally cannot be required. Stored ONLY as a Fernet token on "
                "platform_patients.sin_encrypted (app.core.sin_crypto); the "
                "profile field row holds sin_last3 and nothing else."
            ),
        ),
        FieldSpec(key="id_type", block=ProfileBlock.IDENTIFICATION,
                  label="Type of ID Verification",
                  field_type=FieldType.DROPDOWN, format=FieldFormat.ALPHA,
                  mandatory=True, options=ID_TYPE_OPTIONS),
        FieldSpec(key="drivers_license_number", block=ProfileBlock.IDENTIFICATION,
                  label="Driver's License #",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHANUMERIC,
                  mandatory=True, char_limit=25,
                  visible_when=when_equals("id_type", "drivers_license",
                                           'If Type of ID Verification = "Driver\'s License"')),
        FieldSpec(key="government_photo_id_number", block=ProfileBlock.IDENTIFICATION,
                  label="Government Photo ID #",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHANUMERIC,
                  mandatory=True, char_limit=25,
                  visible_when=when_equals("id_type", "government_photo_id",
                                           'If Type of ID Verification = "Government Photo ID"')),
        FieldSpec(key="permanent_residence_card_number", block=ProfileBlock.IDENTIFICATION,
                  label="Permanent Residence Card #",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHANUMERIC,
                  mandatory=True, char_limit=25,
                  visible_when=when_equals(
                      "id_type", "permanent_residence_card",
                      'If Type of ID Verification = "Permanent Residence Card"')),
        FieldSpec(key="passport_number", block=ProfileBlock.IDENTIFICATION,
                  label="Passport #",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHANUMERIC,
                  mandatory=True, char_limit=25,
                  visible_when=when_equals("id_type", "passport",
                                           'If Type of ID Verification = "Passport"')),
        FieldSpec(key="expiry_date", block=ProfileBlock.IDENTIFICATION, label="Expiry Date",
                  field_type=FieldType.DATE, format=FieldFormat.DATE, mandatory=False),
        FieldSpec(key="province_of_issue", block=ProfileBlock.IDENTIFICATION,
                  label="Province of Issue",
                  field_type=FieldType.DROPDOWN, format=FieldFormat.ALPHA,
                  mandatory=True, options=PROVINCES),
    ),
)

_FINANCIAL = BlockSpec(
    block=ProfileBlock.FINANCIAL,
    label="Financial Information",
    order=6,
    fields=(
        FieldSpec(key="car_owner", block=ProfileBlock.FINANCIAL, label="Car Owner",
                  field_type=FieldType.DROPDOWN, format=FieldFormat.ALPHA,
                  mandatory=True, options=CAR_OWNER_OPTIONS),
        FieldSpec(key="monthly_car_payment", block=ProfileBlock.FINANCIAL,
                  label="Monthly Car Payment",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.CURRENCY,
                  mandatory=True, char_limit=10,
                  visible_when=when_equals(
                      "car_owner", "yes_financing_leasing",
                      'IF Car Owner is "Yes - Financing / Leasing"')),
        FieldSpec(key="number_of_credit_accounts", block=ProfileBlock.FINANCIAL,
                  label="# of Credit Accounts",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.NUMERIC,
                  mandatory=True, char_limit=5),
        FieldSpec(key="monthly_credit_payments", block=ProfileBlock.FINANCIAL,
                  label="Monthly Credit Payments",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.CURRENCY,
                  mandatory=True, char_limit=10),
        FieldSpec(key="other_monthly_expenses", block=ProfileBlock.FINANCIAL,
                  label="Other Monthly Expenses",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.CURRENCY,
                  mandatory=True, char_limit=10),
    ),
)

_PRIMARY_INCOME = BlockSpec(
    block=ProfileBlock.PRIMARY_INCOME,
    label="Primary Income",
    order=7,
    fields=_income_fields(ProfileBlock.PRIMARY_INCOME),
)

_ADDITIONAL_INCOME_1 = BlockSpec(
    block=ProfileBlock.ADDITIONAL_INCOME_1,
    label="Additional Income 1",
    order=8,
    visible_when=when_block_present("If Add Additional Income Source Button Pressed"),
    fields=_income_fields(ProfileBlock.ADDITIONAL_INCOME_1),
    note="Added by Dave's 'Add Additional Income Source' button (1st click); "
         "removable via its own Remove button (delete the block instance).",
)

_ADDITIONAL_INCOME_2 = BlockSpec(
    block=ProfileBlock.ADDITIONAL_INCOME_2,
    label="Additional Income 2",
    order=9,
    visible_when=when_block_present("If Add Additional Income Source Button Pressed"),
    fields=_income_fields(ProfileBlock.ADDITIONAL_INCOME_2),
    note="Added by the 2nd click of 'Add Additional Income Source'. The sheet's "
         "Remove button row for this block is labelled 'Remove Additional "
         "Income 1' — a copy/paste slip; it removes block 2.",
)

_BANK_DETAILS = BlockSpec(
    block=ProfileBlock.BANK_DETAILS,
    label="Bank Details",
    order=10,
    repeatable=True,
    filled_by=FilledBy.STAFF_OR_BANK_VERIFICATION,
    external_table="platform_patient_bank_accounts",
    owned_by="/api/v1/admin/borrower-security (admin_borrower_security.py)",
    note=(
        "Every Bank Details row carries 'Completed by Bank Verification process "
        "or Backend staff' — the borrower never types these. "
        "READ-THROUGH: these values are OWNED by ``platform_patient_bank_accounts`` "
        "(migration 064, extended by 069 with institution/transit/holder/"
        "account_number_encrypted/source). The profile renders and validates the "
        "block but stores no parallel copy — that table already Fernet-encrypts "
        "the full account number, enforces one default payment source per patient "
        "and records Flinks-vs-manual provenance, none of which a second copy "
        "could keep in step. Repeatable because that table is one row per account."
    ),
    fields=(
        FieldSpec(key="bank_name", block=ProfileBlock.BANK_DETAILS, label="Bank name",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHANUMERIC,
                  mandatory=True, char_limit=50,
                  filled_by=FilledBy.STAFF_OR_BANK_VERIFICATION,
                  external_storage="platform_patient_bank_accounts.institution_name"),
        FieldSpec(key="institution_number", block=ProfileBlock.BANK_DETAILS,
                  label="Bank institution number",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.NUMERIC,
                  mandatory=True, char_limit=3,
                  filled_by=FilledBy.STAFF_OR_BANK_VERIFICATION,
                  external_storage="platform_patient_bank_accounts.institution_number",
                  sheet_discrepancy=(
                      "Sheet says 4. Resolved to 3: the owning column is "
                      "String(3) and the Originations review specifies 3 with "
                      "leading-zero handling. Still worth confirming with Dave."
                  )),
        FieldSpec(key="transit_number", block=ProfileBlock.BANK_DETAILS,
                  label="Bank transit number",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.NUMERIC,
                  mandatory=True, char_limit=5,
                  filled_by=FilledBy.STAFF_OR_BANK_VERIFICATION,
                  external_storage="platform_patient_bank_accounts.transit_number",
                  masking=Masking(
                      visible_suffix=2,
                      reason="Hidden apart from last 2 numbers for security")),
        FieldSpec(key="account_holder_name", block=ProfileBlock.BANK_DETAILS,
                  label="Account holder's full name",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHA,
                  mandatory=True, char_limit=50,
                  filled_by=FilledBy.STAFF_OR_BANK_VERIFICATION,
                  external_storage="platform_patient_bank_accounts.account_holder"),
        FieldSpec(key="account_number", block=ProfileBlock.BANK_DETAILS,
                  label="Account number",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.NUMERIC,
                  mandatory=True, char_limit=25,
                  filled_by=FilledBy.STAFF_OR_BANK_VERIFICATION,
                  external_storage=(
                      "platform_patient_bank_accounts.account_number_encrypted"
                  ),
                  masking=Masking(
                      visible_suffix=3,
                      reason="Hidden apart from last 3 numbers for security")),
        FieldSpec(key="account_type", block=ProfileBlock.BANK_DETAILS, label="Account type",
                  field_type=FieldType.TEXTBOX, format=FieldFormat.ALPHA,
                  mandatory=True, char_limit=25,
                  filled_by=FilledBy.STAFF_OR_BANK_VERIFICATION,
                  external_storage="platform_patient_bank_accounts.account_type",
                  note="The sheet gives no options. The Originations review says "
                       "Checking / Savings — left free-text until confirmed."),
    ),
)


BLOCKS: tuple[BlockSpec, ...] = (
    _PERSONAL,
    _CONTACT,
    _CURRENT_ADDRESS,
    _PREVIOUS_ADDRESS_1,
    _IDENTIFICATION,
    _FINANCIAL,
    _PRIMARY_INCOME,
    _ADDITIONAL_INCOME_1,
    _ADDITIONAL_INCOME_2,
    _BANK_DETAILS,
)

BLOCK_REGISTRY: dict[ProfileBlock, BlockSpec] = {b.block: b for b in BLOCKS}

#: full_key ("personal.first_name") -> spec
FIELD_REGISTRY: dict[str, FieldSpec] = {
    f.full_key: f for b in BLOCKS for f in b.fields
}

#: the income blocks, in the order the "Add Additional Income Source" button fills them
INCOME_BLOCKS: tuple[ProfileBlock, ...] = (
    ProfileBlock.PRIMARY_INCOME,
    ProfileBlock.ADDITIONAL_INCOME_1,
    ProfileBlock.ADDITIONAL_INCOME_2,
)

ADDRESS_BLOCKS: tuple[ProfileBlock, ...] = (
    ProfileBlock.CURRENT_ADDRESS,
    ProfileBlock.PREVIOUS_ADDRESS_1,
)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def block_spec(block: ProfileBlock | str) -> Optional[BlockSpec]:
    """The BlockSpec for a block name, or ``None`` when unknown."""
    try:
        key = block if isinstance(block, ProfileBlock) else ProfileBlock(block)
    except ValueError:
        return None
    return BLOCK_REGISTRY.get(key)


def field_spec(block: ProfileBlock | str, key: str) -> Optional[FieldSpec]:
    """The FieldSpec for (block, key), or ``None`` when unknown."""
    block_value = block.value if isinstance(block, ProfileBlock) else str(block)
    return FIELD_REGISTRY.get(f"{block_value}.{key}")


def mandatory_fields(block: ProfileBlock) -> tuple[FieldSpec, ...]:
    spec = block_spec(block)
    return tuple(f for f in spec.fields if f.mandatory) if spec else ()


def masked_fields() -> tuple[FieldSpec, ...]:
    """Every field carrying a masking instruction (bank transit/account + SIN)."""
    return tuple(f for f in FIELD_REGISTRY.values() if f.masking is not None)


# ---------------------------------------------------------------------------
# Visibility evaluation
# ---------------------------------------------------------------------------

#: a profile's values: instance key -> {field key: value}
ProfileValues = Mapping[str, Mapping[str, Any]]


def instance_key(block: ProfileBlock | str, index: int = 0) -> str:
    """Storage/lookup key for one block instance.

    Index 0 (every non-repeatable block, and the first bank account) is just the
    block name so the common case reads naturally.
    """
    name = block.value if isinstance(block, ProfileBlock) else str(block)
    return name if index == 0 else f"{name}#{index}"


def parse_instance_key(key: str) -> tuple[str, int]:
    """Inverse of :func:`instance_key`."""
    if "#" in key:
        name, _, idx = key.partition("#")
        try:
            return name, int(idx)
        except ValueError:
            return name, 0
    return key, 0


def _years_since(value: Any, *, today: Optional[date] = None) -> Optional[float]:
    """Approximate years between ``value`` (a date or ISO string) and today."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value[:10])
        except ValueError:
            return None
    if not isinstance(value, date):
        return None
    reference = today or date.today()
    return (reference - value).days / 365.2425


def evaluate_rule(
    rule: VisibilityRule,
    values: ProfileValues,
    *,
    own_instance: str,
    today: Optional[date] = None,
) -> bool:
    """Evaluate one visibility trigger against a profile's values."""
    if rule.kind is RuleKind.ALWAYS:
        return True
    if rule.kind is RuleKind.BLOCK_PRESENT:
        instance = values.get(own_instance) or {}
        return any(v not in (None, "") for v in instance.values())
    if rule.kind is RuleKind.ANY_OF:
        return any(
            evaluate_rule(r, values, own_instance=own_instance, today=today) for r in rule.rules
        )
    if rule.kind is RuleKind.ALL_OF:
        return all(
            evaluate_rule(r, values, own_instance=own_instance, today=today) for r in rule.rules
        )

    source = instance_key(rule.block) if rule.block is not None else own_instance
    current = (values.get(source) or {}).get(rule.field)

    if rule.kind is RuleKind.EQUALS:
        return current == rule.value
    if rule.kind is RuleKind.NOT_EQUALS:
        # An unanswered driver does not make a dependent field visible.
        return current is not None and current != "" and current != rule.value
    if rule.kind is RuleKind.IN:
        return current in tuple(rule.value or ())
    if rule.kind is RuleKind.NOT_IN:
        return current is not None and current != "" and current not in tuple(rule.value or ())
    if rule.kind is RuleKind.TENURE_LT_YEARS:
        years = _years_since(current, today=today)
        return years is not None and years < float(rule.value)
    return False


def is_block_visible(
    block: ProfileBlock,
    values: ProfileValues,
    *,
    index: int = 0,
    today: Optional[date] = None,
) -> bool:
    spec = BLOCK_REGISTRY.get(block)
    if spec is None:
        return False
    return evaluate_rule(
        spec.visible_when, values, own_instance=instance_key(block, index), today=today
    )


def is_field_visible(
    spec: FieldSpec,
    values: ProfileValues,
    *,
    index: int = 0,
    today: Optional[date] = None,
) -> bool:
    """A field is visible only when its BLOCK is visible AND its own trigger fires.

    This is the rule the validator leans on: an invisible field is never
    required, so "mandatory" is always "mandatory *when visible*".
    """
    if not is_block_visible(spec.block, values, index=index, today=today):
        return False
    return evaluate_rule(
        spec.visible_when, values, own_instance=instance_key(spec.block, index), today=today
    )


def visible_fields(
    values: ProfileValues, *, today: Optional[date] = None
) -> tuple[FieldSpec, ...]:
    """Every field currently visible for these values (index-0 instances only)."""
    return tuple(
        f
        for b in BLOCKS
        for f in b.fields
        if is_field_visible(f, values, today=today)
    )


# ---------------------------------------------------------------------------
# Wire payload
# ---------------------------------------------------------------------------


def schema_payload() -> dict:
    """The whole registry, JSON-serializable — what ``/admin/profile-schema`` returns."""
    return {
        "version": SCHEMA_VERSION,
        "source": SCHEMA_SOURCE,
        "date_display_format": DATE_DISPLAY_FORMAT,
        "date_storage_format": "ISO-8601 (YYYY-MM-DD)",
        "blocks": [b.to_dict() for b in sorted(BLOCKS, key=lambda b: b.order)],
        "option_sets": {
            "provinces": [o.to_dict() for o in PROVINCES],
            "income_types": [o.to_dict() for o in INCOME_TYPE_OPTIONS],
            "residential_statuses": [o.to_dict() for o in RESIDENTIAL_STATUS_OPTIONS],
        },
        "rule_kinds": [k.value for k in RuleKind],
        "field_count": len(FIELD_REGISTRY),
    }


def iter_field_specs() -> Iterable[FieldSpec]:
    for block in BLOCKS:
        yield from block.fields
