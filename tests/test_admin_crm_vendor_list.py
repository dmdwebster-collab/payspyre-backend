"""DB-FREE tests for the vendor directory list endpoint (GET /admin/crm/vendors).

Same no-live-DB style as test_admin_vendor_changes.py: a fresh FastAPI app
mounting only the admin CRM router, with ``get_db`` and ``get_current_user``
overridden by fakes. The ``db.query(Vendor)`` fake records the filter/order/
paging chain and returns preset vendor rows + a preset count, so we can assert
the endpoint's search wiring, status validation, pagination echo, and RBAC
without a database.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.endpoints.admin_crm_vendors import router as crm_router
from app.core.auth import get_current_user
from app.db.base import get_db
from app.models.loan import Vendor

_BASE = "/admin/crm"


# --- fakes -----------------------------------------------------------------


def _admin_user():
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="admin@payspyre.test",
        roles=[SimpleNamespace(role=SimpleNamespace(name="admin"))],
    )


def _staff_user():
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="staff@payspyre.test",
        roles=[SimpleNamespace(role=SimpleNamespace(name="staff"))],
    )


def _outsider_user():
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="borrower@payspyre.test",
        roles=[SimpleNamespace(role=SimpleNamespace(name="borrower"))],
    )


def _vendor(business_name="Kelowna Dental Centre", dba_name=None, status="active"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        business_name=business_name,
        dba_name=dba_name,
        status=status,
        created_at=datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc),
    )


class _RecordingQuery:
    """Records .filter()/.order_by()/.offset()/.limit() calls, returns preset
    rows and count. filter() is variadic and chainable, matching SQLAlchemy."""

    def __init__(self, rows, count, spy):
        self._rows = rows
        self._count = count
        self._spy = spy

    def filter(self, *criteria):
        self._spy["filter_calls"] += 1
        return self

    def order_by(self, *cols):
        self._spy["ordered"] = True
        return self

    def offset(self, n):
        self._spy["offset"] = n
        return self

    def limit(self, n):
        self._spy["limit"] = n
        return self

    def count(self):
        return self._count

    def all(self):
        return self._rows


def _fake_db(*, rows=None, count=None, spy=None):
    db = MagicMock()
    rows = rows or []
    count = count if count is not None else len(rows)

    def _query(model):
        assert model is Vendor
        return _RecordingQuery(rows, count, spy)

    db.query.side_effect = _query
    return db


@pytest.fixture
def app_under_test():
    app = FastAPI()
    app.include_router(crm_router, prefix=_BASE)
    app.dependency_overrides[get_current_user] = _admin_user
    yield app
    app.dependency_overrides.clear()


# --- happy path ------------------------------------------------------------


class TestList:
    def test_returns_shaped_rows_and_count(self, app_under_test):
        spy = {"filter_calls": 0}
        rows = [
            _vendor("Kelowna Dental Centre", dba_name="KDC"),
            _vendor("New Teeth Today", status="pending"),
        ]
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(
            rows=rows, count=7, spy=spy
        )
        resp = TestClient(app_under_test).get(f"{_BASE}/vendors")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["count"] == 7  # total ignores paging
        assert body["limit"] == 50 and body["offset"] == 0
        assert [v["business_name"] for v in body["vendors"]] == [
            "Kelowna Dental Centre",
            "New Teeth Today",
        ]
        first = body["vendors"][0]
        assert set(first) == {"id", "business_name", "dba_name", "status"}
        assert first["dba_name"] == "KDC"
        assert spy["filter_calls"] == 0  # no q/status → no filter applied

    def test_search_applies_filter(self, app_under_test):
        spy = {"filter_calls": 0}
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(
            rows=[_vendor("Kelowna Dental Centre")], count=1, spy=spy
        )
        resp = TestClient(app_under_test).get(f"{_BASE}/vendors?q=kelowna")
        assert resp.status_code == 200, resp.text
        assert spy["filter_calls"] == 1  # ILIKE filter applied
        assert len(resp.json()["vendors"]) == 1

    def test_blank_q_is_ignored(self, app_under_test):
        spy = {"filter_calls": 0}
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(
            rows=[], count=0, spy=spy
        )
        resp = TestClient(app_under_test).get(f"{_BASE}/vendors?q=%20%20")
        assert resp.status_code == 200, resp.text
        assert spy["filter_calls"] == 0  # whitespace-only q → no filter

    def test_status_filter_applies(self, app_under_test):
        spy = {"filter_calls": 0}
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(
            rows=[_vendor(status="suspended")], count=1, spy=spy
        )
        resp = TestClient(app_under_test).get(f"{_BASE}/vendors?status=suspended")
        assert resp.status_code == 200, resp.text
        assert spy["filter_calls"] == 1

    def test_invalid_status_is_422(self, app_under_test):
        spy = {"filter_calls": 0}
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(spy=spy)
        resp = TestClient(app_under_test).get(f"{_BASE}/vendors?status=bogus")
        assert resp.status_code == 422, resp.text

    def test_pagination_echoed_and_passed_through(self, app_under_test):
        spy = {"filter_calls": 0}
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(
            rows=[], count=100, spy=spy
        )
        resp = TestClient(app_under_test).get(
            f"{_BASE}/vendors?limit=10&offset=20"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["limit"] == 10 and body["offset"] == 20
        assert body["count"] == 100
        assert spy["limit"] == 10 and spy["offset"] == 20 and spy["ordered"] is True

    def test_limit_bounds_enforced(self, app_under_test):
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(spy={"filter_calls": 0})
        assert (
            TestClient(app_under_test).get(f"{_BASE}/vendors?limit=0").status_code == 422
        )
        assert (
            TestClient(app_under_test).get(f"{_BASE}/vendors?limit=999").status_code
            == 422
        )
        assert (
            TestClient(app_under_test).get(f"{_BASE}/vendors?offset=-1").status_code
            == 422
        )


# --- authz -----------------------------------------------------------------


class TestAuthz:
    def test_staff_allowed(self, app_under_test):
        app_under_test.dependency_overrides[get_current_user] = _staff_user
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(
            rows=[], count=0, spy={"filter_calls": 0}
        )
        resp = TestClient(app_under_test).get(f"{_BASE}/vendors")
        assert resp.status_code == 200, resp.text

    def test_non_admin_non_staff_forbidden(self, app_under_test):
        app_under_test.dependency_overrides[get_current_user] = _outsider_user
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(
            spy={"filter_calls": 0}
        )
        resp = TestClient(app_under_test).get(f"{_BASE}/vendors")
        assert resp.status_code == 403, resp.text

    def test_requires_auth(self, app_under_test):
        app_under_test.dependency_overrides.pop(get_current_user, None)
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(
            spy={"filter_calls": 0}
        )
        resp = TestClient(app_under_test).get(f"{_BASE}/vendors")
        assert resp.status_code in (401, 403), resp.text
