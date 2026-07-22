"""DB-backed tests for the Customer Profile service.

Covers Dave's mandates end-to-end: one profile -> many applications, edits create
a new version (the prior becomes "former", never overwritten), lock/soft-delete,
bank-details masking, the frozen decision snapshot, and the backfill path for
applications captured before profiles existed.
"""
from datetime import date, timedelta
from uuid import uuid4

import pytest

from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.borrower_portal import PlatformPatientBankAccount
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.customer_profile import PlatformCustomerProfileField
from app.models.platform.event import PlatformEvent
from app.models.platform.patient import PlatformPatient
from app.services import customer_profile as profiles
from app.services.customer_profile_validation import ProfileValidationError


@pytest.fixture
def patient(db_session):
    p = PlatformPatient(id=uuid4(), legal_first_name="Ann", legal_last_name="Tremblay")
    db_session.add(p)
    db_session.commit()
    return p


@pytest.fixture
def product(db_session):
    existing = db_session.query(PlatformCreditProduct).first()
    if existing:
        return existing
    pytest.skip("no credit product seeded in the test database")


def _base_values() -> dict:
    return {
        "personal": {
            "first_name": "Ann",
            "last_name": "Tremblay",
            "date_of_birth": "1988-03-14",
            "citizenship": "canadian",
            "education": "college_university",
        },
        "contact": {"email": "ann@example.ca", "main_phone": "2505551234"},
        "current_address": {
            "street_address": "1200 Water Street",
            "city": "Kelowna",
            "province": "BC",
            "postal_code": "V1Y9K3",
            "residential_status": "rent",
            "resided_since": (date.today() - timedelta(days=365 * 6)).isoformat(),
            "monthly_rent": "1850.00",
        },
        "primary_income": {
            "income_type": "employed_full_time",
            "net_monthly_income": "4800.00",
            "employer_name": "Okanagan Dental Group",
        },
    }


# ---------------------------------------------------------------------------
# Create / read
# ---------------------------------------------------------------------------


def test_create_profile_writes_version_1_and_audits(db_session, patient):
    profile = profiles.create_profile(
        db_session, patient_id=patient.id, values=_base_values(), actor="staff-1"
    )
    assert profile.version == 1
    assert profile.schema_version == "1.0"

    rows = profiles.current_fields(db_session, profile.id)
    assert all(r.profile_version == 1 and r.created_by == "staff-1" for r in rows)

    event = (
        db_session.query(PlatformEvent)
        .filter(PlatformEvent.event_type == "customer_profile.created")
        .filter(PlatformEvent.patient_id == patient.id)
        .one()
    )
    # Hard Rule #6: never a field VALUE in the event payload.
    assert "Tremblay" not in str(event.payload)
    assert event.payload["field_count"] > 0


def test_one_profile_per_patient(db_session, patient):
    profiles.create_profile(
        db_session, patient_id=patient.id, values=_base_values(), actor="a"
    )
    with pytest.raises(profiles.ProfileError):
        profiles.create_profile(
            db_session, patient_id=patient.id, values=_base_values(), actor="a"
        )


def test_create_rejects_invalid_values(db_session, patient):
    with pytest.raises(ProfileValidationError):
        profiles.create_profile(
            db_session,
            patient_id=patient.id,
            values={"personal": {"first_name": "A" * 99}},
            actor="a",
        )


def test_identity_columns_stay_in_step_with_the_profile(db_session, patient):
    profile = profiles.create_profile(
        db_session, patient_id=patient.id, values=_base_values(), actor="a"
    )
    db_session.refresh(patient)
    assert patient.email == "ann@example.ca"
    assert patient.dob == date(1988, 3, 14)

    profiles.update_profile(
        db_session, profile.id, values={"contact": {"email": "new@example.ca"}}, actor="a"
    )
    db_session.refresh(patient)
    assert patient.email == "new@example.ca"


