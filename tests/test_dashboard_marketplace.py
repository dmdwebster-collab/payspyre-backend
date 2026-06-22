"""Tests for Phase 3 vendor dashboard — marketplace funnel & revenue.

Mirrors ``tests/test_marketplace_api.py``: builds a standalone FastAPI app that
mounts ONLY the new ``dashboard_marketplace`` router, overrides ``get_db`` (-> the
test session) and ``get_current_clinic_user`` (-> a fixed vendor). Data is seeded
directly through the session so the real vendor-scoped queries are exercised.

Covers: the application + marketplace funnel tracks (incl. the derived
``disbursed`` count from linked disbursed loans), the revenue endpoint
(total/by_trigger/day-bucketed timeseries), the pure ``marketplace_block``
helper, and cross-vendor scoping isolation (no leakage).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.api.clinic.v1.endpoints import dashboard_marketplace as dm
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.loan import PlatformLoan
from app.models.platform.marketplace import (
    PlatformMarketplaceListing,
    PlatformMarketplaceVendorInterest,
)
from app.models.platform.patient import PlatformPatient


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- seed helpers -----------------------------------------------------------


def _make_vendor(db, name="Test Clinic") -> Vendor:
    v = Vendor(
        business_name=name,
        business_type="corporation",
        contact_name="Owner",
        email=f"{uuid4().hex}@example.com",
        phone="+15555550100",
        address_line1="1 Main St",
        city="Kelowna",
        province="BC",
        postal_code="V1Y2A3",
        status="active",
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


def _make_patient(db) -> PlatformPatient:
    p = PlatformPatient(
        legal_first_name="Pat",
        legal_last_name="Ient",
        email=f"{uuid4().hex}@example.com",
        lead_state="pre_approved",
        verification_depth="id_bank_verified",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _credit_product_id(db):
    """The dental_full_arch_v1 product re-seeded by conftest after TRUNCATE."""
    row = db.execute(
        text("SELECT id FROM platform_credit_products WHERE code = 'dental_full_arch_v1'")
    ).fetchone()
    return row[0]


def _make_application(db, *, vendor_id, patient_id, product_id, status, created_at=None):
    app_row = PlatformCreditApplication(
        patient_id=patient_id,
        credit_product_id=product_id,
        credit_product_version=1,
        requested_amount_cents=2_000_000,
        requested_amount_source="clinic",
        vendor_id=vendor_id,
        status=status,
    )
    db.add(app_row)
    db.commit()
    db.refresh(app_row)
    if created_at is not None:
        db.execute(
            text("UPDATE platform_credit_applications SET created_at = :ts WHERE id = :id"),
            {"ts": created_at, "id": app_row.id},
        )
        db.commit()
        db.refresh(app_row)
    return app_row


def _make_loan(db, *, application_id, disbursed_at):
    loan = PlatformLoan(
        application_id=application_id,
        principal_cents=2_000_000,
        annual_rate_bps=1299,
        term_months=36,
        status="active",
        disbursed_at=disbursed_at,
        principal_balance_cents=2_000_000,
    )
    db.add(loan)
    db.commit()
    db.refresh(loan)
    return loan


def _make_listing(db, *, patient_id) -> PlatformMarketplaceListing:
    listing = PlatformMarketplaceListing(
        patient_id=patient_id,
        status="active",
        treatment_categories=["implants"],
        treatment_urgency="this_week",
        estimated_budget_cents=2_000_000,
        location_postal_code="V1Y2A3",
        max_travel_km=25,
        lead_state="pre_approved",
        verification_depth="id_bank_verified",
        base_lead_price_cents=15_000,
        expires_at=_now() + timedelta(days=30),
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)
    return listing


def _make_interest(
    db,
    *,
    vendor_id,
    listing_id,
    expressed_at=None,
    appointment_booked_at=None,
    lead_charge_cents=None,
    lead_charged_at=None,
    charge_trigger=None,
):
    interest = PlatformMarketplaceVendorInterest(
        vendor_id=vendor_id,
        listing_id=listing_id,
        appointment_booked_at=appointment_booked_at,
        lead_charge_cents=lead_charge_cents,
        lead_charged_at=lead_charged_at,
        charge_trigger=charge_trigger,
    )
    db.add(interest)
    db.commit()
    db.refresh(interest)
    if expressed_at is not None:
        db.execute(
            text(
                "UPDATE platform_marketplace_vendor_interest "
                "SET expressed_at = :ts WHERE id = :id"
            ),
            {"ts": expressed_at, "id": interest.id},
        )
        db.commit()
        db.refresh(interest)
    return interest


# --- app fixture ------------------------------------------------------------


def _build_app(db_session, vendor_id):
    app = FastAPI()
    app.include_router(dm.router, prefix="/api/clinic/v1")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_clinic_user] = lambda: ClinicPrincipal(
        user=SimpleNamespace(id=uuid4()), vendor_id=vendor_id
    )
    return app


# --- tests ------------------------------------------------------------------


def test_funnel_application_and_marketplace_tracks(db_session):
    vendor = _make_vendor(db_session)
    patient = _make_patient(db_session)
    product_id = _credit_product_id(db_session)

    # application track: 1 started, 1 verifying, 2 pre_qualified, 2 approved
    _make_application(db_session, vendor_id=vendor.id, patient_id=patient.id,
                      product_id=product_id, status="started")
    _make_application(db_session, vendor_id=vendor.id, patient_id=patient.id,
                      product_id=product_id, status="verifying")
    _make_application(db_session, vendor_id=vendor.id, patient_id=patient.id,
                      product_id=product_id, status="pre_qualified")
    _make_application(db_session, vendor_id=vendor.id, patient_id=patient.id,
                      product_id=product_id, status="pre_qualified")
    approved_a = _make_application(db_session, vendor_id=vendor.id, patient_id=patient.id,
                                   product_id=product_id, status="approved")
    _make_application(db_session, vendor_id=vendor.id, patient_id=patient.id,
                      product_id=product_id, status="approved")

    # one approved app has a disbursed loan -> disbursed = 1
    _make_loan(db_session, application_id=approved_a.id, disbursed_at=_now())

    # marketplace track
    listing1 = _make_listing(db_session, patient_id=patient.id)
    listing2 = _make_listing(db_session, patient_id=patient.id)
    _make_interest(db_session, vendor_id=vendor.id, listing_id=listing1.id)
    _make_interest(
        db_session, vendor_id=vendor.id, listing_id=listing2.id,
        appointment_booked_at=_now(),
        lead_charge_cents=15_000, lead_charged_at=_now(),
        charge_trigger="appointment_booked",
    )

    app = _build_app(db_session, vendor.id)
    client = TestClient(app)

    resp = client.get("/api/clinic/v1/dashboard/funnel")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["started"] == 1
    assert body["verifying"] == 1
    assert body["pre_qualified"] == 2
    assert body["approved"] == 2
    assert body["disbursed"] == 1

    assert body["leads_viewed"] == 2          # 2 distinct listings
    assert body["interest_expressed"] == 2    # 2 interest rows in window
    assert body["appointment_booked"] == 1
    assert body["charged"] == 1


def test_revenue_totals_by_trigger_and_timeseries(db_session):
    vendor = _make_vendor(db_session)
    patient = _make_patient(db_session)

    day1 = _now() - timedelta(days=2)
    day2 = _now() - timedelta(days=1)

    l1 = _make_listing(db_session, patient_id=patient.id)
    l2 = _make_listing(db_session, patient_id=patient.id)
    l3 = _make_listing(db_session, patient_id=patient.id)

    _make_interest(db_session, vendor_id=vendor.id, listing_id=l1.id,
                   lead_charge_cents=15_000, lead_charged_at=day1,
                   charge_trigger="patient_selected")
    _make_interest(db_session, vendor_id=vendor.id, listing_id=l2.id,
                   lead_charge_cents=15_000, lead_charged_at=day1,
                   charge_trigger="appointment_booked")
    _make_interest(db_session, vendor_id=vendor.id, listing_id=l3.id,
                   lead_charge_cents=20_000, lead_charged_at=day2,
                   charge_trigger="patient_selected")

    app = _build_app(db_session, vendor.id)
    client = TestClient(app)

    resp = client.get("/api/clinic/v1/dashboard/revenue")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["total_charges_cents"] == 50_000
    assert body["charge_count"] == 3
    assert body["by_trigger"] == {
        "patient_selected": 35_000,
        "appointment_booked": 15_000,
    }
    # day-bucketed timeseries, chronological
    assert len(body["timeseries"]) == 2
    assert body["timeseries"][0]["bucket"] == day1.date().isoformat()
    assert body["timeseries"][0]["charges_cents"] == 30_000
    assert body["timeseries"][0]["charge_count"] == 2
    assert body["timeseries"][1]["bucket"] == day2.date().isoformat()
    assert body["timeseries"][1]["charges_cents"] == 20_000
    assert body["timeseries"][1]["charge_count"] == 1


def test_marketplace_block_helper(db_session):
    vendor = _make_vendor(db_session)
    patient = _make_patient(db_session)

    l1 = _make_listing(db_session, patient_id=patient.id)
    l2 = _make_listing(db_session, patient_id=patient.id)
    _make_interest(db_session, vendor_id=vendor.id, listing_id=l1.id)
    _make_interest(db_session, vendor_id=vendor.id, listing_id=l2.id,
                   appointment_booked_at=_now(),
                   lead_charge_cents=15_000, lead_charged_at=_now(),
                   charge_trigger="appointment_booked")

    frm = _now() - timedelta(days=30)
    to = _now() + timedelta(minutes=1)
    block = dm.marketplace_block(db_session, vendor.id, frm, to)

    assert set(block.keys()) == {
        "leads_available",
        "interests_expressed",
        "appointments_booked",
        "charges_cents",
    }
    assert block["leads_available"] == 2
    assert block["interests_expressed"] == 2
    assert block["appointments_booked"] == 1
    assert block["charges_cents"] == 15_000


def test_window_filtering_excludes_out_of_range(db_session):
    vendor = _make_vendor(db_session)
    patient = _make_patient(db_session)
    product_id = _credit_product_id(db_session)

    old = _now() - timedelta(days=200)
    # an old application + an old charge, both outside the default 30-day window
    _make_application(db_session, vendor_id=vendor.id, patient_id=patient.id,
                      product_id=product_id, status="approved", created_at=old)
    l_old = _make_listing(db_session, patient_id=patient.id)
    _make_interest(db_session, vendor_id=vendor.id, listing_id=l_old.id,
                   expressed_at=old, lead_charge_cents=99_000,
                   lead_charged_at=old, charge_trigger="patient_selected")

    app = _build_app(db_session, vendor.id)
    client = TestClient(app)

    funnel = client.get("/api/clinic/v1/dashboard/funnel").json()
    assert funnel["approved"] == 0           # old app excluded by default window
    assert funnel["interest_expressed"] == 0  # old interest excluded
    assert funnel["charged"] == 0

    revenue = client.get("/api/clinic/v1/dashboard/revenue").json()
    assert revenue["total_charges_cents"] == 0
    assert revenue["charge_count"] == 0

    # but an explicit wide window includes them
    frm = (old - timedelta(days=1)).isoformat()
    to = _now().isoformat()
    funnel2 = client.get(
        "/api/clinic/v1/dashboard/funnel", params={"from": frm, "to": to}
    ).json()
    assert funnel2["approved"] == 1
    revenue2 = client.get(
        "/api/clinic/v1/dashboard/revenue", params={"from": frm, "to": to}
    ).json()
    assert revenue2["total_charges_cents"] == 99_000


def test_cross_vendor_scoping_isolation(db_session):
    vendor_a = _make_vendor(db_session, name="Clinic A")
    vendor_b = _make_vendor(db_session, name="Clinic B")
    patient = _make_patient(db_session)
    product_id = _credit_product_id(db_session)

    # vendor A data
    a_approved = _make_application(db_session, vendor_id=vendor_a.id, patient_id=patient.id,
                                   product_id=product_id, status="approved")
    _make_loan(db_session, application_id=a_approved.id, disbursed_at=_now())
    la = _make_listing(db_session, patient_id=patient.id)
    _make_interest(db_session, vendor_id=vendor_a.id, listing_id=la.id,
                   lead_charge_cents=15_000, lead_charged_at=_now(),
                   charge_trigger="patient_selected")

    # vendor B data (must NOT show up for vendor A)
    _make_application(db_session, vendor_id=vendor_b.id, patient_id=patient.id,
                      product_id=product_id, status="approved")
    _make_application(db_session, vendor_id=vendor_b.id, patient_id=patient.id,
                      product_id=product_id, status="started")
    lb = _make_listing(db_session, patient_id=patient.id)
    _make_interest(db_session, vendor_id=vendor_b.id, listing_id=lb.id,
                   lead_charge_cents=88_000, lead_charged_at=_now(),
                   charge_trigger="patient_selected")

    # query AS vendor A
    app_a = _build_app(db_session, vendor_a.id)
    client_a = TestClient(app_a)

    funnel_a = client_a.get("/api/clinic/v1/dashboard/funnel").json()
    assert funnel_a["approved"] == 1     # only A's approved app
    assert funnel_a["started"] == 0      # B's started app not leaked
    assert funnel_a["disbursed"] == 1
    assert funnel_a["leads_viewed"] == 1
    assert funnel_a["charged"] == 1

    revenue_a = client_a.get("/api/clinic/v1/dashboard/revenue").json()
    assert revenue_a["total_charges_cents"] == 15_000  # NOT 15_000 + 88_000

    block_a = dm.marketplace_block(
        db_session, vendor_a.id, _now() - timedelta(days=30), _now() + timedelta(minutes=1)
    )
    assert block_a["charges_cents"] == 15_000
    assert block_a["leads_available"] == 1

    # and AS vendor B sees only its own
    app_b = _build_app(db_session, vendor_b.id)
    client_b = TestClient(app_b)
    revenue_b = client_b.get("/api/clinic/v1/dashboard/revenue").json()
    assert revenue_b["total_charges_cents"] == 88_000
