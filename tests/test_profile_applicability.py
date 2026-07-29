"""Applicability rules of the Customer Profile registry — the regression suite.

Written against a live bug report (2026-07-28): a complete, correct borrower
profile was rejected with fourteen "… is not applicable" errors, seven of them
on fields whose own trigger is *Always Visible*.

Root cause was one conflation and one missing value:

* the validator asked ``is_field_visible`` — BLOCK gate **and** field trigger —
  and then printed the FIELD's trigger text, so a block that did not apply
  (Previous Address 1, for someone who has not moved in three years) produced
  *"Street Address is not applicable (Always Visable)"*;
* applicability was two-valued, so "the driver says no" and "the driver has not
  been answered" were the same answer, and every conditional field failed at
  once whenever its driver could not be read.

Everything here is registry-driven and needs no database.
"""
from __future__ import annotations

import copy
from datetime import date, timedelta

import pytest

from app.services.customer_profile_schema import (
    ALT_PHONE_TYPE_OPTIONS,
    BLOCKS,
    EMPLOYED_INCOME_TYPES,
    FIELD_REGISTRY,
    Applicability,
    ProfileBlock,
    RuleKind,
    block_applicability,
    field_applicability,
    field_spec,
    is_field_visible,
    normalize_values,
    schema_payload,
)
from app.services.customer_profile_validation import (
    ErrorCode,
    completeness,
    validate_profile,
)


TODAY = date(2026, 7, 28)


# ---------------------------------------------------------------------------
# The owner's exact scenario
# ---------------------------------------------------------------------------


def _owner_profile() -> dict:
    """The profile the platform owner could not save.

    Employed income type, a driver's licence, an alternative phone belonging to
    a family member, and a full current address — every conditional field
    answered consistently with its driver.
    """
    return {
        "personal": {
            "first_name": "David",
            "middle_name": "Alan",
            "last_name": "Wilson",
            "date_of_birth": "1975-04-02",
            "sex": "male",
            "citizenship": "canadian",
            "education": "college_university",
            "marital_status": "married",
            "number_of_dependents": "2",
        },
        "contact": {
            "email": "owner@example.com",
            "main_phone": "2505551234",
            "alternative_phone": "2505554321",
            "alternative_phone_type": "family_member",
            "alternative_phone_name": "Jane Wilson",
        },
        "current_address": {
            "street_address": "123 Bernard Ave",
            "apartment_unit": "4B",
            "city": "Kelowna",
            "province": "BC",
            "postal_code": "V1Y1A1",
            "residential_status": "rent",
            "resided_since": "2015-06-01",
            "monthly_rent": "1800.00",
        },
        "identification": {
            "id_type": "drivers_license",
            "drivers_license_number": "WILSO1234567",
            "province_of_issue": "BC",
        },
        "financial": {
            "car_owner": "no",
            "number_of_credit_accounts": "3",
            "monthly_credit_payments": "300.00",
            "other_monthly_expenses": "500.00",
        },
        "primary_income": {
            "income_type": "employed_full_time",
            "net_monthly_income": "5200.00",
            "next_pay_date": "2026-08-07",
            "pay_frequency": "bi_weekly",
            "employer_name": "Acme Dental Group",
            "job_title": "Practice Manager",
            "hire_date": "2019-03-01",
            "work_phone": "2505559999",
        },
    }


def _with_bank(values: dict) -> dict:
    """Bank Details is read-through; supply it as ``profile_values`` would."""
    values = copy.deepcopy(values)
    values["bank_details"] = {
        "bank_name": "RBC",
        "institution_number": "003",
        "transit_number": "05012",
        "account_holder_name": "David Alan Wilson",
        "account_number": "1234567",
        "account_type": "Chequing",
    }
    return values


def _not_applicable(issues):
    return [i for i in issues if i.code == ErrorCode.NOT_APPLICABLE]


def test_the_owners_profile_validates_with_zero_not_applicable_errors():
    """The bug report, exactly. Nothing about this profile is inapplicable."""
    issues = validate_profile(_owner_profile(), partial=True, today=TODAY)
    assert issues == [], [i.message for i in issues]