# ---------------------------------------------------------------------------
# Versioning — Dave's "never overwrite" mandate
# ---------------------------------------------------------------------------


def test_edit_supersedes_rather_than_overwrites(db_session, patient):
    profile = profiles.create_profile(
        db_session, patient_id=patient.id, values=_base_values(), actor="staff-1"
    )
    profiles.update_profile(
        db_session,
        profile.id,
        values={"current_address": {"street_address": "77 Bernard Avenue"}},
        actor="staff-2",
    )
    db_session.refresh(profile)
    assert profile.version == 2

    rows = (
        db_session.query(PlatformCustomerProfileField)
        .filter(
            PlatformCustomerProfileField.profile_id == profile.id,
            PlatformCustomerProfileField.field_key == "street_address",
        )
        .all()
    )
    assert len(rows) == 2
    former = next(r for r in rows if not r.is_current)
    current = next(r for r in rows if r.is_current)
    assert former.value == "1200 Water Street"      # prior value preserved
    assert current.value == "77 Bernard Avenue"
    assert former.superseded_at is not None
    assert former.superseded_by_id == current.id
    assert current.profile_version == 2
    assert current.created_by == "staff-2"


def test_history_is_the_changelog(db_session, patient):
    profile = profiles.create_profile(
        db_session, patient_id=patient.id, values=_base_values(), actor="staff-1"
    )
    profiles.update_profile(
        db_session, profile.id, values={"personal": {"first_name": "Anne"}}, actor="staff-2"
    )
    entries = profiles.field_history(db_session, profile.id, field_key="first_name")
    assert [e["value"] for e in entries] == ["Anne", "Ann"]
    assert {e["changed_by"] for e in entries} == {"staff-1", "staff-2"}
    assert all(e["changed_at"] is not None for e in entries)


def test_a_no_op_edit_does_not_manufacture_a_version_row(db_session, patient):
    profile = profiles.create_profile(
        db_session, patient_id=patient.id, values=_base_values(), actor="a"
    )
    profiles.update_profile(
        db_session, profile.id, values={"personal": {"first_name": "Ann"}}, actor="b"
    )
    rows = (
        db_session.query(PlatformCustomerProfileField)
        .filter(
            PlatformCustomerProfileField.profile_id == profile.id,
            PlatformCustomerProfileField.field_key == "first_name",
        )
        .count()
    )
    assert rows == 1


def test_edit_validates_against_the_merged_state(db_session, patient):
    """A trigger satisfied by a value the caller is not resending must still hold."""
    profile = profiles.create_profile(
        db_session,
        patient_id=patient.id,
        values={"personal": {"citizenship": "non_resident"}},
        actor="a",
    )
    # citizenship is already non_resident, so country_of_citizenship IS visible
    # even though this PATCH does not resend citizenship.
    profiles.update_profile(
        db_session,
        profile.id,
        values={"personal": {"country_of_citizenship": "FR"}},
        actor="a",
    )
    assert profiles.profile_values(db_session, profile.id)["personal"][
        "country_of_citizenship"
    ] == "FR"


# ---------------------------------------------------------------------------
# Lock / delete
# ---------------------------------------------------------------------------


def test_locked_profile_rejects_edits(db_session, patient):
    profile = profiles.create_profile(
        db_session, patient_id=patient.id, values=_base_values(), actor="a"
    )
    profiles.lock_profile(db_session, profile.id, actor="admin", reason="under review")
    with pytest.raises(profiles.ProfileLockedError):
        profiles.update_profile(
            db_session, profile.id, values={"personal": {"first_name": "Zoe"}}, actor="a"
        )
    profiles.unlock_profile(db_session, profile.id, actor="admin")
    profiles.update_profile(
        db_session, profile.id, values={"personal": {"first_name": "Zoe"}}, actor="a"
    )


