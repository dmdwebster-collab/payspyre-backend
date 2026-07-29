"""The admin HTTP surface: portfolio import, providers, and the purge.

The service layers are covered by ``test_portfolio_import.py`` and
``test_demo_purge.py``. What is pinned here is the API contract an operator
actually calls: who may call it, what the safety interlocks reject, and that a
dry run really is a dry run.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.db.base import get_db
from app.main import app
from app.models.loan import Vendor
from app.models.platform.patient import PlatformPatient
from app.models.platform.provider import PlatformProvider
from app.models.user import User
from app.services import demo_purge

RETAINED = ("dave@payspyrebeta.com", "admin@payspyrebeta.com")


def _fake_user(role_name: str, email: str):
    role = type("Role", (), {"name": role_name})()
    user_role = type("UserRole", (), {"role": role})()
    return type(
        "U", (), {"id": uuid.uuid4(), "email": email, "role": role_name, "roles": [user_role]}
    )()


@pytest.fixture
def admin_client(db_session: Session):
    prior = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: _fake_user(
        "admin", "admin@payspyre.test"
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(prior)


@pytest.fixture
def staff_client(db_session: Session):
    prior = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: _fake_user(
        "patient", "user@payspyre.test"
    )
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(prior)


# ---------------------------------------------------------------------------
# Import profiles — the declarative mapping is discoverable over the API
# ---------------------------------------------------------------------------


def test_profiles_are_listed_and_fully_downloadable(admin_client):
    listed = admin_client.get("/api/v1/admin/import/portfolio/profiles")
    assert listed.status_code == 200
    names = [p["name"] for p in listed.json()["profiles"]]
    assert "legacy_servicing_v1" in names

    detail = admin_client.get("/api/v1/admin/import/portfolio/profiles/legacy_servicing_v1")
    assert detail.status_code == 200
    body = detail.json()
    # The whole mapping comes back as JSON, so an operator can copy it, re-point
    # the column bindings at a different source system, and import without a deploy.
    assert body["accounts"]["columns"]["account_number"] == "Acct#"
    assert body["transactions"]["header_row"] == 14
    assert body["money_unit"] == "dollars"
    assert body["status_map"]["OPEN/ACTIVE"] == "active"


def test_an_unknown_profile_is_a_404_not_a_silent_default(admin_client):
    r = admin_client.get("/api/v1/admin/import/portfolio/profiles/no_such_source")
    assert r.status_code == 404


def test_import_endpoints_are_admin_only(staff_client):
    assert staff_client.get("/api/v1/admin/import/portfolio/profiles").status_code == 403
    assert staff_client.post("/api/v1/admin/maintenance/demo-purge/dry-run").status_code == 403


def test_a_non_workbook_upload_is_refused(admin_client):
    r = admin_client.post(
        "/api/v1/admin/import/portfolio/preview",
        files={"file": ("book.csv", b"a,b,c\n1,2,3\n", "text/csv")},
    )
    assert r.status_code == 422
    assert ".xlsx" in r.json()["detail"]


def test_a_corrupt_workbook_is_a_422_not_a_500(admin_client):
    r = admin_client.post(
        "/api/v1/admin/import/portfolio/preview",
        files={"file": ("book.xlsx", b"not actually a zip", None)},
    )
    assert r.status_code == 422
    assert "readable" in r.json()["detail"]


def test_preview_reports_the_book_without_writing_anything(admin_client, db_session):
    from tests.fixtures.portfolio_book import build_xlsx_bytes

    r = admin_client.post(
        "/api/v1/admin/import/portfolio/preview",
        files={"file": ("book.xlsx", build_xlsx_bytes(), None)},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["source"]["accounts"] == 5
    assert body["source"]["transactions"] == 12
    assert body["loans"]["skipped_by_status"] == ["5004"]
    # The account whose stated balance contradicts its own history is named.
    assert body["reconciliation"]["source"]["accounts_with_discrepancies"] == 1
    assert db_session.query(PlatformProvider).count() == 0
    assert db_session.query(Vendor).count() == 0


def test_apply_defaults_to_a_dry_run_that_leaves_no_trace(admin_client, db_session):
    from tests.fixtures.portfolio_book import build_xlsx_bytes

    r = admin_client.post(
        "/api/v1/admin/import/portfolio/apply",
        files={"file": ("book.xlsx", build_xlsx_bytes(), None)},
        data={"generate_placeholders": "true",
              "placeholder_email_domain": "payspyre-import.kom",
              "placeholder_phone_area_code": "555"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert body["loans"]["created"] == 4
    assert body["options"]["placeholders"]["email_tld"] == "kom"
    # Every write was rolled back.
    assert db_session.query(Vendor).count() == 0
    assert db_session.query(PlatformPatient).count() == 0


def test_a_committed_apply_lands_the_book_and_reports_reconciliation(admin_client, db_session):
    from tests.fixtures.portfolio_book import build_xlsx_bytes

    r = admin_client.post(
        "/api/v1/admin/import/portfolio/apply",
        files={"file": ("book.xlsx", build_xlsx_bytes(), None)},
        data={"dry_run": "false", "generate_placeholders": "true",
              "placeholder_email_domain": "payspyre-import.kom",
              "placeholder_phone_area_code": "555"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is False
    assert body["loans"]["created"] == 4
    assert body["reconciliation"]["persisted"]["ok"] is True
    assert db_session.query(Vendor).count() == 2
    assert db_session.query(PlatformProvider).count() == 4
    for p in db_session.query(PlatformPatient).all():
        assert p.email.endswith(".kom")
        assert p.phone_e164.startswith("+1555")


def test_a_placeholder_shape_that_cannot_be_safe_is_refused(admin_client):
    r = admin_client.post(
        "/api/v1/admin/import/portfolio/apply",
        files={"file": ("book.xlsx", b"not-a-real-workbook", None)},
        data={"generate_placeholders": "true", "placeholder_phone_area_code": "5"},
    )
    assert r.status_code == 422
    assert "area_code" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


@pytest.fixture
def vendor(db_session: Session) -> Vendor:
    v = Vendor(
        external_code="BC1000", business_name="Northside Dental",
        business_type="corporation", contact_name="A", email="a@b.invalid",
        phone="1", address_line1="1", city="Kelowna", province="BC",
        postal_code="V1Y1A1",
    )
    db_session.add(v)
    db_session.commit()
    return v


def test_a_provider_can_be_added_and_retired_but_never_deleted(admin_client, vendor, db_session):
    created = admin_client.post(
        "/api/v1/admin/providers",
        json={"vendor_id": str(vendor.id), "name": "Dr. Ada Lovelace"},
    )
    assert created.status_code == 201
    provider_id = created.json()["id"]
    assert created.json()["is_active"] is True

    listed = admin_client.get(f"/api/v1/admin/providers?vendor_id={vendor.id}")
    assert [p["name"] for p in listed.json()] == ["Dr. Ada Lovelace"]

    retired = admin_client.patch(
        f"/api/v1/admin/providers/{provider_id}", json={"is_active": False}
    )
    assert retired.status_code == 200
    assert retired.json()["is_active"] is False

    # Gone from the dropdown, still present as a record.
    assert admin_client.get(f"/api/v1/admin/providers?vendor_id={vendor.id}").json() == []
    assert db_session.query(PlatformProvider).count() == 1


def test_a_vendor_cannot_have_the_same_provider_twice(admin_client, vendor):
    body = {"vendor_id": str(vendor.id), "name": "Dr. Ada Lovelace"}
    assert admin_client.post("/api/v1/admin/providers", json=body).status_code == 201
    dup = admin_client.post(
        "/api/v1/admin/providers",
        json={"vendor_id": str(vendor.id), "name": "  dr.  ADA lovelace "},
    )
    assert dup.status_code == 409


def test_a_provider_needs_a_real_vendor(admin_client):
    r = admin_client.post(
        "/api/v1/admin/providers",
        json={"vendor_id": str(uuid.uuid4()), "name": "Dr. Nobody"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# The purge endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
def with_operators(db_session: Session):
    for email in RETAINED:
        db_session.add(
            User(email=email, first_name="Op", last_name="Erator", password_hash="x")
        )
    db_session.add(
        User(email="demo@example.com", first_name="D", last_name="T", password_hash="x")
    )
    db_session.add(PlatformPatient(legal_first_name="Demo", legal_last_name="Borrower"))
    db_session.commit()
    return db_session


def test_the_dry_run_endpoint_reports_counts_and_deletes_nothing(admin_client, with_operators):
    r = admin_client.post("/api/v1/admin/maintenance/demo-purge/dry-run", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert body["tables"]["platform_patients"] == 1
    assert body["tables"]["users"] == 1
    assert body["retained_emails"] == sorted(RETAINED)
    assert with_operators.query(PlatformPatient).count() == 1
    assert with_operators.query(User).count() == 3


def test_the_purge_endpoint_refuses_without_the_confirmation_phrase(admin_client, with_operators):
    # Omitted entirely -> the request body itself is invalid.
    assert admin_client.post("/api/v1/admin/maintenance/demo-purge", json={}).status_code == 422
    # Present but wrong -> refused by the interlock, nothing changed.
    wrong = admin_client.post(
        "/api/v1/admin/maintenance/demo-purge", json={"confirmation": "yes please"}
    )
    assert wrong.status_code == 409
    assert "PURGE-DEMO-DATA" in wrong.json()["detail"]
    assert with_operators.query(PlatformPatient).count() == 1


def test_the_purge_endpoint_deletes_and_keeps_the_named_logins(admin_client, with_operators):
    r = admin_client.post(
        "/api/v1/admin/maintenance/demo-purge",
        json={"confirmation": demo_purge.CONFIRMATION_TOKEN},
    )
    assert r.status_code == 200
    assert r.json()["tables"]["platform_patients"] == 1
    assert with_operators.query(PlatformPatient).count() == 0
    assert {u.email for u in with_operators.query(User).all()} == set(RETAINED)


def test_the_purge_is_refused_in_production_over_http(admin_client, with_operators, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    r = admin_client.post(
        "/api/v1/admin/maintenance/demo-purge",
        json={"confirmation": demo_purge.CONFIRMATION_TOKEN},
    )
    assert r.status_code == 409
    assert "production" in r.json()["detail"]
    assert with_operators.query(PlatformPatient).count() == 1
