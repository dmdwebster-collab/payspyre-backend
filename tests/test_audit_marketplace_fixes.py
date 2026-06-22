"""Regression tests for the marketplace audit fixes:
- listing requires + records marketplace_listing consent
- soft-deleted patients excluded from listing + lead discovery
- marketplace_listed denorm flag reset on status transition
"""
import uuid
from datetime import datetime, timezone

import pytest

from app.models.platform.consent import PlatformConsent
from app.models.platform.patient import PlatformPatient
from app.services.marketplace import listings as svc


def _patient(db, *, deleted=False, lead_state="pre_qualified", verification_depth="id_verified"):
    p = PlatformPatient(
        email=f"{uuid.uuid4().hex}@example.com",
        lead_state=lead_state,
        verification_depth=verification_depth,
    )
    if deleted:
        p.deleted_at = datetime.now(timezone.utc)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


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


def test_create_listing_requires_consent(db_session):
    p = _patient(db_session)
    with pytest.raises(ValueError):
        _create(db_session, p, consent_acknowledged=False)


def test_create_listing_records_marketplace_consent(db_session):
    p = _patient(db_session)
    _create(db_session, p)
    consent = (
        db_session.query(PlatformConsent)
        .filter(
            PlatformConsent.patient_id == p.id,
            PlatformConsent.purpose == "marketplace_listing",
        )
        .first()
    )
    assert consent is not None
    assert consent.consent_granted is True


def test_soft_deleted_patient_cannot_list(db_session):
    p = _patient(db_session, deleted=True)
    with pytest.raises(LookupError):
        _create(db_session, p)


def test_soft_deleted_patient_excluded_from_leads(db_session):
    p = _patient(db_session)
    listing = _create(db_session, p)
    # The lead is discoverable while the patient is live.
    leads = svc.list_leads_for_vendor(db_session)
    assert any(str(x["listing_id"]) == str(listing.id) for x in leads)
    # Erase (soft-delete) the patient — the lead must disappear.
    p.deleted_at = datetime.now(timezone.utc)
    db_session.commit()
    leads = svc.list_leads_for_vendor(db_session)
    assert all(str(x["listing_id"]) != str(listing.id) for x in leads)


def test_soft_deleted_patient_listing_not_loadable_by_id(db_session):
    """Direct-id read paths through _load_listing must 404 for an erased patient.

    Covers the vendor-facing reads (get_listing_for_vendor / express_interest /
    book_appointment) and the owner-scoped reads (pause / interested_clinics /
    select_clinic), all of which load the listing by id via _load_listing.
    """
    p = _patient(db_session)
    listing = _create(db_session, p)
    # Live: vendor can still load it by id (no interest yet -> None, not LookupError).
    assert svc.get_listing_for_vendor(
        db_session, listing_id=listing.id, vendor_id=uuid.uuid4()
    ) is None

    # Erase (soft-delete) the patient — the listing must no longer be loadable.
    p.deleted_at = datetime.now(timezone.utc)
    db_session.commit()

    vendor_id = uuid.uuid4()
    with pytest.raises(LookupError):
        svc.get_listing_for_vendor(db_session, listing_id=listing.id, vendor_id=vendor_id)
    with pytest.raises(LookupError):
        svc.express_interest(db_session, listing_id=listing.id, vendor_id=vendor_id)
    with pytest.raises(LookupError):
        svc.book_appointment(db_session, listing_id=listing.id, vendor_id=vendor_id)
    # Owner-scoped reads also 404 rather than leaking the erased patient's listing.
    with pytest.raises(LookupError):
        svc.interested_clinics(db_session, listing_id=listing.id, patient_id=p.id)
    with pytest.raises(LookupError):
        svc.pause_listing(db_session, listing_id=listing.id, patient_id=p.id)


def test_marketplace_listed_resets_on_pause(db_session):
    p = _patient(db_session)
    listing = _create(db_session, p)
    db_session.refresh(p)
    assert p.marketplace_listed is True
    svc.pause_listing(db_session, listing_id=listing.id, patient_id=p.id)
    db_session.refresh(p)
    assert p.marketplace_listed is False  # no live listing remains