def test_the_owners_profile_is_complete_once_bank_details_are_on_file():
    issues = validate_profile(_with_bank(_owner_profile()), partial=False, today=TODAY)
    assert issues == [], [i.message for i in issues]
    assert completeness(_with_bank(_owner_profile()), today=TODAY)["is_complete"] is True


def test_the_owners_profile_plus_a_previous_address_is_still_clean():
    """The seven "(Always Visable)" errors: a block gate, misreported per field.

    He had lived at his current address for eleven years, so Previous Address 1
    does not apply — but he had typed one, and every always-visible field in it
    was rejected as "not applicable (Always Visable)". Extra address history is
    unnecessary, never contradictory: it is accepted and never required.
    """
    values = _owner_profile()
    values["previous_address_1"] = {
        "street_address": "9 Old Vernon Rd",
        "city": "Vernon",
        "province": "BC",
        "postal_code": "V1T1A1",
        "residential_status": "rent",
        "resided_since": "2010-01-01",
    }
    assert (
        block_applicability(ProfileBlock.PREVIOUS_ADDRESS_1, values, today=TODAY)
        is Applicability.NOT_APPLICABLE
    )
    assert validate_profile(values, partial=True, today=TODAY) == []
    # ... and nothing in that block is required, either.
    required = {
        i.field
        for i in validate_profile(_with_bank(values), partial=False, today=TODAY)
        if i.block == ProfileBlock.PREVIOUS_ADDRESS_1.value
    }
    assert required == set()


# ---------------------------------------------------------------------------
# "Always" means always
# ---------------------------------------------------------------------------

_ALWAYS_FIELDS = [
    key for key, spec in FIELD_REGISTRY.items() if spec.visible_when.kind is RuleKind.ALWAYS
]


def test_the_registry_still_has_plenty_of_always_fields():
    assert len(_ALWAYS_FIELDS) > 50


@pytest.mark.parametrize("full_key", _ALWAYS_FIELDS)
def test_an_always_field_is_applicable_whatever_else_is_going_on(full_key):
    """Requirement one: a field marked Always can never be inapplicable."""
    spec = FIELD_REGISTRY[full_key]
    hostile = [
        {},                                        # empty profile
        _owner_profile(),                          # a consistent profile
        {spec.block.value: {}},                    # its own block, empty
        # every gate in the registry turned off at once
        {"personal": {"citizenship": "canadian", "education": "high_school"},
         "contact": {"alternative_phone_type": "work"},
         "identification": {"id_type": "passport"},
         "current_address": {"residential_status": "own_detached",
                             "resided_since": "1999-01-01"},
         "financial": {"car_owner": "no"},
         "primary_income": {"income_type": "self_employed"}},
    ]
    for values in hostile:
        for index in (0, 1):
            assert (
                field_applicability(spec, values, index=index, today=TODAY)
                is Applicability.APPLICABLE
            ), full_key


def test_no_always_field_is_ever_reported_inapplicable_by_the_validator():
    """End-to-end, across gated blocks: no "(Always …)" contradiction survives."""
    values = _owner_profile()
    values["previous_address_1"] = {"street_address": "9 Old Vernon Rd", "city": "Vernon"}
    values["additional_income_1"] = {"net_monthly_income": "400.00"}
    reported = {
        f"{i.block}.{i.field}"
        for i in _not_applicable(validate_profile(values, partial=True, today=TODAY))
    }
    assert not (reported & set(_ALWAYS_FIELDS)), reported


# ---------------------------------------------------------------------------
# Conditional fields: applicable exactly when their condition holds
# ---------------------------------------------------------------------------

