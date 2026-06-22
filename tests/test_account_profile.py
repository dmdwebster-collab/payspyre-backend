"""Tests for the clinic account self-service API (spec §3.6) — Phase 4.

These tests follow the mocked-DB pattern in ``tests/test_clinic_api.py``: a fresh
``FastAPI`` app mounts only the account router, and ``get_db`` /
``get_current_clinic_user`` are overridden so no live database is required.

Asserted invariants:
  - GET /account/profile returns the CALLER's vendor only (scoped to vendor_id).
  - POST with whitelisted fields creates a PENDING change-request row and does
    NOT mutate the vendor row.
  - POST with a forbidden/admin-only field → 422.
  - Another vendor cannot see this vendor's profile (scoping).

Run (only this file):
    python -m pytest tests/test_account_profile.py -x -q
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
from app.api.clinic.v1.endpoints import account as account_endpoint
from app.db.base import get_db
from app.models.platform.profile_change_request import (
    PlatformVendorProfileChangeRequest,
)

_BASE = "/api/clinic/v1"
_VENDOR_ID = uuid.uuid4()
_OTHER_VENDOR_ID = uuid.uuid4()


# --- fakes -----------------------------------------------------------------


def _fake_vendor(vendor_id=_VENDOR_ID, contact_name="Dr. Jordan Lee", email="clinic@example.com"):
    return SimpleNamespace(
        id=vendor_id,
        business_name="Bright Smiles Dental",
        dba_name="Bright Smiles",
        business_type="corporation",
        contact_name=contact_name,
        email=email,
        phone="+15555550100",
        address_line1="123 Main St",
        address_line2="Suite 5",
        city="Kelowna",
        province="BC",
        postal_code="V1Y1A1",
        license_number="LIC-001",
        license_expiry=datetime(2027, 1, 1, tzinfo=timezone.utc),
        status="active",
        compliance_score=87.50,
    )


@pytest.fixture
def app_under_test():
    app = FastAPI()
    app.include_router(account_endpoint.router, prefix="/api/clinic/v1")
    app.dependency_overrides[get_current_clinic_user] = lambda: ClinicPrincipal(
        user=SimpleNamespace(id=uuid.uuid4()), vendor_id=_VENDOR_ID
    )
    yield app
    app.dependency_overrides.clear()


# --- pure shaping ----------------------------------------------------------


def test_to_vendor_profile_shape():
    out = account_endpoint.to_vendor_profile(_fake_vendor())
    assert out.id == _VENDOR_ID
    assert out.business_name == "Bright Smiles Dental"
    assert out.address.line1 == "123 Main St"
    assert out.address.line2 == "Suite 5"
    assert out.compliance_score == 87.50
    assert out.license_expiry.startswith("2027-01-01")


def test_whitelist_matches_spec():
    assert account_endpoint.WHITELIST_FIELDS == frozenset(
        {
            "contact_name",
            "email",
            "phone",
            "address_line1",
            "address_line2",
            "city",
            "province",
            "postal_code",
        }
    )
    # The never-requestable set must be disjoint from the whitelist.
    assert not (
        account_endpoint.NEVER_REQUESTABLE_FIELDS & account_endpoint.WHITELIST_FIELDS
    )


# --- GET /account/profile --------------------------------------------------


class TestGetProfile:
    def _wire_db(self, app_under_test, vendor):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = vendor
        app_under_test.dependency_overrides[get_db] = lambda: db
        return db

    def test_returns_callers_vendor(self, app_under_test):
        self._wire_db(app_under_test, _fake_vendor())
        client = TestClient(app_under_test)
        resp = client.get(f"{_BASE}/account/profile")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == str(_VENDOR_ID)
        assert body["contact_name"] == "Dr. Jordan Lee"
        assert body["address"]["city"] == "Kelowna"
        assert body["status"] == "active"
        assert body["license_number"] == "LIC-001"

    def test_scopes_query_to_caller_vendor_id(self, app_under_test):
        # The query must filter on the principal's vendor_id. We assert the row
        # returned belongs to the caller (the endpoint never returns another
        # vendor because it filters by principal.vendor_id then .first()).
        db = self._wire_db(app_under_test, _fake_vendor())
        client = TestClient(app_under_test)
        resp = client.get(f"{_BASE}/account/profile")
        assert resp.status_code == 200
        # A filter was applied (scoping), not an unfiltered fetch.
        assert db.query.return_value.filter.called

    def test_other_vendor_cannot_see_this_profile(self, app_under_test):
        # Caller is _OTHER_VENDOR_ID. The DB, scoped to that id, has no matching
        # vendor (this vendor's row is not visible) -> 404, never another's data.
        app_under_test.dependency_overrides[get_current_clinic_user] = (
            lambda: ClinicPrincipal(
                user=SimpleNamespace(id=uuid.uuid4()), vendor_id=_OTHER_VENDOR_ID
            )
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        app_under_test.dependency_overrides[get_db] = lambda: db
        client = TestClient(app_under_test)
        resp = client.get(f"{_BASE}/account/profile")
        assert resp.status_code == 404, resp.text


# --- POST /account/profile/change-requests ---------------------------------


class TestCreateChangeRequest:
    def _wire_db(self, app_under_test):
        """Mock DB whose add/commit/refresh simulate the persisted row.

        ``refresh`` is what populates server-defaulted columns (id, status,
        created_at) on the SQLAlchemy instance; we emulate that so the response
        shaping has values to read.
        """
        db = MagicMock()
        captured = {}

        def _add(obj):
            captured["obj"] = obj

        def _refresh(obj):
            obj.id = uuid.uuid4()
            obj.created_at = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
            if obj.status is None:
                obj.status = "pending"

        db.add.side_effect = _add
        db.refresh.side_effect = _refresh
        app_under_test.dependency_overrides[get_db] = lambda: db
        return db, captured

    def test_whitelisted_fields_create_pending_row(self, app_under_test):
        db, captured = self._wire_db(app_under_test)
        client = TestClient(app_under_test)
        resp = client.post(
            f"{_BASE}/account/profile/change-requests",
            json={
                "contact_name": "Dr. New Name",
                "phone": "+15555550199",
                "note": "Updated front-desk contact",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "pending"
        assert body["vendor_id"] == str(_VENDOR_ID)
        assert body["requested_changes"] == {
            "contact_name": "Dr. New Name",
            "phone": "+15555550199",
        }
        # note is stored separately, NOT inside requested_changes.
        assert "note" not in body["requested_changes"]
        assert body["note"] == "Updated front-desk contact"

        # A pending change-request row was persisted, scoped to the caller.
        obj = captured["obj"]
        assert isinstance(obj, PlatformVendorProfileChangeRequest)
        assert obj.vendor_id == _VENDOR_ID
        assert obj.status == "pending"
        db.commit.assert_called_once()

    def test_does_not_mutate_vendor(self, app_under_test):
        db, captured = self._wire_db(app_under_test)
        client = TestClient(app_under_test)
        resp = client.post(
            f"{_BASE}/account/profile/change-requests",
            json={"email": "new@example.com"},
        )
        assert resp.status_code == 201, resp.text
        # Only a change-request row is added; no Vendor instance is ever added or
        # mutated/merged. (The endpoint never queries or writes vendors.)
        added_types = {type(c.args[0]) for c in db.add.call_args_list}
        assert added_types == {PlatformVendorProfileChangeRequest}
        assert not db.merge.called
        # The endpoint must not have loaded a vendor row to mutate it.
        assert not db.query.called

    def test_forbidden_field_is_422(self, app_under_test):
        self._wire_db(app_under_test)
        client = TestClient(app_under_test)
        # business_name is admin-only / never-requestable.
        resp = client.post(
            f"{_BASE}/account/profile/change-requests",
            json={"contact_name": "ok", "business_name": "Sneaky Rename Inc"},
        )
        assert resp.status_code == 422, resp.text

    def test_compliance_score_forbidden_is_422(self, app_under_test):
        self._wire_db(app_under_test)
        client = TestClient(app_under_test)
        resp = client.post(
            f"{_BASE}/account/profile/change-requests",
            json={"compliance_score": "100"},
        )
        assert resp.status_code == 422, resp.text

    def test_empty_body_is_422(self, app_under_test):
        self._wire_db(app_under_test)
        client = TestClient(app_under_test)
        resp = client.post(
            f"{_BASE}/account/profile/change-requests",
            json={"note": "I changed my mind about nothing"},
        )
        assert resp.status_code == 422, resp.text
