"""Tests for the composed clinic dashboard overview endpoint.

GET /api/clinic/v1/dashboard/overview stitches four already-tested, independently
scoped block helpers (applications / loan_book / payments / marketplace) plus the
vendor row into one payload. These tests exercise the COMPOSITION — that each
block lands under the right key, the window defaults/overrides correctly, and the
vendor-not-found path 404s — by patching the block helpers (covered by their own
suites) and faking the DB, mirroring the no-live-DB style of test_clinic_api.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.api.clinic.v1.endpoints import dashboard_overview as ov
from app.api.clinic.v1.router import clinic_router
from app.db.base import get_db

_BASE = "/api/clinic/v1"
_VENDOR_ID = uuid.uuid4()

_APPS = {"total": 3, "approved": 2, "rejected": 1, "in_review": 0, "started": 0,
         "approval_rate": 0.6667, "avg_requested_amount_cents": 1_000_000,
         "total_requested_amount_cents": 3_000_000}
_LOANS = {"active_count": 1, "active_principal_balance_cents": 500_000,
          "disbursed_count": 1, "disbursed_amount_cents": 600_000,
          "paid_off_count": 0, "delinquent_count": 0}
_PAYMENTS = {"on_time_rate": 1.0, "late_count": 0, "delinquent_count": 0,
             "collected_cents": 100_000}
_MARKET = {"leads_available": 4, "interests_expressed": 2,
           "appointments_booked": 1, "charges_cents": 25_000}


def _fake_vendor():
    return SimpleNamespace(id=_VENDOR_ID, business_name="Bright Smiles Dental", status="active")


@pytest.fixture
def app_under_test(monkeypatch):
    app = FastAPI()
    app.include_router(clinic_router, prefix="/api/clinic/v1")
    app.dependency_overrides[get_current_clinic_user] = lambda: ClinicPrincipal(
        user=SimpleNamespace(id=uuid.uuid4()), vendor_id=_VENDOR_ID
    )
    # Patch the four block helpers — each is verified by its own test suite; here
    # we only assert the overview routes their output to the right keys.
    monkeypatch.setattr(ov, "applications_block", lambda db, vid, frm, to: dict(_APPS))
    monkeypatch.setattr(ov, "loan_book_block", lambda db, vid: dict(_LOANS))
    monkeypatch.setattr(ov, "payments_block", lambda db, vid, frm, to: dict(_PAYMENTS))
    monkeypatch.setattr(ov, "marketplace_block", lambda db, vid, frm, to: dict(_MARKET))
    yield app
    app.dependency_overrides.clear()


def _wire_vendor(app, vendor):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = vendor
    app.dependency_overrides[get_db] = lambda: db
    return db


class TestOverview:
    def test_composes_all_blocks(self, app_under_test):
        _wire_vendor(app_under_test, _fake_vendor())
        body = TestClient(app_under_test).get(f"{_BASE}/dashboard/overview").json()
        assert body["vendor"] == {
            "id": str(_VENDOR_ID), "business_name": "Bright Smiles Dental", "status": "active"
        }
        assert body["applications"] == _APPS
        assert body["loan_book"] == _LOANS
        assert body["payments"] == _PAYMENTS
        assert body["marketplace"] == _MARKET
        assert set(body["window"]) == {"from", "to"}

    def test_default_window_is_30_days(self, app_under_test):
        _wire_vendor(app_under_test, _fake_vendor())
        body = TestClient(app_under_test).get(f"{_BASE}/dashboard/overview").json()
        frm = datetime.fromisoformat(body["window"]["from"])
        to = datetime.fromisoformat(body["window"]["to"])
        assert 29 <= (to - frm).days <= 30

    def test_explicit_window_is_echoed(self, app_under_test):
        _wire_vendor(app_under_test, _fake_vendor())
        params = {"from": "2026-01-01T00:00:00+00:00", "to": "2026-02-01T00:00:00+00:00"}
        body = TestClient(app_under_test).get(
            f"{_BASE}/dashboard/overview", params=params
        ).json()
        assert datetime.fromisoformat(body["window"]["from"]) == datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert datetime.fromisoformat(body["window"]["to"]) == datetime(2026, 2, 1, tzinfo=timezone.utc)

    def test_vendor_not_found_404(self, app_under_test):
        _wire_vendor(app_under_test, None)
        resp = TestClient(app_under_test).get(f"{_BASE}/dashboard/overview")
        assert resp.status_code == 404, resp.text

    def test_requires_auth(self, app_under_test):
        app_under_test.dependency_overrides.pop(get_current_clinic_user, None)
        app_under_test.dependency_overrides[get_db] = lambda: MagicMock()
        resp = TestClient(app_under_test).get(f"{_BASE}/dashboard/overview")
        assert resp.status_code in (401, 403)