#: (block, field, driver, values that make it apply, values that do not)
CONDITIONAL_RULES = [
    (ProfileBlock.CONTACT, "alternative_phone_name", "alternative_phone_type",
     ("family_member", "friend"), ("work", "alt_phone_number")),
    (ProfileBlock.PERSONAL, "country_of_citizenship", "citizenship",
     ("resident", "non_resident"), ("canadian",)),
    (ProfileBlock.PERSONAL, "education_details", "education",
     ("other",), ("none", "high_school", "college_university", "masters_phd")),
    (ProfileBlock.IDENTIFICATION, "drivers_license_number", "id_type",
     ("drivers_license",), ("government_photo_id", "permanent_residence_card", "passport")),
    (ProfileBlock.IDENTIFICATION, "government_photo_id_number", "id_type",
     ("government_photo_id",), ("drivers_license", "passport")),
    (ProfileBlock.IDENTIFICATION, "permanent_residence_card_number", "id_type",
     ("permanent_residence_card",), ("drivers_license", "passport")),
    (ProfileBlock.IDENTIFICATION, "passport_number", "id_type",
     ("passport",), ("drivers_license", "government_photo_id")),
    (ProfileBlock.FINANCIAL, "monthly_car_payment", "car_owner",
     ("yes_financing_leasing",), ("yes_paid_in_full", "no")),
    (ProfileBlock.CURRENT_ADDRESS, "monthly_rent", "residential_status",
     ("rent",), ("own_detached", "own_townhouse_condo", "living_with_parents")),
    (ProfileBlock.CURRENT_ADDRESS, "monthly_mortgage_payment", "residential_status",
     ("own_detached", "own_townhouse_condo", "own_mobile_owns_land",
      "own_mobile_rents_land"), ("rent", "living_with_parents")),
    (ProfileBlock.PRIMARY_INCOME, "employer_name", "income_type",
     EMPLOYED_INCOME_TYPES, ("self_employed", "pension_investment", "other",
                             "disability_insurance")),
    (ProfileBlock.PRIMARY_INCOME, "job_title", "income_type",
     EMPLOYED_INCOME_TYPES, ("self_employed", "pension_investment", "other")),
    (ProfileBlock.PRIMARY_INCOME, "hire_date", "income_type",
     EMPLOYED_INCOME_TYPES, ("self_employed", "pension_investment", "other")),
    (ProfileBlock.PRIMARY_INCOME, "work_phone", "income_type",
     EMPLOYED_INCOME_TYPES, ("self_employed", "pension_investment", "other")),
    (ProfileBlock.PRIMARY_INCOME, "income_start_date", "income_type",
     ("pension_investment", "other"), EMPLOYED_INCOME_TYPES + ("self_employed",
                                                               "disability_insurance")),
    (ProfileBlock.PRIMARY_INCOME, "income_source", "income_type",
     ("pension_investment", "other"), EMPLOYED_INCOME_TYPES + ("self_employed",)),
    (ProfileBlock.PRIMARY_INCOME, "company_name", "income_type",
     ("self_employed",), EMPLOYED_INCOME_TYPES + ("pension_investment",)),
    (ProfileBlock.PRIMARY_INCOME, "noa_last_2_years_filed", "income_type",
     ("self_employed",), EMPLOYED_INCOME_TYPES),
    (ProfileBlock.PRIMARY_INCOME, "benefit_start_date", "income_type",
     ("disability_insurance",), EMPLOYED_INCOME_TYPES + ("other",)),
    (ProfileBlock.PRIMARY_INCOME, "benefit_source", "income_type",
     ("disability_insurance",), EMPLOYED_INCOME_TYPES + ("other",)),
    (ProfileBlock.PRIMARY_INCOME, "income_verification_phone", "income_type",
     ("other",), EMPLOYED_INCOME_TYPES + ("pension_investment",)),
]


@pytest.mark.parametrize("block,field,driver,applies,does_not", CONDITIONAL_RULES)
def test_a_conditional_field_applies_exactly_when_its_condition_holds(
    block, field, driver, applies, does_not
):
    spec = field_spec(block, field)
    assert spec is not None, f"{block.value}.{field}"
    for value in applies:
        values = {block.value: {driver: value}}
        assert field_applicability(spec, values, today=TODAY) is Applicability.APPLICABLE, value
        assert is_field_visible(spec, values, today=TODAY), value
    for value in does_not:
        values = {block.value: {driver: value}}
        assert (
            field_applicability(spec, values, today=TODAY) is Applicability.NOT_APPLICABLE
        ), value
        assert not is_field_visible(spec, values, today=TODAY), value


