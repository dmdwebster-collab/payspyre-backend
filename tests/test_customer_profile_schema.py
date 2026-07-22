"""DB-free tests for the Customer Profile registry + validator.

These assert FIDELITY to Dave's ``PaySpyre - Credit Application v1.0.xlsx``
(219 rows) and the behaviour that spec implies — above all: **a field that is
not visible is never required**.
"""
from datetime import date, timedelta

import pytest

from app.services import customer_profile_schema as schema
from app.services.customer_profile_schema import (
    FieldFormat,
    FieldType,
    ProfileBlock,
    instance_key,
    is_block_visible,
    is_field_visible,
    parse_instance_key,
)
from app.services.customer_profile_validation import (
    ErrorCode,
    completeness,
    validate_profile,
)


# ---------------------------------------------------------------------------
# Registry shape / fidelity to the sheet
# ---------------------------------------------------------------------------


def test_all_ten_categories_present():
    assert [b.block for b in schema.BLOCKS] == [
        ProfileBlock.PERSONAL,
        ProfileBlock.CONTACT,
        ProfileBlock.CURRENT_ADDRESS,
        ProfileBlock.PREVIOUS_ADDRESS_1,
        ProfileBlock.IDENTIFICATION,
        ProfileBlock.FINANCIAL,
        ProfileBlock.PRIMARY_INCOME,
        ProfileBlock.ADDITIONAL_INCOME_1,
        ProfileBlock.ADDITIONAL_INCOME_2,
        ProfileBlock.BANK_DETAILS,
    ]


def test_field_keys_are_unique_and_registered():
    keys = [f.full_key for b in schema.BLOCKS for f in b.fields]
    assert len(keys) == len(set(keys))
    assert set(keys) == set(schema.FIELD_REGISTRY)


def test_sin_is_optional_and_never_stored_in_the_profile():
    """Dave: a SIN legally cannot be required."""
    sin = schema.field_spec(ProfileBlock.IDENTIFICATION, "social_insurance_number")
    assert sin.mandatory is False
    assert sin.char_limit == 9
    assert sin.external_storage == "platform_patients.sin_encrypted"
    assert sin.masking.visible_suffix == 3


def test_id_expiry_is_the_only_other_non_mandatory_identification_field():
    optional = {
        f.key
        for f in schema.BLOCK_REGISTRY[ProfileBlock.IDENTIFICATION].fields
        if not f.mandatory
    }
    assert optional == {"social_insurance_number", "expiry_date"}


def test_bank_details_masking_matches_the_sheet():
    transit = schema.field_spec(ProfileBlock.BANK_DETAILS, "transit_number")
    account = schema.field_spec(ProfileBlock.BANK_DETAILS, "account_number")
    assert transit.masking.visible_suffix == 2
    assert account.masking.visible_suffix == 3
    # The "Hidden apart from..." cell is a masking instruction, NOT a visibility
    # trigger — both fields stay always-visible.
    assert transit.visible_when.kind is schema.RuleKind.ALWAYS
    assert account.visible_when.kind is schema.RuleKind.ALWAYS


def test_bank_details_are_staff_or_bank_verification_filled():
    block = schema.BLOCK_REGISTRY[ProfileBlock.BANK_DETAILS]
    assert block.filled_by is schema.FilledBy.STAFF_OR_BANK_VERIFICATION
    assert all(f.mandatory for f in block.fields)
    assert {f.key: f.char_limit for f in block.fields} == {
        "bank_name": 50,
        "institution_number": 4,
        "transit_number": 5,
        "account_holder_name": 50,
        "account_number": 25,
        "account_type": 25,
    }


def test_character_limits_match_the_sheet():
    expected = {
        "personal.first_name": 50,
        "personal.education_details": 75,
        "personal.number_of_dependents": 2,
        "contact.email": 75,
        "contact.main_phone": 10,
        "current_address.street_address": 100,
        "current_address.apartment_unit": 10,
        "current_address.postal_code": 6,
        "current_address.monthly_rent": 10,
        "identification.passport_number": 25,
        "financial.number_of_credit_accounts": 5,
        "primary_income.employer_name": 75,
        "primary_income.income_source": 100,
    }
    for key, limit in expected.items():
        assert schema.FIELD_REGISTRY[key].char_limit == limit, key


