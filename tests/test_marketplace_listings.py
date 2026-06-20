"""Service-level tests for the marketplace lead engine (app/services/marketplace/listings.py).

Runs against the migrated local Postgres test DB via the repo's ``db_session``
fixture (tests/conftest.py truncates between tests). Pricing/visibility come from
the real config (config/marketplace/*.yaml); the default charge_trigger there is
``patient_selected``, which the select_clinic tests rely on. A monkeypatch test
covers the ``expressed_interest`` trigger without depending on config.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.models.platform.event import PlatformEvent
from app.models.platform.patient import PlatformPatient
from app.services.marketplace import listings as svc


def _make_patient(
    db,
    *,
    lead_state="pre_approved",
    verification_depth="id_bank_verified",
):
    patient = PlatformPatient(
        legal_first_name="Pat",
        legal_last_name="Ient",
        email=f"{uuid4().hex}@example.com",
        lead_state=lead_state,
        verification_depth=verification_depth,
    )
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return patient


def _create(db, patient, **kw):
    defaults = dict(
        patient_id=patient.id,
        treatment_categories=["implants"],
        treatment_urgency="this_week",
        estimated_budget_cents=2_000_000,
        location_postal_code="V1Y 2A3",
        consent_acknowledged=True,
    )
    defaults.update(kw)
    return svc.create_listing(db, **defaults)


# --- create_listing ---------------------------------------------------------


def test_create_listing_denormalizes_prices_and_flags(db_session):
    patient = _make_patient(
        db_session, lead_state="pre_approved", verification_depth="id_bank_verified"
    )
    listing = _create(db_session, patient)

    # denormalized snapshot from the patient
    assert listing.lead_state == "pre_approved"
    assert listing.verification_depth == "id_bank_verified"
    # priced via the engine (integer cents, > 0)
    assert isinstance(listing.base_lead_price_cents, int)
    assert listing.base_lead_price_cents > 0
    assert listing.status == "active"
    assert listing.expires_at > datetime.now(timezone.utc)

    db_session.refresh(patient)
    assert patient.marketplace_listed is True


def test_create_listing_emits_event(db_session):
    patient = _make_patient(db_session)
    listing = _create(db_session, patient, treatment_categories=["full_arch"])

    event = (
        db_session.query(PlatformEvent)
        .filter(PlatformEvent.event_type == "marketplace.listing_created")
        .filter(PlatformEvent.patient_id == patient.id)
        .one()
    )
    assert event.payload["lead_state"] == listing.lead_state
    assert event.payload["treatment_categories"] == ["full_arch"]


def test_create_listing_missing_patient_raises_lookup(db_session):
    with pytest.raises(LookupError):
        svc.create_listing(
            db_session,
            patient_id=uuid4(),
            treatment_categories=["implants"],
            treatment_urgency="flexible",
            estimated_budget_cents=None,
            location_postal_code="V1Y 2A3",
        )


# --- pause_listing ----------------------------------------------------------


def test_pause_listing_owner(db_session):
    patient = _make_patient(db_session)
    listing = _create(db_session, patient)
    paused = svc.pause_listing(
        db_session, listing_id=listing.id, patient_id=patient.id
    )
    assert paused.status == "paused"


def test_pause_listing_non_owner_raises_permission(db_session):
    patient = _make_patient(db_session)
    other = _make_patient(db_session)
    listing = _create(db_session, patient)
    with pytest.raises(PermissionError):
        svc.pause_listing(db_session, listing_id=listing.id, patient_id=other.id)


# --- list_leads_for_vendor --------------------------------------------------


def test_list_leads_active_and_visibility(db_session):
    visible = _make_patient(db_session, lead_state="pre_approved")
    l_visible = _create(db_session, visible)

    # A listing whose lead_state later becomes 'declined' (not visible_to_clinics).
    # 'declined' isn't in the pricing table, so it can't be priced at create time —
    # mirror a real post-listing lead-state change by flipping the snapshot directly.
    declined_patient = _make_patient(db_session, lead_state="pre_approved")
    l_declined = _create(db_session, declined_patient)
    l_declined.lead_state = "declined"
    db_session.commit()

    # a paused listing must not show
    paused_patient = _make_patient(db_session, lead_state="pre_approved")
    paused = _create(db_session, paused_patient)
    svc.pause_listing(db_session, listing_id=paused.id, patient_id=paused_patient.id)

    ids = {row["listing_id"] for row in svc.list_leads_for_vendor(db_session)}
    assert str(l_visible.id) in ids
    assert str(paused.id) not in ids
    # declined lead_state is not visible_to_clinics
    assert str(l_declined.id) not in ids


def test_list_leads_expired_excluded(db_session):
    patient = _make_patient(db_session)
    listing = _create(db_session, patient)
    listing.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    db_session.commit()

    ids = {row["listing_id"] for row in svc.list_leads_for_vendor(db_session)}
    assert str(listing.id) not in ids


def test_list_leads_treatment_category_filter(db_session):
    p1 = _make_patient(db_session)
    p2 = _make_patient(db_session)
    implants = _create(db_session, p1, treatment_categories=["implants"])
    ortho = _create(db_session, p2, treatment_categories=["orthodontics"])

    ids = {
        row["listing_id"]
        for row in svc.list_leads_for_vendor(db_session, treatment_category="implants")
    }
    assert str(implants.id) in ids
    assert str(ortho.id) not in ids


def test_list_leads_min_verification_filter(db_session):
    shallow = _make_patient(db_session, verification_depth="id_verified")
    deep = _make_patient(db_session, verification_depth="id_bank_cb_verified")
    l_shallow = _create(db_session, shallow)
    l_deep = _create(db_session, deep)

    ids = {
        row["listing_id"]
        for row in svc.list_leads_for_vendor(
            db_session, min_verification="id_bank_verified"
        )
    }
    assert str(l_deep.id) in ids
    assert str(l_shallow.id) not in ids


# --- express_interest -------------------------------------------------------


def test_express_interest_idempotent(db_session):
    patient = _make_patient(db_session)
    listing = _create(db_session, patient)
    vendor_id = uuid4()

    first = svc.express_interest(db_session, listing_id=listing.id, vendor_id=vendor_id)
    second = svc.express_interest(db_session, listing_id=listing.id, vendor_id=vendor_id)
    assert first.id == second.id

    event_count = (
        db_session.query(PlatformEvent)
        .filter(PlatformEvent.event_type == "marketplace.vendor_interest")
        .count()
    )
    assert event_count == 1


def test_express_interest_inactive_listing_raises(db_session):
    patient = _make_patient(db_session)
    listing = _create(db_session, patient)
    svc.pause_listing(db_session, listing_id=listing.id, patient_id=patient.id)
    with pytest.raises(ValueError):
        svc.express_interest(db_session, listing_id=listing.id, vendor_id=uuid4())


def test_express_interest_charges_when_trigger_is_expressed(db_session, monkeypatch):
    monkeypatch.setattr(svc, "get_charge_trigger", lambda: "expressed_interest")
    patient = _make_patient(db_session)
    listing = _create(db_session, patient)
    vendor_id = uuid4()

    interest = svc.express_interest(
        db_session, listing_id=listing.id, vendor_id=vendor_id
    )
    assert interest.lead_charge_cents == listing.base_lead_price_cents
    assert interest.charge_trigger == "expressed_interest"


# --- select_clinic + billing (default config trigger = patient_selected) -----


def test_select_clinic_charges_and_closes(db_session):
    patient = _make_patient(db_session)
    listing = _create(db_session, patient)
    vendor_id = uuid4()
    svc.express_interest(db_session, listing_id=listing.id, vendor_id=vendor_id)

    updated = svc.select_clinic(
        db_session,
        listing_id=listing.id,
        patient_id=patient.id,
        vendor_id=vendor_id,
    )
    assert updated.status == "closed"
    assert updated.accepted_vendor_id == vendor_id
    assert updated.accepted_at is not None

    billing = svc.vendor_billing(db_session, vendor_id=vendor_id)
    assert len(billing) == 1
    assert billing[0]["lead_charge_cents"] == listing.base_lead_price_cents
    assert billing[0]["charge_trigger"] == "patient_selected"
    assert billing[0]["listing_id"] == listing.id


def test_select_clinic_requires_interest(db_session):
    patient = _make_patient(db_session)
    listing = _create(db_session, patient)
    with pytest.raises(ValueError):
        svc.select_clinic(
            db_session,
            listing_id=listing.id,
            patient_id=patient.id,
            vendor_id=uuid4(),
        )


def test_select_clinic_non_owner_raises(db_session):
    patient = _make_patient(db_session)
    other = _make_patient(db_session)
    listing = _create(db_session, patient)
    vendor_id = uuid4()
    svc.express_interest(db_session, listing_id=listing.id, vendor_id=vendor_id)
    with pytest.raises(PermissionError):
        svc.select_clinic(
            db_session,
            listing_id=listing.id,
            patient_id=other.id,
            vendor_id=vendor_id,
        )


# --- get_listing_for_vendor / book_appointment ------------------------------


def test_get_listing_for_vendor_requires_interest(db_session):
    patient = _make_patient(db_session)
    listing = _create(db_session, patient)
    vendor_id = uuid4()

    assert (
        svc.get_listing_for_vendor(db_session, listing_id=listing.id, vendor_id=vendor_id)
        is None
    )
    svc.express_interest(db_session, listing_id=listing.id, vendor_id=vendor_id)
    view = svc.get_listing_for_vendor(
        db_session, listing_id=listing.id, vendor_id=vendor_id
    )
    assert view is not None
    # PII-free: no patient name/email/phone keys
    assert "legal_first_name" not in view
    assert "email" not in view


def test_book_appointment_requires_interest(db_session):
    patient = _make_patient(db_session)
    listing = _create(db_session, patient)
    with pytest.raises(ValueError):
        svc.book_appointment(db_session, listing_id=listing.id, vendor_id=uuid4())


def test_book_appointment_sets_timestamp(db_session):
    patient = _make_patient(db_session)
    listing = _create(db_session, patient)
    vendor_id = uuid4()
    svc.express_interest(db_session, listing_id=listing.id, vendor_id=vendor_id)
    interest = svc.book_appointment(
        db_session, listing_id=listing.id, vendor_id=vendor_id
    )
    assert interest.appointment_booked_at is not None