def test_delete_is_soft_and_keeps_history(db_session, patient):
    profile = profiles.create_profile(
        db_session, patient_id=patient.id, values=_base_values(), actor="a"
    )
    profiles.delete_profile(db_session, profile.id, actor="admin", reason="duplicate")
    assert profile.deleted_at is not None
    with pytest.raises(profiles.ProfileError):
        profiles.get_profile(db_session, profile.id)
    assert profiles.current_fields(db_session, profile.id)  # history retained

    profiles.restore_profile(db_session, profile.id, actor="admin")
    assert profiles.get_profile(db_session, profile.id) is not None


# ---------------------------------------------------------------------------
# Masking + SIN
# ---------------------------------------------------------------------------


def _add_bank_account(db_session, patient, **overrides):
    """Create a row in the table that OWNS bank accounts (064/069)."""
    row = PlatformPatientBankAccount(
        id=uuid4(),
        patient_id=patient.id,
        institution_name=overrides.get("institution_name", "RBC"),
        account_mask=overrides.get("account_mask", "\u2022\u2022\u2022\u2022\u2022\u2022\u2022890"),
        routing_mask=overrides.get("routing_mask", "\u2022\u2022\u202212"),
        institution_number=overrides.get("institution_number", "003"),
        transit_number=overrides.get("transit_number", "05012"),
        account_holder=overrides.get("account_holder", "Ann Tremblay"),
        account_type=overrides.get("account_type", "Chequing"),
        source=overrides.get("source", "manual"),
        verified_via=overrides.get("verified_via", "manual_staff"),
        is_default=overrides.get("is_default", False),
        status="active",
        added_by="staff-1",
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_bank_details_read_through_to_the_owning_table(db_session, patient):
    """No parallel copy: the block is populated from platform_patient_bank_accounts."""
    profile = profiles.create_profile(
        db_session, patient_id=patient.id, values=_base_values(), actor="staff"
    )
    _add_bank_account(db_session, patient)

    read = profiles.read_profile(db_session, profile.id)
    bank = next(b for b in read["blocks"] if b["block"] == "bank_details")
    assert bank["read_through"] is True
    assert bank["owned_by"]
    assert bank["values"]["bank_name"] == "RBC"
    assert bank["values"]["institution_number"] == "003"
    # The account number is the owning table's mask — the full number lives
    # there Fernet-encrypted and is never decrypted for display.
    assert bank["values"]["account_number"].endswith("890")
    assert "1234567890" not in str(read)

    # Nothing was copied into profile field storage.
    assert not [
        r for r in profiles.current_fields(db_session, profile.id)
        if r.block == "bank_details"
    ]


def test_bank_details_writes_are_rejected_with_a_pointer_to_the_owner(
    db_session, patient
):
    values = _base_values()
    values["bank_details"] = {"bank_name": "RBC", "account_number": "1234567890"}
    with pytest.raises(ProfileValidationError) as excinfo:
        profiles.create_profile(
            db_session, patient_id=patient.id, values=values, actor="staff"
        )
    assert any(
        "platform_patient_bank_accounts" in i.message for i in excinfo.value.issues
    )


def test_bank_details_edits_are_rejected_too(db_session, patient):
    profile = profiles.create_profile(
        db_session, patient_id=patient.id, values=_base_values(), actor="staff"
    )
    with pytest.raises(ProfileValidationError):
        profiles.update_profile(
            db_session,
            profile.id,
            values={"bank_details": {"bank_name": "TD"}},
            actor="staff",
        )


def test_mask_value_uses_the_shared_masking_contract(db_session):
    """PR #198's validators must accept everything we emit."""
    from app.api.clinic.v1.masking import validate_last4, validate_masked

    for raw, reveal, last in (("1234567890", 3, "890"), ("05012", 2, "12")):
        out = profiles.mask_value(raw, reveal)
        assert out["last"] == last
        assert out["is_masked"] is True
        assert raw not in out["masked"]
        validate_masked(out["masked"])   # would raise if it leaked
        validate_last4(out["last"])


def test_sin_never_lands_in_profile_storage(db_session, patient):
    values = _base_values()
    values["identification"] = {"social_insurance_number": "046454286"}
    profile = profiles.create_profile(
        db_session, patient_id=patient.id, values=values, actor="staff"
    )
    stored = profiles.profile_values(db_session, profile.id)["identification"][
        "social_insurance_number"
    ]
    assert stored == {"last3": "286", "stored": "platform_patients.sin_encrypted"}

    db_session.refresh(patient)
    assert patient.sin_last3 == "286"
    assert patient.sin_encrypted is not None

    # Even the privileged read must not surface the full SIN.
    read = profiles.read_profile(db_session, profile.id, include_sensitive=True)
    assert "046454286" not in str(read)


def test_bank_details_repeat_one_instance_per_owned_account(db_session, patient):
    profile = profiles.create_profile(
        db_session, patient_id=patient.id, values=_base_values(), actor="staff"
    )
    _add_bank_account(db_session, patient, institution_name="RBC", is_default=True)
    _add_bank_account(db_session, patient, institution_name="TD")

    read = profiles.read_profile(db_session, profile.id)
    banks = [b for b in read["blocks"] if b["block"] == "bank_details"]
    assert {b["index"] for b in banks} == {0, 1}
    # Default payment source sorts first — the Originations tab reads it that way.
    assert banks[0]["values"]["bank_name"] == "RBC"


def test_removed_bank_accounts_do_not_appear(db_session, patient):
    profile = profiles.create_profile(
        db_session, patient_id=patient.id, values=_base_values(), actor="staff"
    )
    row = _add_bank_account(db_session, patient)
    row.status = "removed"
    db_session.commit()
    read = profiles.read_profile(db_session, profile.id)
    # The empty block still renders (the form needs the section) but carries
    # no values — a removed account must not leak back through the profile.
    bank = next(b for b in read["blocks"] if b["block"] == "bank_details")
    assert bank["values"] == {}


# ---------------------------------------------------------------------------
# Application <- profile
# ---------------------------------------------------------------------------


def _make_application(db_session, patient, product) -> PlatformCreditApplication:
    application = PlatformCreditApplication(
        id=uuid4(),
        patient_id=patient.id,
        credit_product_id=product.id,
        credit_product_version=getattr(product, "version", 1) or 1,
        requested_amount_cents=500_000,
        requested_amount_source="clinic",
        status="started",
    )
    db_session.add(application)
    db_session.commit()
    return application


def test_attach_freezes_the_profile_and_fills_the_scored_columns(
    db_session, patient, product
):
    profile = profiles.create_profile(
        db_session, patient_id=patient.id, values=_base_values(), actor="a"
    )
    application = _make_application(db_session, patient, product)
    profiles.attach_profile_to_application(
        db_session, application, profile.id, actor="staff"
    )

    assert application.customer_profile_id == profile.id
    assert application.profile_version == 1
    assert application.profile_snapshot["profile_version"] == 1
    assert application.profile_snapshot_at is not None

    # Backwards compatibility: the migration-043 columns the decision engine
    # reads are populated exactly as a hand-entered application would be.
    assert application.first_name == "Ann"
    assert application.residence_city == "Kelowna"
    assert application.income_type == "employed_full_time"
    assert application.net_monthly_income_cents == 480_000
    assert application.monthly_housing_payment_cents == 185_000

    # A later profile edit must NOT rewrite the frozen basis of the decision.
    profiles.update_profile(
        db_session, profile.id, values={"personal": {"first_name": "Zoe"}}, actor="a"
    )
    db_session.refresh(application)
    assert application.profile_snapshot["values"]["personal"]["first_name"] == "Ann"


def test_one_profile_serves_many_applications(db_session, patient, product):
    profile = profiles.create_profile(
        db_session, patient_id=patient.id, values=_base_values(), actor="a"
    )
    first = _make_application(db_session, patient, product)
    second = _make_application(db_session, patient, product)
    for application in (first, second):
        profiles.attach_profile_to_application(
            db_session, application, profile.id, actor="staff"
        )
    assert first.customer_profile_id == second.customer_profile_id == profile.id
    # Re-application requires no re-entry of any field.
    assert second.first_name == "Ann"


def test_attach_rejects_a_profile_for_a_different_patient(db_session, patient, product):
    other = PlatformPatient(id=uuid4(), legal_first_name="Bob")
    db_session.add(other)
    db_session.commit()
    profile = profiles.create_profile(
        db_session, patient_id=other.id, values=_base_values(), actor="a"
    )
    application = _make_application(db_session, patient, product)
    with pytest.raises(profiles.ProfileError):
        profiles.attach_profile_to_application(
            db_session, application, profile.id, actor="staff"
        )


def test_snapshot_carries_only_masked_bank_details(db_session, patient, product):
    profile = profiles.create_profile(
        db_session, patient_id=patient.id, values=_base_values(), actor="staff"
    )
    _add_bank_account(db_session, patient)
    snapshot = profiles.build_snapshot(db_session, profile.id)
    account = snapshot["values"]["bank_details"]["account_number"]
    assert account.endswith("890")
    assert "1234567890" not in str(snapshot)


# ---------------------------------------------------------------------------
# Backfill — backwards compatibility
# ---------------------------------------------------------------------------


def test_backfill_materialises_a_profile_from_an_existing_application(
    db_session, patient, product
):
    application = _make_application(db_session, patient, product)
    application.first_name = "Ann"
    application.last_name = "Tremblay"
    application.email = "ann@example.ca"
    application.residence_city = "Kelowna"
    application.residence_province = "BC"
    application.residential_status = "rent"
    application.monthly_housing_payment_cents = 185_000
    application.income_type = "employed_full_time"
    application.net_monthly_income_cents = 480_000
    application.time_at_address_years = 6
    db_session.commit()

    profile = profiles.backfill_profile_from_application(db_session, application)
    assert profile is not None
    values = profiles.profile_values(db_session, profile.id)
    assert values["personal"]["first_name"] == "Ann"
    assert values["current_address"]["monthly_rent"] == "1850.00"
    assert values["primary_income"]["income_type"] == "employed_full_time"
    assert values["current_address"]["resided_since"]  # derived from years-at-address
    db_session.refresh(application)
    assert application.customer_profile_id == profile.id


def test_backfill_is_idempotent_and_never_clobbers_a_maintained_profile(
    db_session, patient, product
):
    profile = profiles.create_profile(
        db_session, patient_id=patient.id, values=_base_values(), actor="borrower"
    )
    application = _make_application(db_session, patient, product)
    application.first_name = "STALE"
    application.job_title = "Office Manager"
    db_session.commit()

    profiles.backfill_profile_from_application(db_session, application)
    values = profiles.profile_values(db_session, profile.id)
    # The maintained value wins; only the GAP is filled.
    assert values["personal"]["first_name"] == "Ann"
    assert values["primary_income"]["job_title"] == "Office Manager"

    before = profiles.get_profile(db_session, profile.id).version
    profiles.backfill_profile_from_application(db_session, application)
    assert profiles.get_profile(db_session, profile.id).version == before


def test_backfill_all_reports_what_it_did(db_session, patient, product):
    application = _make_application(db_session, patient, product)
    application.first_name = "Ann"
    db_session.commit()
    result = profiles.backfill_all(db_session, actor="admin")
    assert result["created"] >= 1
    assert set(result) == {"created", "topped_up", "skipped", "patients"}


def test_existing_applications_work_without_a_profile(db_session, patient, product):
    """Backwards compatibility: a NULL profile link breaks nothing."""
    application = _make_application(db_session, patient, product)
    assert application.customer_profile_id is None
    assert application.profile_snapshot is None
    assert application.status == "started"
