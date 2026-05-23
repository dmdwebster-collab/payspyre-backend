"""HTTP-layer integration tests for the credit_products admin API (P3.5).

Mirrors `tests/test_credit_products_service.py` but exercises the wire — real
FastAPI TestClient against the live Supabase test DB. Closes the Section 7 gap:
"P3 service is unreachable over HTTP."

Auth: routes that mutate require role='admin'. We monkeypatch `require_roles`
to admit a synthetic admin user for these tests; this is the same pattern used
in `tests/test_credit_products_service.py` to avoid coupling P3.5 tests to the
full auth pipeline (real auth is exercised in tests/test_auth.py).
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.endpoints import credit_products as endpoint_module
from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.main import app


# ---------------------------------------------------------------------------
# Reusable verification_matrix / pricing_config (mirrors service test)
# ---------------------------------------------------------------------------

_VALID_MATRIX = {
    "identity": {
        "required": True,
        "methods": ["email_otp", "id_doc_scan"],
        "min_confidence": 0.85,
    },
    "income": {
        "required": True,
        "methods": ["bank_link", "t4"],
        "require_bank_link": False,
        "min_stated_income_cents": 4000000,
    },
    "bureau": {
        "soft_pull_required": True,
        "hard_pull_required": True,
        "min_score": 640,
        "max_score_age_days": 90,
    },
    "affordability": {
        "max_dti": 0.42,
        "min_payment_to_income_ratio": 0.20,
        "max_loan_to_income_ratio": 2.5,
    },
}

_VALID_PRICING = {
    "term_options": [24, 36, 48],
    "apr_range": [8.99, 24.99],
    "origination_fee_pct": 0.02,
}


def _make_payload(suffix: str | None = None) -> dict:
    s = suffix or uuid.uuid4().hex[:8]
    return {
        "code": f"api_product_{s}",
        "name": f"API Product {s}",
        "vertical": "dental",
        "status": "active",
        "min_amount_cents": 500000,
        "max_amount_cents": 5000000,
        "currency": "CAD",
        "verification_matrix": _VALID_MATRIX,
        "decision_ruleset": f"api_ruleset_{s}.yaml",
        "pricing_config": _VALID_PRICING,
        "funding_source": "payspyre_capital",
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_client(db_session: Session):
    """TestClient with auth dependencies overridden to a synthetic admin user."""
    fake_admin = type("U", (), {"id": uuid.uuid4(), "email": "admin@payspyre.test", "role": "admin"})()

    def _override_user():
        return fake_admin

    # require_roles returns an inner role_checker function; override it to a no-op.
    def _override_role_checker():
        return fake_admin

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = _override_user

    # require_roles is built dynamically per-call, so we override at the route
    # dependency layer by patching the role_checker that the routes registered.
    # FastAPI stores the actual Depends() callable, so the cleanest approach is
    # to monkeypatch require_roles' returned callable. The endpoint declared
    # Depends(require_roles("admin")) at import time — that closure is what we
    # must override. We do this via app.dependency_overrides keyed on the
    # callable object.
    for route in app.routes:
        if getattr(route, "path", "").startswith("/api/v1/credit-products"):
            for dep in getattr(route, "dependencies", []):
                app.dependency_overrides[dep.dependency] = _override_role_checker

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture
def read_only_client(db_session: Session):
    """TestClient authenticated as a non-admin (read-only)."""
    fake_user = type("U", (), {"id": uuid.uuid4(), "email": "user@payspyre.test", "role": "patient"})()

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: fake_user

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /api/v1/credit-products
# ---------------------------------------------------------------------------


class TestCreateOverHTTP:
    def test_create_returns_201_with_product(self, admin_client: TestClient):
        payload = _make_payload()
        response = admin_client.post("/api/v1/credit-products", json=payload)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["code"] == payload["code"]
        assert body["status"] == "active"
        assert body["version"] == 1
        assert "id" in body
        assert "created_at" in body

    def test_create_with_invalid_matrix_returns_422(self, admin_client: TestClient):
        payload = _make_payload()
        payload["verification_matrix"] = {"identity": {"required": "not_a_bool"}}  # type mismatch
        response = admin_client.post("/api/v1/credit-products", json=payload)
        assert response.status_code == 422, response.text
        assert "verification_matrix" in response.json()["detail"]

    def test_create_duplicate_code_returns_400(self, admin_client: TestClient):
        payload = _make_payload(suffix="dup_test")
        first = admin_client.post("/api/v1/credit-products", json=payload)
        assert first.status_code == 201
        second = admin_client.post("/api/v1/credit-products", json=payload)
        assert second.status_code == 400
        assert "already exists" in second.json()["detail"]


# ---------------------------------------------------------------------------
# GET /api/v1/credit-products
# ---------------------------------------------------------------------------


class TestListAndGet:
    def test_list_active_only(self, admin_client: TestClient):
        admin_client.post("/api/v1/credit-products", json=_make_payload())
        response = admin_client.get("/api/v1/credit-products")
        assert response.status_code == 200
        assert isinstance(response.json(), list)
        assert all(p["status"] == "active" for p in response.json())

    def test_list_include_archived(self, admin_client: TestClient):
        created = admin_client.post("/api/v1/credit-products", json=_make_payload()).json()
        admin_client.delete(f"/api/v1/credit-products/{created['id']}")
        response = admin_client.get("/api/v1/credit-products?active_only=false")
        assert response.status_code == 200
        statuses = {p["status"] for p in response.json()}
        assert "archived" in statuses

    def test_get_by_id_404_for_unknown(self, admin_client: TestClient):
        response = admin_client.get(f"/api/v1/credit-products/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_get_by_code_returns_product(self, admin_client: TestClient):
        payload = _make_payload()
        admin_client.post("/api/v1/credit-products", json=payload)
        response = admin_client.get(f"/api/v1/credit-products/by-code/{payload['code']}")
        assert response.status_code == 200
        assert response.json()["code"] == payload["code"]


# ---------------------------------------------------------------------------
# PATCH /api/v1/credit-products/{id}
# ---------------------------------------------------------------------------


class TestUpdateOverHTTP:
    def test_patch_updates_fields_and_bumps_version(self, admin_client: TestClient):
        created = admin_client.post("/api/v1/credit-products", json=_make_payload()).json()
        response = admin_client.patch(
            f"/api/v1/credit-products/{created['id']}",
            json={"name": "Renamed Product", "max_amount_cents": 7500000},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["name"] == "Renamed Product"
        assert body["max_amount_cents"] == 7500000
        assert body["version"] == created["version"] + 1

    def test_patch_unknown_id_returns_404(self, admin_client: TestClient):
        response = admin_client.patch(
            f"/api/v1/credit-products/{uuid.uuid4()}",
            json={"name": "Will not stick"},
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/v1/credit-products/{id}  (soft delete / archive)
# ---------------------------------------------------------------------------


class TestDeactivateOverHTTP:
    def test_delete_sets_status_archived(self, admin_client: TestClient):
        created = admin_client.post("/api/v1/credit-products", json=_make_payload()).json()
        response = admin_client.delete(f"/api/v1/credit-products/{created['id']}")
        assert response.status_code == 200
        assert response.json()["status"] == "archived"

    def test_delete_unknown_id_returns_404(self, admin_client: TestClient):
        response = admin_client.delete(f"/api/v1/credit-products/{uuid.uuid4()}")
        assert response.status_code == 404
