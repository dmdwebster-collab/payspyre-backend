"""Endpoint tests for the marketplace API (applicant + clinic surfaces).

NOTE ON WIRING: the orchestrator has not yet added the two marketplace routers
to ``applicant_router`` / ``clinic_router``. So this test builds its OWN FastAPI
app and mounts both marketplace routers directly under their real prefixes, then
overrides ``get_db`` (-> the test session), ``get_current_applicant`` (-> a fixed
patient), and ``get_current_clinic_user`` (-> a fixed vendor). Once the routers
are wired into the shared app, these could instead use the repo ``client``
fixture; until then the standalone app keeps the test self-contained.
"""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.applicant.v1.deps import ApplicantClaims, get_current_applicant
from app.api.applicant.v1.endpoints import marketplace as applicant_mp
from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.api.clinic.v1.endpoints import marketplace as clinic_mp
from app.db.base import get_db
from app.models.platform.patient import PlatformPatient

_VENDOR_ID = uuid4()


def _make_patient(db):
    patient = PlatformPatient(
        legal_first_name="Pat",
        legal_last_name="Ient",
        email=f"{uuid4().hex}@example.com",
        lead_state="pre_approved",
        verification_depth="id_bank_verified",
    )
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return patient


def _build_app(db_session, patient_id):
    app = FastAPI()
    app.include_router(applicant_mp.router, prefix="/api/applicant/v1")
    app.include_router(clinic_mp.router, prefix="/api/clinic/v1")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_applicant] = lambda: ApplicantClaims(
        patient_id=patient_id, app_ids=[]
    )
    app.dependency_overrides[get_current_clinic_user] = lambda: ClinicPrincipal(
        user=SimpleNamespace(id=uuid4()), vendor_id=_VENDOR_ID
    )
    return app


def test_marketplace_happy_path(db_session):
    patient = _make_patient(db_session)
    app = _build_app(db_session, patient.id)
    client = TestClient(app)

    # 1) patient creates a listing
    resp = client.post(
        "/api/applicant/v1/marketplace/listings",
        json={
            "treatment_categories": ["implants"],
            "treatment_urgency": "this_week",
            "estimated_budget_cents": 2_000_000,
            "location_postal_code": "V1Y 2A3",
        },
    )
    assert resp.status_code == 201, resp.text
    listing = resp.json()
    listing_id = listing["id"]
    assert listing["status"] == "active"
    assert listing["lead_state"] == "pre_approved"
    assert listing["base_lead_price_cents"] > 0

    # 2) vendor browses leads and sees it (PII-free)
    resp = client.get("/api/clinic/v1/marketplace/leads")
    assert resp.status_code == 200, resp.text
    leads = resp.json()
    assert any(l["listing_id"] == listing_id for l in leads)
    for l in leads:
        assert "email" not in l and "legal_first_name" not in l

    # vendor cannot see listing detail before expressing interest
    resp = client.get(f"/api/clinic/v1/marketplace/listings/{listing_id}")
    assert resp.status_code == 404

    # 3) vendor expresses interest
    resp = client.post(
        f"/api/clinic/v1/marketplace/leads/{listing_id}/express_interest"
    )
    assert resp.status_code == 201, resp.text

    # now the detail view is available
    resp = client.get(f"/api/clinic/v1/marketplace/listings/{listing_id}")
    assert resp.status_code == 200, resp.text

    # 4) patient sees interested clinics
    resp = client.get(
        f"/api/applicant/v1/marketplace/listings/{listing_id}/interested_clinics"
    )
    assert resp.status_code == 200, resp.text
    clinics = resp.json()
    assert len(clinics) == 1
    assert clinics[0]["vendor_id"] == str(_VENDOR_ID)

    # 5) patient selects the clinic -> closed + charge recorded
    resp = client.post(
        f"/api/applicant/v1/marketplace/listings/{listing_id}/select_clinic",
        json={"vendor_id": str(_VENDOR_ID)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "closed"

    # 6) vendor billing reflects the charge
    resp = client.get("/api/clinic/v1/marketplace/billing/leads")
    assert resp.status_code == 200, resp.text
    billing = resp.json()
    assert len(billing) == 1
    assert billing[0]["lead_charge_cents"] == listing["base_lead_price_cents"]
    assert billing[0]["charge_trigger"] == "patient_selected"


def test_pause_other_patients_listing_is_403(db_session):
    owner = _make_patient(db_session)
    intruder = _make_patient(db_session)

    # owner creates a listing
    owner_app = _build_app(db_session, owner.id)
    owner_client = TestClient(owner_app)
    resp = owner_client.post(
        "/api/applicant/v1/marketplace/listings",
        json={
            "treatment_categories": ["implants"],
            "treatment_urgency": "flexible",
            "estimated_budget_cents": None,
            "location_postal_code": "V1Y 2A3",
        },
    )
    assert resp.status_code == 201, resp.text
    listing_id = resp.json()["id"]

    # a DIFFERENT patient tries to pause it -> 403
    intruder_app = _build_app(db_session, intruder.id)
    intruder_client = TestClient(intruder_app)
    resp = intruder_client.post(
        f"/api/applicant/v1/marketplace/listings/{listing_id}/pause"
    )
    assert resp.status_code == 403, resp.text
