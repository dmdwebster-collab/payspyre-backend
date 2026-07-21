"""Tests for clinic data-scoping — P-clinic scoping wave.

Clinics ARE the existing ``vendors`` table (spec §13). These tests verify, with
pure logic + ``MagicMock`` sessions (NO live DB, NO network), that:

  * ``resolve_clinic_vendor_id`` returns the membership's ``vendor_id``, or
    ``None`` when the user has no clinic membership.
  * ``get_current_clinic_user`` builds a ``ClinicPrincipal`` for a member and
    raises 403 for a non-member.
  * the application list/summary query filters by ``vendor_id``.
  * the financing-link endpoint stamps the new application with the caller's
    ``vendor_id``.

Run (only the clinic files — the full suite hits a shared remote DB):
    python -m pytest tests/test_clinic_scoping.py tests/test_clinic_api.py -p no:warnings -q
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.api.clinic.v1 import deps as clinic_deps
from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.api.clinic.v1.endpoints import applications as apps_endpoint
from app.services import clinic_membership as membership_svc


# --- resolve_clinic_vendor_id ----------------------------------------------


class TestResolveClinicVendorId:
    def test_returns_membership_vendor_id(self):
        vendor_id = uuid.uuid4()
        user_id = uuid.uuid4()
        membership = SimpleNamespace(vendor_id=vendor_id)

        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = (
            membership
        )

        out = membership_svc.resolve_clinic_vendor_id(db, user_id)
        assert out == vendor_id

    def test_returns_none_when_no_membership(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        out = membership_svc.resolve_clinic_vendor_id(db, uuid.uuid4())
        assert out is None


# --- get_current_clinic_user -----------------------------------------------


class TestGetCurrentClinicUser:
    def test_resolves_principal_for_member(self, monkeypatch):
        vendor_id = uuid.uuid4()
        user = SimpleNamespace(id=uuid.uuid4())
        membership = SimpleNamespace(vendor_id=vendor_id, role="staff")
        monkeypatch.setattr(
            clinic_deps, "resolve_clinic_membership", lambda db, user_id: membership
        )

        principal = get_current_clinic_user(user=user, db=MagicMock())
        assert isinstance(principal, ClinicPrincipal)
        assert principal.vendor_id == vendor_id
        assert principal.role == "staff"
        assert principal.user is user
        assert principal.user_id == user.id

    def test_membership_role_lands_on_principal(self, monkeypatch):
        # WS-I: the clinic-side role gates rate-editing on vendor intake.
        membership = SimpleNamespace(vendor_id=uuid.uuid4(), role="admin")
        monkeypatch.setattr(
            clinic_deps, "resolve_clinic_membership", lambda db, user_id: membership
        )
        principal = get_current_clinic_user(
            user=SimpleNamespace(id=uuid.uuid4()), db=MagicMock()
        )
        assert principal.role == "admin"

    def test_null_role_falls_back_to_staff(self, monkeypatch):
        membership = SimpleNamespace(vendor_id=uuid.uuid4(), role=None)
        monkeypatch.setattr(
            clinic_deps, "resolve_clinic_membership", lambda db, user_id: membership
        )
        principal = get_current_clinic_user(
            user=SimpleNamespace(id=uuid.uuid4()), db=MagicMock()
        )
        assert principal.role == "staff"

    def test_403_when_user_has_no_clinic(self, monkeypatch):
        user = SimpleNamespace(id=uuid.uuid4())
        monkeypatch.setattr(
            clinic_deps, "resolve_clinic_membership", lambda db, user_id: None
        )

        with pytest.raises(HTTPException) as exc:
            get_current_clinic_user(user=user, db=MagicMock())
        assert exc.value.status_code == 403

    def test_passes_user_id_through_to_resolver(self, monkeypatch):
        user = SimpleNamespace(id=uuid.uuid4())
        seen = {}

        def _fake(db, user_id):
            seen["user_id"] = user_id
            return SimpleNamespace(vendor_id=uuid.uuid4(), role="staff")

        monkeypatch.setattr(clinic_deps, "resolve_clinic_membership", _fake)
        get_current_clinic_user(user=user, db=MagicMock())
        assert seen["user_id"] == user.id


# --- query_applications is vendor-scoped ------------------------------------


class TestQueryApplicationsScoping:
    def test_filters_by_vendor_id(self):
        vendor_id = uuid.uuid4()
        db = MagicMock()
        (
            db.query.return_value.filter.return_value.options.return_value
            .order_by.return_value.limit.return_value.all
        ).return_value = ["row"]

        rows = apps_endpoint.query_applications(db, vendor_id)
        assert rows == ["row"]

        # A .filter(...) was applied (the vendor scope) — the bare-query path
        # (.options first) must NOT be used.
        assert db.query.return_value.filter.called
        # The vendor_id binds into the filter expression.
        filter_arg = db.query.return_value.filter.call_args.args[0]
        assert str(vendor_id) in str(filter_arg.right.value)


# --- financing-link stamps vendor_id ----------------------------------------


class TestFinancingLinkStampsVendor:
    def test_create_application_gets_vendor_id(self, monkeypatch):
        from app.api.clinic.v1.endpoints import financing_links as fl

        vendor_id = uuid.uuid4()
        product = SimpleNamespace(id=uuid.uuid4(), status="active", name="Dental")
        patient = SimpleNamespace(id=uuid.uuid4())

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = product
        monkeypatch.setattr(fl, "_find_or_create_patient", lambda _db, _body: patient)

        created = SimpleNamespace(id=uuid.uuid4())
        orchestrator = MagicMock()
        orchestrator.create_application.return_value = created

        body = SimpleNamespace(
            credit_product_id=product.id,
            amount_cents=180_000,
            patient_name="Jordan Lee",
            patient_contact="jordan@example.com",
        )
        principal = ClinicPrincipal(user=SimpleNamespace(id=uuid.uuid4()), vendor_id=vendor_id)

        fl.create_financing_link(
            body=body, db=db, orchestrator=orchestrator, principal=principal
        )

        kwargs = orchestrator.create_application.call_args.kwargs
        assert kwargs["vendor_id"] == vendor_id
        assert kwargs["requested_amount_source"] == "clinic"


@pytest.fixture(autouse=True)
def _customer_not_blocked(monkeypatch):
    """WS-G added a customer lock/block gate to the origination entry points.
    These endpoint unit tests use blanket mock sessions where every query
    returns the same stubbed row, which the new block check would misread as
    "blocked". The customer under test is not blocked; the block gate itself is
    covered in tests/test_crm_parity_g.py."""
    monkeypatch.setattr(
        "app.services.customer_blocks.is_blocked", lambda *a, **k: False
    )