def test_the_three_income_blocks_share_one_shape():
    shapes = [
        tuple((f.key, f.field_type, f.format, f.mandatory) for f in
              schema.BLOCK_REGISTRY[b].fields)
        for b in schema.INCOME_BLOCKS
    ]
    assert shapes[0] == shapes[1] == shapes[2]


def test_previous_address_has_no_rent_or_mortgage_but_has_resided_to():
    keys = {f.key for f in schema.BLOCK_REGISTRY[ProfileBlock.PREVIOUS_ADDRESS_1].fields}
    assert "resided_to" in keys
    assert "monthly_rent" not in keys
    assert "monthly_mortgage_payment" not in keys


def test_hire_date_is_modelled_as_a_date_with_the_discrepancy_recorded():
    spec = schema.field_spec(ProfileBlock.PRIMARY_INCOME, "hire_date")
    assert spec.field_type is FieldType.DATE
    assert spec.format is FieldFormat.DATE
    assert spec.sheet_discrepancy is not None


def test_schema_payload_is_json_serializable():
    import json

    payload = schema.schema_payload()
    assert payload["version"] == "1.0"
    assert payload["field_count"] == len(schema.FIELD_REGISTRY)
    json.dumps(payload)  # must not raise


# ---------------------------------------------------------------------------
# Visibility triggers
# ---------------------------------------------------------------------------


def test_country_of_citizenship_only_when_not_canadian():
    spec = schema.field_spec(ProfileBlock.PERSONAL, "country_of_citizenship")
    assert not is_field_visible(spec, {"personal": {"citizenship": "canadian"}})
    assert is_field_visible(spec, {"personal": {"citizenship": "non_resident"}})
    # An unanswered driver must NOT make the dependent field visible.
    assert not is_field_visible(spec, {"personal": {}})


def test_education_details_only_when_other():
    spec = schema.field_spec(ProfileBlock.PERSONAL, "education_details")
    assert is_field_visible(spec, {"personal": {"education": "other"}})
    assert not is_field_visible(spec, {"personal": {"education": "masters_phd"}})


def test_alternative_phone_name_only_for_family_or_friend():
    spec = schema.field_spec(ProfileBlock.CONTACT, "alternative_phone_name")
    for value, visible in (
        ("family_member", True),
        ("friend", True),
        ("work", False),
        ("alt_phone_number", False),
    ):
        assert is_field_visible(spec, {"contact": {"alternative_phone_type": value}}) is visible


def test_mortgage_vs_rent_track_the_residential_status():
    mortgage = schema.field_spec(ProfileBlock.CURRENT_ADDRESS, "monthly_mortgage_payment")
    rent = schema.field_spec(ProfileBlock.CURRENT_ADDRESS, "monthly_rent")
    owning = {"current_address": {"residential_status": "own_townhouse_condo"}}
    renting = {"current_address": {"residential_status": "rent"}}
    with_parents = {"current_address": {"residential_status": "living_with_parents"}}
    assert is_field_visible(mortgage, owning) and not is_field_visible(rent, owning)
    assert is_field_visible(rent, renting) and not is_field_visible(mortgage, renting)
    assert not is_field_visible(rent, with_parents)
    assert not is_field_visible(mortgage, with_parents)


def test_previous_address_appears_only_under_three_years_tenure():
    recent = (date.today() - timedelta(days=365)).isoformat()
    long_ago = (date.today() - timedelta(days=365 * 8)).isoformat()
    assert is_block_visible(
        ProfileBlock.PREVIOUS_ADDRESS_1, {"current_address": {"resided_since": recent}}
    )
    assert not is_block_visible(
        ProfileBlock.PREVIOUS_ADDRESS_1, {"current_address": {"resided_since": long_ago}}
    )
    # No current-address date yet -> no previous-address demand.
    assert not is_block_visible(ProfileBlock.PREVIOUS_ADDRESS_1, {})