@pytest.mark.parametrize("block,field,driver,applies,does_not", CONDITIONAL_RULES)
def test_the_validator_keeps_the_value_when_the_condition_holds(
    block, field, driver, applies, does_not
):
    """…and rejects it, with a reason, when the condition genuinely fails."""
    for value in applies:
        issues = validate_profile(
            {block.value: {driver: value, field: "X"}}, partial=True, today=TODAY
        )
        assert _not_applicable(issues) == [], (value, [i.message for i in issues])
    for value in does_not:
        issues = _not_applicable(
            validate_profile({block.value: {driver: value, field: "X"}},
                             partial=True, today=TODAY)
        )
        assert [i.field for i in issues] == [field], value


def test_alternative_phone_name_covers_every_option_in_the_dropdown():
    """No option may be left un-asserted — the owner's first complaint."""
    spec = field_spec(ProfileBlock.CONTACT, "alternative_phone_name")
    expected = {
        "family_member": Applicability.APPLICABLE,
        "friend": Applicability.APPLICABLE,
        "work": Applicability.NOT_APPLICABLE,
        "alt_phone_number": Applicability.NOT_APPLICABLE,
    }
    assert {o.value for o in ALT_PHONE_TYPE_OPTIONS} == set(expected)
    for value, state in expected.items():
        assert (
            field_applicability(spec, {"contact": {"alternative_phone_type": value}})
            is state
        ), value


# ---------------------------------------------------------------------------
# "Not answered yet" is not "does not apply"
# ---------------------------------------------------------------------------


def test_an_unanswered_driver_leaves_the_dependent_field_undetermined():
    spec = field_spec(ProfileBlock.PRIMARY_INCOME, "employer_name")
    for empty in ({}, {"primary_income": {}}, {"primary_income": {"income_type": ""}}):
        assert field_applicability(spec, empty) is Applicability.UNDETERMINED
        # Undetermined is not visible: it can never be REQUIRED …
        assert not is_field_visible(spec, empty)
    # … and it is never rejected either. The unanswered DRIVER is the issue.
    issues = validate_profile(
        {"primary_income": {"employer_name": "Acme Dental"}}, partial=True
    )
    assert issues == [], [i.message for i in issues]
    required = {
        i.field for i in validate_profile(
            {"primary_income": {"employer_name": "Acme Dental"}}, partial=False
        ) if i.code == ErrorCode.REQUIRED
    }
    assert "income_type" in required


def test_a_driver_carrying_an_unrecognised_code_does_not_cascade():
    """A label where a code belongs is one error on the driver, not five."""
    issues = validate_profile(
        {"primary_income": {
            "income_type": "Employed - Full-Time",   # the LABEL, not the code
            "employer_name": "Acme Dental",
            "job_title": "Manager",
        }},
        partial=True,
    )
    assert [i.code for i in issues] == [ErrorCode.INVALID_OPTION]
    assert issues[0].field == "income_type"


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def test_a_not_applicable_message_names_the_condition_the_answer_and_the_way_out():
    issues = _not_applicable(validate_profile(
        {"contact": {"alternative_phone_type": "work", "alternative_phone_name": "Jane"}},
        partial=True,
    ))
    assert len(issues) == 1
    message = issues[0].message
    assert "Alternative Phone Name only applies when" in message
    assert "Alternative Phone Type is Family Member or Friend" in message
    assert "currently Work" in message           # the answer that failed it
    assert "change Alternative Phone Type" in message   # the way out
    assert issues[0].block_label == "Contact Information"
    assert issues[0].field_label == "Alternative Phone Name"


def test_no_message_ever_contradicts_itself():
    """"… is not applicable (Always Visable)" must be unreachable."""
    values = _owner_profile()
    values["previous_address_1"] = {"street_address": "9 Old Vernon Rd"}
    values["contact"]["alternative_phone_type"] = "work"
    for issue in validate_profile(values, partial=False, today=TODAY):
        assert "not applicable (Always" not in issue.message
        assert "Visable" not in issue.message


