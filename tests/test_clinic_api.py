"""Tests for the clinic (practice-facing) API — P-clinic.

These tests deliberately AVOID a live database. They exercise the endpoints
through a fresh ``FastAPI`` app that mounts only ``clinic_router``, with the
``get_db`` / ``get_current_user`` / ``get_orchestrator`` dependencies overridden
by ``MagicMock`` fakes. The pure response-shaping helpers are tested directly.

Run (only this file — the full suite hits a shared remote DB):
    python -m pytest tests/test_clinic_api.py -p no:warnings -q
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.clinic.v1 import deps as clinic_deps
from app.api.clinic.v1.endpoints import applications as apps_endpoint
from app.api.clinic.v1.endpoints import financing_links as fl_endpoint
from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.api.clinic.v1.router import clinic_router
from app.api.clinic.v1.status_map import to_clinic_status
from app.db.base import get_db

_BASE = "/api/clinic/v1"
_VENDOR_ID = uuid.uuid4()


# --- fakes -----------------------------------------------------------------


def _fake_product(name="Dental — Elective Care", status="active", currency="CAD"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        code="DENTAL_ELECTIVE",
        name=name,
        vertical="dental",
        status=status,
        min_amount_cents=50_000,
        max_amount_cents=3_000_000,
        currency=currency,
    )


def _fake_patient(first="Jordan", last="Lee", email="jordan@example.com", phone=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        legal_first_name=first,
        legal_last_name=last,
        email=email,
        phone_e164=phone,
    )


def _fake_application(status="approved", amount=1_800_000, patient=None, product=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        requested_amount_cents=amount,
        created_at=datetime(2026, 6, 18, 14, 32, tzinfo=timezone.utc),
        patient=patient or _fake_patient(),
        credit_product=product or _fake_product(),
    )


@pytest.fixture
def app_under_test():
    app = FastAPI()
    app.include_router(clinic_router, prefix="/api/clinic/v1")
    # Auth + clinic scoping: pretend a valid staff user that belongs to a clinic.
    app.dependency_overrides[get_current_clinic_user] = lambda: ClinicPrincipal(
        user=SimpleNamespace(id=uuid.uuid4()), vendor_id=_VENDOR_ID
    )
    yield app
    app.dependency_overrides.clear()


# --- pure helpers ----------------------------------------------------------


class TestStatusMap:
    def test_one_to_one(self):
        assert to_clinic_status("approved") == "approved"
        assert to_clinic_status("declined") == "declined"

    def test_under_review_is_manual_review(self):
        assert to_clinic_status("under_review") == "manual_review"

    def test_in_flight_collapse_to_started(self):
        for s in ("started", "verifying", "pre_qualified", "awaiting_hard_pull"):
            assert to_clinic_status(s) == "started"

    def test_dead_states_collapse_to_declined(self):
        assert to_clinic_status("withdrawn") == "declined"
        assert to_clinic_status("expired") == "declined"

    def test_unknown_defaults_to_started(self):
        assert to_clinic_status("brand_new_enum_value") == "started"


class TestShaping:
    def test_to_clinic_application(self):
        row = _fake_application(status="under_review", amount=650_000)
        out = apps_endpoint.to_clinic_application(row)
        assert out.patient_name == "Jordan Lee"
        assert out.patient_contact == "jordan@example.com"
        assert out.product_name == "Dental — Elective Care"
        assert out.amount_cents == 650_000
        assert out.currency == "CAD"
        assert out.status == "manual_review"

    def test_contact_falls_back_to_phone(self):
        patient = _fake_patient(email=None, phone="+15555550148")
        row = _fake_application(patient=patient)
        out = apps_endpoint.to_clinic_application(row)
        assert out.patient_contact == "+15555550148"

    def test_missing_patient_and_product_are_safe(self):
        row = _fake_application()
        row.patient = None
        row.credit_product = None
        out = apps_endpoint.to_clinic_application(row)
        assert out.patient_name == "Unknown patient"
        assert out.product_name == "Financing"
        assert out.currency == "CAD"

    def test_summary_from_status_counts_buckets(self):
        # (platform_status, decision_by, count) triples as the GROUP BY returns them.
        triples = [
            ("approved", "auto", 2),
            ("declined", "some-human-id", 1),
            ("under_review", None, 1),
            ("verifying", None, 1),
        ]
        summary = apps_endpoint._summary_from_status_counts(triples)
        assert summary.approved == 2
        assert summary.declined == 1
        assert summary.manual_review == 1
        assert summary.started == 1
        assert summary.total == 5

    def test_summary_counts_are_not_capped(self):
        # The whole point of the GROUP BY: counts reflect ALL rows, not a capped
        # fetch. A clinic with 5,000 approvals must report 5,000.
        summary = apps_endpoint._summary_from_status_counts(
            [("approved", "auto", 5000), ("started", None, 1200), ("withdrawn", None, 7)]
        )
        assert summary.approved == 5000
        assert summary.started == 1200
        assert summary.declined == 7  # withdrawn -> declined bucket
        assert summary.total == 6207

    def test_summary_auto_decline_is_silently_escalated(self):
        # WS-I (Dave): an AUTO decline pending human review must count as
        # manual_review on the vendor surface, never as a final decline.
        summary = apps_endpoint._summary_from_status_counts(
            [("declined", "auto", 3), ("declined", "user-uuid", 2)]
        )
        assert summary.manual_review == 3
        assert summary.declined == 2
        assert summary.total == 5


class TestFinancingLinkHelpers:
    def test_split_name(self):
        assert fl_endpoint._split_name("Jordan Lee") == ("Jordan", "Lee")
        assert fl_endpoint._split_name("Cher") == ("Cher", None)
        assert fl_endpoint._split_name("Mary Jane Watson") == ("Mary", "Jane Watson")
        assert fl_endpoint._split_name("   ") == (None, None)

    def test_looks_like_email(self):
        assert fl_endpoint._looks_like_email("a@b.com")
        assert not fl_endpoint._looks_like_email("+15555550148")

    def test_build_url_shape(self):
        app_id = uuid.uuid4()
        prod_id = uuid.uuid4()
        url = fl_endpoint._build_patient_flow_url(app_id, prod_id, 180_000)
        assert f"ref={app_id}" in url
        assert f"product={prod_id}" in url
        assert "amount=180000" in url


# --- endpoint integration (mocked DB) --------------------------------------


class TestProductsEndpoint:
    def test_lists_active_products(self, app_under_test):
        product = _fake_product()
        db = MagicMock()
        (
            db.query.return_value.filter.return_value.order_by.return_value.all
        ).return_value = [product]
        app_under_test.dependency_overrides[get_db] = lambda: db

        client = TestClient(app_under_test)
        resp = client.get(f"{_BASE}/products")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body) == 1
        assert body[0]["code"] == "DENTAL_ELECTIVE"
        assert body[0]["currency"] == "CAD"

    def test_requires_auth(self, app_under_test):
        # Drop the clinic-scope override so the real dependency runs. With no
        # token, the underlying get_current_user raises (no credentials -> 403).
        app_under_test.dependency_overrides.pop(get_current_clinic_user, None)
        app_under_test.dependency_overrides[get_db] = lambda: MagicMock()
        client = TestClient(app_under_test)
        resp = client.get(f"{_BASE}/products")
        assert resp.status_code in (401, 403)


class TestApplicationsEndpoint:
    def _wire_db(self, app_under_test, rows):
        db = MagicMock()
        (
            db.query.return_value.filter.return_value.options.return_value
            .order_by.return_value.limit.return_value.all
        ).return_value = rows
        app_under_test.dependency_overrides[get_db] = lambda: db
        return db

    def test_list_applications(self, app_under_test):
        rows = [_fake_application(status="approved"), _fake_application(status="under_review")]
        self._wire_db(app_under_test, rows)
        client = TestClient(app_under_test)
        resp = client.get(f"{_BASE}/applications")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body) == 2
        statuses = {r["status"] for r in body}
        assert statuses == {"approved", "manual_review"}

    def test_dashboard_summary(self, app_under_test):
        # The summary counts via SQL GROUP BY (status, decision_by), so the DB
        # yields (platform_status, decision_by, count) triples rather than rows.
        db = MagicMock()
        (
            db.query.return_value.filter.return_value.group_by.return_value.all
        ).return_value = [
            ("approved", "auto", 1),
            ("declined", "human-actor", 1),
            ("under_review", None, 1),
            ("started", None, 1),
        ]
        app_under_test.dependency_overrides[get_db] = lambda: db
        client = TestClient(app_under_test)
        resp = client.get(f"{_BASE}/dashboard/summary")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {
            "started": 1,
            "approved": 1,
            "declined": 1,
            "manual_review": 1,
            "total": 4,
        }


class TestFinancingLinkEndpoint:
    def test_creates_link(self, app_under_test):
        product = _fake_product()

        # DB: product lookup returns the active product; patient lookup returns None
        # (so a new patient would be created — add/commit/refresh are no-ops on the mock).
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = product

        created_app = SimpleNamespace(id=uuid.uuid4())
        orchestrator = MagicMock()
        orchestrator.create_application.return_value = created_app

        app_under_test.dependency_overrides[get_db] = lambda: db
        app_under_test.dependency_overrides[clinic_deps.get_orchestrator] = lambda: orchestrator

        client = TestClient(app_under_test)
        resp = client.post(
            f"{_BASE}/financing-links",
            json={
                "credit_product_id": str(product.id),
                "amount_cents": 180_000,
                "patient_name": "Jordan Lee",
                "patient_contact": "jordan@example.com",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["application_ref"] == str(created_app.id)
        assert body["patient_name"] == "Jordan Lee"
        assert body["amount_cents"] == 180_000
        assert body["product_name"] == product.name
        assert f"ref={created_app.id}" in body["url"]

        # Orchestrator called with clinic-sourced amount.
        kwargs = orchestrator.create_application.call_args.kwargs
        assert kwargs["requested_amount_source"] == "clinic"
        assert kwargs["requested_amount_cents"] == 180_000
        assert kwargs["credit_product_id"] == product.id

    def test_inactive_product_404(self, app_under_test):
        product = _fake_product(status="archived")
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = product
        app_under_test.dependency_overrides[get_db] = lambda: db
        app_under_test.dependency_overrides[clinic_deps.get_orchestrator] = lambda: MagicMock()

        client = TestClient(app_under_test)
        resp = client.post(
            f"{_BASE}/financing-links",
            json={
                "credit_product_id": str(product.id),
                "amount_cents": 180_000,
                "patient_name": "Jordan Lee",
                "patient_contact": "jordan@example.com",
            },
        )
        assert resp.status_code == 404, resp.text


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