def test_income_sub_blocks_track_income_type():
    employer = schema.field_spec(ProfileBlock.PRIMARY_INCOME, "employer_name")
    company = schema.field_spec(ProfileBlock.PRIMARY_INCOME, "company_name")
    noa = schema.field_spec(ProfileBlock.PRIMARY_INCOME, "noa_last_2_years_filed")
    benefit = schema.field_spec(ProfileBlock.PRIMARY_INCOME, "benefit_source")
    verification = schema.field_spec(
        ProfileBlock.PRIMARY_INCOME, "income_verification_phone"
    )
    income_start = schema.field_spec(ProfileBlock.PRIMARY_INCOME, "income_start_date")

    employed = {"primary_income": {"income_type": "employed_part_time"}}
    self_emp = {"primary_income": {"income_type": "self_employed"}}
    pension = {"primary_income": {"income_type": "pension_investment"}}
    disability = {"primary_income": {"income_type": "disability_insurance"}}
    other = {"primary_income": {"income_type": "other"}}

    assert is_field_visible(employer, employed)
    assert not is_field_visible(employer, self_emp)
    assert is_field_visible(company, self_emp) and is_field_visible(noa, self_emp)
    assert is_field_visible(benefit, disability)
    assert not is_field_visible(benefit, pension)
    # Income start date / source are shared by Pension/Investment AND Other.
    assert is_field_visible(income_start, pension)
    assert is_field_visible(income_start, other)
    assert not is_field_visible(income_start, employed)
    # Income verification phone is Other-only.
    assert is_field_visible(verification, other)
    assert not is_field_visible(verification, pension)


def test_additional_income_blocks_are_gated_on_being_added():
    assert not is_block_visible(ProfileBlock.ADDITIONAL_INCOME_1, {})
    assert is_block_visible(
        ProfileBlock.ADDITIONAL_INCOME_1,
        {"additional_income_1": {"income_type": "employed_full_time"}},
    )


def test_instance_key_round_trips():
    assert instance_key(ProfileBlock.BANK_DETAILS) == "bank_details"
    assert instance_key(ProfileBlock.BANK_DETAILS, 2) == "bank_details#2"
    assert parse_instance_key("bank_details#2") == ("bank_details", 2)
    assert parse_instance_key("personal") == ("personal", 0)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _codes(issues):
    return {i.code for i in issues}


def test_char_limit_is_enforced():
    issues = validate_profile(
        {"personal": {"first_name": "A" * 51}}, partial=True
    )
    assert ErrorCode.TOO_LONG in _codes(issues)


def test_enum_membership_is_enforced():
    issues = validate_profile({"personal": {"sex": "Male"}}, partial=True)
    assert ErrorCode.INVALID_OPTION in _codes(issues)
    assert not validate_profile({"personal": {"sex": "male"}}, partial=True)


@pytest.mark.parametrize(
    "block,field,value,valid",
    [
        ("personal", "first_name", "Jean-Luc", True),
        ("personal", "first_name", "R2D2", False),          # alpha only
        ("current_address", "postal_code", "V1Y9K3", True),
        ("current_address", "postal_code", "99999", False),
        ("contact", "email", "a@b.co", True),
        ("contact", "email", "not-an-email", False),
        ("contact", "main_phone", "(250) 555-1234", True),  # digits after strip
        ("contact", "main_phone", "555-CALL", False),
        ("financial", "monthly_credit_payments", "1250.75", True),
        ("financial", "monthly_credit_payments", "-5", False),
        ("financial", "monthly_credit_payments", "12.345", False),
        ("identification", "social_insurance_number", "123456789", True),
        ("identification", "social_insurance_number", "12345", False),
        ("personal", "date_of_birth", "1990-04-01", True),
        ("personal", "date_of_birth", "04-01-1990", False),  # stored ISO
    ],
)
def test_formats(block, field, value, valid):
    issues = validate_profile({block: {field: value}}, partial=True)
    format_issues = [i for i in issues if i.code != ErrorCode.NOT_VISIBLE]
    assert (not format_issues) is valid, format_issues


def test_a_value_for_an_invisible_field_is_rejected():
    issues = validate_profile(
        {"personal": {"citizenship": "canadian", "country_of_citizenship": "US"}},
        partial=True,
    )
    assert ErrorCode.NOT_VISIBLE in _codes(issues)


def test_unknown_field_and_block_are_rejected():
    assert ErrorCode.UNKNOWN_FIELD in _codes(
        validate_profile({"personal": {"favourite_colour": "teal"}}, partial=True)
    )
    assert ErrorCode.UNKNOWN_BLOCK in _codes(
        validate_profile({"pets": {"name": "Rex"}}, partial=True)
    )


def test_only_bank_details_repeats():
    assert not _codes(validate_profile({"bank_details#1": {"bank_name": "RBC"}}, partial=True))
    assert ErrorCode.NOT_REPEATABLE in _codes(
        validate_profile({"personal#1": {"first_name": "Ann"}}, partial=True)
    )