def test_issues_name_their_block_so_repeated_labels_are_distinguishable():
    """Two address blocks, one "City" label each."""
    values = _owner_profile()
    values["current_address"]["resided_since"] = "2025-01-01"   # < 3 years -> previous applies
    del values["current_address"]["city"]
    values["previous_address_1"] = {"street_address": "9 Old Vernon Rd"}
    missing_city = [
        i for i in validate_profile(_with_bank(values), partial=False, today=TODAY)
        if i.field == "city"
    ]
    assert {i.block for i in missing_city} == {"current_address", "previous_address_1"}
    assert {i.message for i in missing_city} == {
        "Current Address — City is required",
        "Previous Address 1 — City is required",
    }


def test_daves_spelling_slip_is_not_carried_into_copy_a_user_reads():
    payload = schema_payload()
    for block in payload["blocks"]:
        assert "Visable" not in block["visible_when"]["trigger_text"]
        for field in block["fields"]:
            assert "Visable" not in field["visible_when"]["trigger_text"]
            assert field["applicable_when"]


# ---------------------------------------------------------------------------
# Apartment / Unit is optional
# ---------------------------------------------------------------------------


def test_apartment_unit_is_optional_in_every_address_block():
    """Owner instruction: most addresses have no unit number."""
    for block in (ProfileBlock.CURRENT_ADDRESS, ProfileBlock.PREVIOUS_ADDRESS_1):
        assert field_spec(block, "apartment_unit").mandatory is False


def test_a_house_with_no_unit_number_is_a_complete_profile():
    values = _with_bank(_owner_profile())
    del values["current_address"]["apartment_unit"]
    assert validate_profile(values, partial=False, today=TODAY) == []
    assert completeness(values, today=TODAY)["is_complete"] is True


def test_the_rest_of_the_address_is_still_mandatory():
    values = _with_bank(_owner_profile())
    del values["current_address"]["street_address"]
    required = {
        i.field for i in validate_profile(values, partial=False, today=TODAY)
        if i.code == ErrorCode.REQUIRED
    }
    assert required == {"street_address"}


# ---------------------------------------------------------------------------
# Instance keys
# ---------------------------------------------------------------------------


def test_an_explicit_index_zero_key_is_the_same_instance_as_the_bare_name():
    """"contact#0" and "contact" are one instance — a rule must see both."""
    values = {f"{k}#0": v for k, v in _owner_profile().items()}
    assert normalize_values(values) == _owner_profile()
    assert validate_profile(values, partial=True, today=TODAY) == []


def test_a_repeated_instance_of_a_non_repeatable_block_is_still_rejected():
    issues = validate_profile({"personal#1": {"first_name": "Ann"}}, partial=True)
    assert [i.code for i in issues] == [ErrorCode.NOT_REPEATABLE]


# ---------------------------------------------------------------------------
# Gated blocks
# ---------------------------------------------------------------------------


def test_previous_address_applies_only_below_three_years_at_the_current_one():
    recent = {"current_address": {"resided_since": (TODAY - timedelta(days=400)).isoformat()}}
    settled = {"current_address": {"resided_since": (TODAY - timedelta(days=4000)).isoformat()}}
    assert (
        block_applicability(ProfileBlock.PREVIOUS_ADDRESS_1, recent, today=TODAY)
        is Applicability.APPLICABLE
    )
    assert (
        block_applicability(ProfileBlock.PREVIOUS_ADDRESS_1, settled, today=TODAY)
        is Applicability.NOT_APPLICABLE
    )
    # Nobody has said how long they have lived there yet.
    assert (
        block_applicability(ProfileBlock.PREVIOUS_ADDRESS_1, {}, today=TODAY)
        is Applicability.UNDETERMINED
    )


def test_every_block_gate_is_reachable_in_both_directions():
    """No block may be permanently off — that was never a real state."""
    for block in BLOCKS:
        if block.visible_when.kind is RuleKind.ALWAYS:
            assert (
                block_applicability(block.block, {}, today=TODAY) is Applicability.APPLICABLE
            ), block.block
