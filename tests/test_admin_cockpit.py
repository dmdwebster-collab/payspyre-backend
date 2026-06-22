"""Lender/admin portal Phase 1 read cockpit — integration tests (live test DB).

Mounts the v1 api_router, overrides get_db with the conftest db_session and
get_current_user with a fake admin, seeds a little data, and asserts the
whole-book reads + the admin/staff gate.
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.api import api_router
from app.core.auth import get_current_user
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import PlatformLoan
from app.models.platform.patient import PlatformPatient

_BASE = "/api/v1/admin"


def _admin():
    return SimpleNamespace(
        id=uuid.uuid4(), roles=[SimpleNamespace(role=SimpleNamespace(name="admin"))]
    )


def _non_privileged():
    return SimpleNamespace(
        id=uuid.uuid4(), roles=[SimpleNamespace(role=SimpleNamespace(name="patient"))]
    )


def _product_id(db: Session):
    p = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
    )
    assert p is not None
    return p.id


def _seed(db: Session):
    patient = PlatformPatient(email=f"admin-cockpit-{uuid.uuid4().hex[:8]}@example.com",
                              legal_first_name="Jordan", legal_last_name="Lee")
    db.add(patient)
    db.commit()
    db.refresh(patient)
    app_approved = PlatformCreditApplication(
        patient_id=patient.id, credit_product_id=_product_id(db), credit_product_version=1,
        requested_amount_cents=1_800_000, requested_amount_source="clinic", status="approved",
    )
    app_review = PlatformCreditApplication(
        patient_id=patient.id, credit_product_id=_product_id(db), credit_product_version=1,
        requested_amount_cents=900_000, requested_amount_source="patient", status="under_review",
    )
    db.add_all([app_approved, app_review])
    db.commit()
    db.refresh(app_approved)
    loan = PlatformLoan(
        application_id=app_approved.id, principal_cents=1_800_000, annual_rate_bps=1290,
        term_months=24, status="active", principal_balance_cents=1_500_000, currency="CAD",
    )
    db.add(loan)
    db.add(PlatformEvent(event_type="admin_cockpit_test", actor="patient",
                         patient_id=patient.id, application_id=app_approved.id, payload={"v": 1}))
    db.commit()
    return patient, app_approved, app_review, loan


@pytest.fixture
def client(db_session: Session):
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = _admin
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestReadCockpit:
    def test_dashboard_overview(self, client, db_session):
        _seed(db_session)
        r = client.get(f"{_BASE}/dashboard/overview")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["applications"]["total"] >= 2
        assert body["loan_book"]["total"] >= 1
        # the approved app + active loan are reflected
        assert body["work_queue"]["under_review"] >= 1
        assert body["loan_book"]["active_principal_balance_cents"] >= 1_500_000

    def test_applications_list_and_detail(self, client, db_session):
        _, app_approved, _, _ = _seed(db_session)
        rows = client.get(f"{_BASE}/applications?status=under_review").json()
        assert all(row["status"] == "under_review" for row in rows)
        detail = client.get(f"{_BASE}/applications/{app_approved.id}")
        assert detail.status_code == 200, detail.text
        d = detail.json()
        assert d["patient_name"] == "Jordan Lee"
        assert d["status"] == "approved"

    def test_application_detail_404(self, client):
        assert client.get(f"{_BASE}/applications/{uuid.uuid4()}").status_code == 404

    def test_loans_list_and_aging(self, client, db_session):
        _seed(db_session)
        loans = client.get(f"{_BASE}/loans?status=active").json()
        assert any(loan["status"] == "active" for loan in loans)
        aging = client.get(f"{_BASE}/collections/aging")
        assert aging.status_code == 200, aging.text
        assert "buckets" in aging.json()

    def test_audit_search(self, client, db_session):
        _seed(db_session)
        rows = client.get(f"{_BASE}/audit?event_type=admin_cockpit_test").json()
        assert len(rows) >= 1
        assert rows[0]["event_type"] == "admin_cockpit_test"

    def test_requires_admin_or_staff(self, client):
        # Swap to a non-privileged user → the router-level gate returns 403.
        client.app.dependency_overrides[get_current_user] = _non_privileged
        assert client.get(f"{_BASE}/dashboard/overview").status_code == 403
        assert client.get(f"{_BASE}/applications").status_code == 403
        # audit is admin-only too
        assert client.get(f"{_BASE}/audit").status_code == 403