def test_borrowers_cannot_write_bank_details():
    issues = validate_profile(
        {"bank_details": {"bank_name": "RBC"}}, partial=True, allow_staff_fields=False
    )
    assert ErrorCode.READ_ONLY in _codes(issues)


def test_invisible_mandatory_fields_are_never_required():
    """The load-bearing rule. A Canadian citizen is never asked for a passport #."""
    values = _minimal_complete_profile()
    issues = validate_profile(values, partial=False)
    required = {f"{i.block}.{i.field}" for i in issues if i.code == ErrorCode.REQUIRED}
    assert "identification.passport_number" not in required
    assert "personal.country_of_citizenship" not in required
    assert "previous_address_1.street_address" not in required
    assert "additional_income_1.income_type" not in required
    assert issues == [], issues


def test_missing_mandatory_visible_field_is_required():
    values = _minimal_complete_profile()
    del values["personal"]["last_name"]
    issues = validate_profile(values, partial=False)
    assert any(
        i.code == ErrorCode.REQUIRED and i.field == "last_name" for i in issues
    )
    # ... but a PATCH of an unrelated field must not trip over it.
    assert not [
        i for i in validate_profile(values, partial=True) if i.code == ErrorCode.REQUIRED
    ]


def test_switching_to_self_employed_demands_the_self_employed_fields():
    values = _minimal_complete_profile()
    values["primary_income"] = {
        "income_type": "self_employed",
        "net_monthly_income": "5000.00",
        "next_pay_date": (date.today() + timedelta(days=7)).isoformat(),
        "pay_frequency": "monthly",
    }
    required = {
        i.field for i in validate_profile(values, partial=False)
        if i.code == ErrorCode.REQUIRED
    }
    assert {"company_name", "company_founded_date", "company_phone",
            "noa_last_2_years_filed"} <= required
    assert "employer_name" not in required


def test_completeness_reports_progress():
    partial_values = {"personal": {"first_name": "Ann"}}
    result = completeness(partial_values)
    assert result["is_complete"] is False
    assert result["filled_count"] == 1
    assert result["percent"] < 100
    assert completeness(_minimal_complete_profile())["is_complete"] is True


# ---------------------------------------------------------------------------
# Fixture: the smallest profile that satisfies every VISIBLE mandatory field
# ---------------------------------------------------------------------------


def _minimal_complete_profile() -> dict:
    """A Canadian, employed full-time, renting, resident >3 years, driver's licence.

    Deliberately picks the branch of every trigger that keeps the conditional
    blocks CLOSED, so the test above proves invisible fields are not required.
    """
    long_ago = (date.today() - timedelta(days=365 * 6)).isoformat()
    return {
        "personal": {
            "first_name": "Ann",
            "middle_name": "Marie",
            "last_name": "Tremblay",
            "date_of_birth": "1988-03-14",
            "sex": "female",
            "citizenship": "canadian",
            "education": "college_university",
            "marital_status": "single",
            "number_of_dependents": "0",
        },
        "contact": {
            "email": "ann@example.ca",
            "main_phone": "2505551234",
            "alternative_phone": "2505559876",
            "alternative_phone_type": "work",
        },
        "current_address": {
            "street_address": "1200 Water Street",
            "apartment_unit": "402",
            "city": "Kelowna",
            "province": "BC",
            "postal_code": "V1Y9K3",
            "residential_status": "rent",
            "resided_since": long_ago,
            "monthly_rent": "1850.00",
        },
        "identification": {
            "id_type": "drivers_license",
            "drivers_license_number": "1234567",
            "province_of_issue": "BC",
        },
        "financial": {
            "car_owner": "no",
            "number_of_credit_accounts": "3",
            "monthly_credit_payments": "310.00",
            "other_monthly_expenses": "420.00",
        },
        "primary_income": {
            "income_type": "employed_full_time",
            "net_monthly_income": "4800.00",
            "next_pay_date": (date.today() + timedelta(days=10)).isoformat(),
            "pay_frequency": "bi_weekly",
            "employer_name": "Okanagan Dental Group",
            "job_title": "Office Manager",
            "hire_date": "2019-06-03",
            "work_phone": "2505550000",
        },
        "bank_details": {
            "bank_name": "RBC",
            "institution_number": "0003",
            "transit_number": "05012",
            "account_holder_name": "Ann Marie Tremblay",
            "account_number": "1234567",
            "account_type": "Chequing",
        },
    }
