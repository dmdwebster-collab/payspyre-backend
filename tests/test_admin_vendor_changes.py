"""Tests for the admin-side vendor profile change-request review endpoints.

Mirrors the no-live-DB style of test_clinic_api.py / test_account_profile.py: a
fresh FastAPI app mounting only the admin router, with ``get_db`` and
``get_current_user`` overridden by fakes. The ``db.query`` fake dispatches on the
model so approve (which reads the change request AND the vendor) works.

Covered:
- approve applies ONLY whitelisted changes to the vendor row, stamps review audit
- approve re-enforces the whitelist (a stray admin-only key is skipped, not applied)
- reject does NOT mutate the vendor row
- already-decided request → 409 (no double-apply)
- missing request → 404
- list filters by status
- admin role required (non-admin → 403; no auth → 401/403)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.endpoints import admin_vendor_changes as admin_ep
from app.api.v1.endpoints.admin_vendor_changes import router as admin_router
from app.core.auth import get_current_user
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.profile_change_request import PlatformVendorProfileChangeRequest

_BASE = "/admin/vendor-profile-change-requests"


# --- fakes -----------------------------------------------------------------


def _admin_user():
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="admin@payspyre.test",
        roles=[SimpleNamespace(role=SimpleNamespace(name="admin"))],
    )


def _non_admin_user():
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="staff@payspyre.test",
        roles=[SimpleNamespace(role=SimpleNamespace(name="staff"))],
    )


def _change_request(status="pending", requested_changes=None, vendor_id=None):
    return PlatformVendorProfileChangeRequest(
        id=uuid.uuid4(),
        vendor_id=vendor_id or uuid.uuid4(),
        requested_changes=requested_changes or {"contact_name": "New Name", "city": "Kelowna"},
        status=status,
        note="please update",
        created_at=datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc),
    )


def _vendor(vendor_id):
    return SimpleNamespace(
        id=vendor_id,
        contact_name="Old Name",
        city="Vancouver",
        # an admin-only attr that must NEVER be touched by approval
        status="active",
    )


def _fake_db(*, cr=None, cr_list=None, vendor=None):
    """A MagicMock Session whose ``query(Model)`` resolves to preset objects."""
    db = MagicMock()

    def _query(model):
        q = MagicMock()
        if model is PlatformVendorProfileChangeRequest:
            q.filter.return_value.first.return_value = cr
            (
                q.filter.return_value.filter.return_value.order_by.return_value
                .limit.return_value.all
            ).return_value = cr_list or []
            (
                q.filter.return_value.order_by.return_value.limit.return_value.all
            ).return_value = cr_list or []
            q.order_by.return_value.limit.return_value.all.return_value = cr_list or []
        elif model is Vendor:
            q.filter.return_value.first.return_value = vendor
        return q

    db.query.side_effect = _query
    return db


@pytest.fixture
def app_under_test():
    app = FastAPI()
    app.include_router(admin_router, prefix=_BASE)
    app.dependency_overrides[get_current_user] = _admin_user
    yield app
    app.dependency_overrides.clear()


# --- approve ---------------------------------------------------------------


class TestApprove:
    def test_applies_whitelisted_changes_to_vendor(self, app_under_test):
        vid = uuid.uuid4()
        cr = _change_request(requested_changes={"contact_name": "New Name", "city": "Kelowna"}, vendor_id=vid)
        vendor = _vendor(vid)
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(cr=cr, vendor=vendor)

        resp = TestClient(app_under_test).post(f"{_BASE}/{cr.id}/approve", json={"review_note": "ok"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "approved"
        assert body["reviewed_by"]  # stamped
        assert body["review_note"] == "ok"
        # vendor row actually mutated for whitelisted keys
        assert vendor.contact_name == "New Name"
        assert vendor.city == "Kelowna"

    def test_whitelist_reenforced_skips_admin_only_key(self, app_under_test):
        vid = uuid.uuid4()
        # A poisoned row containing an admin-only key — must be skipped on apply.
        cr = _change_request(
            requested_changes={"contact_name": "Legit", "status": "terminated"}, vendor_id=vid
        )
        vendor = _vendor(vid)
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(cr=cr, vendor=vendor)

        resp = TestClient(app_under_test).post(f"{_BASE}/{cr.id}/approve")
        assert resp.status_code == 200, resp.text
        assert vendor.contact_name == "Legit"        # whitelisted → applied
        assert vendor.status == "active"             # admin-only → untouched

    def test_already_decided_is_409(self, app_under_test):
        cr = _change_request(status="approved")
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(cr=cr, vendor=_vendor(cr.vendor_id))
        resp = TestClient(app_under_test).post(f"{_BASE}/{cr.id}/approve")
        assert resp.status_code == 409, resp.text

    def test_missing_request_is_404(self, app_under_test):
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(cr=None)
        resp = TestClient(app_under_test).post(f"{_BASE}/{uuid.uuid4()}/approve")
        assert resp.status_code == 404, resp.text


# --- reject ----------------------------------------------------------------


class TestReject:
    def test_reject_does_not_touch_vendor(self, app_under_test):
        cr = _change_request()
        # No vendor wired — reject must never query/mutate the vendor row.
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(cr=cr, vendor=None)
        resp = TestClient(app_under_test).post(f"{_BASE}/{cr.id}/reject", json={"review_note": "incomplete"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "rejected"
        assert body["review_note"] == "incomplete"

    def test_reject_already_decided_409(self, app_under_test):
        cr = _change_request(status="rejected")
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(cr=cr)
        resp = TestClient(app_under_test).post(f"{_BASE}/{cr.id}/reject")
        assert resp.status_code == 409, resp.text


# --- list + detail ---------------------------------------------------------


class TestList:
    def test_lists_with_status_filter(self, app_under_test):
        rows = [_change_request(), _change_request()]
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(cr_list=rows)
        resp = TestClient(app_under_test).get(f"{_BASE}?status=pending")
        assert resp.status_code == 200, resp.text
        assert len(resp.json()) == 2

    def test_detail(self, app_under_test):
        cr = _change_request()
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(cr=cr)
        resp = TestClient(app_under_test).get(f"{_BASE}/{cr.id}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == str(cr.id)


# --- authz -----------------------------------------------------------------


class TestAuthz:
    def test_non_admin_forbidden(self, app_under_test):
        app_under_test.dependency_overrides[get_current_user] = _non_admin_user
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(cr_list=[])
        resp = TestClient(app_under_test).get(_BASE)
        assert resp.status_code == 403, resp.text

    def test_requires_auth(self, app_under_test):
        app_under_test.dependency_overrides.pop(get_current_user, None)
        app_under_test.dependency_overrides[get_db] = lambda: _fake_db(cr_list=[])
        resp = TestClient(app_under_test).get(_BASE)
        assert resp.status_code in (401, 403), resp.text
