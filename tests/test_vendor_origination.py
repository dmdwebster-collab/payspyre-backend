"""WS-I vendor origination — intake, preview, request-reprocessing (DB-free).

Covers Dave's vendor-portal spec (docs/turnkey_parity/10__Vendor_Access.md):

* the Application Submission Checklist money invariant
  (treatment − insurance − down payment = amount financed),
* PricingConfig-bound term / frequency / rate validation and the
  ``rate_edit_roles`` role gate,
* the consent-first borrower hand-off (magic link by email/SMS),
* the live payment preview (principal + interest + fees = total, Canadian APR),
* request-reprocessing as the ONLY vendor underwriting action (vendor-scoped,
  cross-vendor 404, audited, idempotent),
* the silent-escalation rule (auto-decline shows as "in review" to vendors).

These tests deliberately AVOID a live database — a fresh FastAPI app mounts
``clinic_router`` with ``get_db`` / auth / orchestrator / auth-service
dependencies overridden by fakes (same pattern as tests/test_clinic_api.py).

Run (only this file — the full suite hits a shared remote DB):
    python -m pytest tests/test_vendor_origination.py -p no:warnings -q
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.applicant.v1.deps import get_patient_auth_service
from app.api.clinic.v1 import deps as clinic_deps
from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.api.clinic.v1.endpoints import vendor_origination as vo
from app.api.clinic.v1.router import clinic_router
from app.api.clinic.v1.status_map import to_vendor_visible_status
from app.db.base import get_db
from app.models.platform.event import PlatformEvent
from app.schemas.pricing_config import parse_pricing_config
from app.services.flow_orchestrator import (
    InvalidStateTransition,
    mark_vendor_reprocessing,
)

_BASE = "/api/clinic/v1"
_VENDOR_ID = uuid.uuid4()

# Typed pricing config: Turkey demo band 9.99–23.99 around 19.99, admin-only
# rate edits, monthly + bi-weekly, terms 6–24 months, $25 origination + $1/payment.
_PRICING = {
    "schema_version": 1,
    "interest": {
        "annual_rate_bps": 1999,
        "min_rate_bps": 999,
        "max_rate_bps": 2399,
        "rate_edit_roles": ["admin"],
    },
    "payment_frequencies": ["monthly", "bi_weekly"],
    "term_min_months": 6,
    "term_max_months": 24,
    "fees": [
        {"fee_type": "origination", "calc": "fixed_cents", "amount": 2500,
         "charge_timing": "at_origination"},
        {"fee_type": "administration", "calc": "fixed_cents", "amount": 100,
         "charge_timing": "per_payment"},
    ],
}


def _fake_product(status="active"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        code="DENTAL_ELECTIVE",
        name="Dental — Elective Care",
        vertical="dental",
        status=status,
        min_amount_cents=50_000,
        max_amount_cents=3_000_000,
        currency="CAD",
        pricing_config=dict(_PRICING),
    )


def _fake_created_application():
    """The row the (mocked) orchestrator returns — mutable, like an ORM object."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        patient_id=uuid.uuid4(),
        status="started",
        decision_by=None,
        self_reported={},
        vendor_reprocessing_requested=False,
    )


# Dave's checklist numbers (f0033): $25,000 − $1,500 − $7,500 = $16,000.
def _intake_body(product_id, **overrides):
    body = {
        "patient_name": "Raleigh Bailey",
        "patient_contact": "raleigh@example.com",
        "credit_product_id": str(product_id),
        "treatment_cost_cents": 2_500_000,
        "insurance_coverage_cents": 150_000,
        "down_payment_cents": 750_000,
        "amount_financed_cents": 1_600_000,
        "term_months": 12,
        "province": "bc",
        "provider_name": "Dr. Khadembashi",
        "loan_start_date": "2026-07-16",
        "first_due_date": "2026-08-07",
        "preferred_payment_amount_cents": 26_000,
        "preferred_payment_frequency": "bi-weekly",
        "preferred_first_due_date": "2026-06-25",
        "alt_contact_name": "June",
        "alt_contact_relationship": "Mother",
        "additional_notes": "Advised has bad credit, concerned about approval",
    }
    body.update(overrides)
    return body


@pytest.fixture
def harness():
    """FastAPI app + fakes; returns (client, ctx) with every mock reachable."""
    app = FastAPI()
    app.include_router(clinic_router, prefix="/api/clinic/v1")

    product = _fake_product()
    created = _fake_created_application()

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = product

    orchestrator = MagicMock()
    orchestrator.create_application.return_value = created

    auth_service = MagicMock()
    auth_service.request_magic_link.return_value = {
        "contact_method": "email", "message": "Code sent."
    }

    ctx = SimpleNamespace(
        app=app, db=db, product=product, created=created,
        orchestrator=orchestrator, auth_service=auth_service, role="staff",
    )

    def _principal():
        return ClinicPrincipal(
            user=SimpleNamespace(id=uuid.uuid4()), vendor_id=_VENDOR_ID, role=ctx.role
        )

    app.dependency_overrides[get_current_clinic_user] = _principal
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[clinic_deps.get_orchestrator] = lambda: orchestrator
    app.dependency_overrides[get_patient_auth_service] = lambda: auth_service

    yield TestClient(app), ctx
    app.dependency_overrides.clear()


# --- pure helpers ------------------------------------------------------------


class TestVendorVisibleStatus:
    def test_auto_decline_is_silently_escalated(self):
        # Dave: the vendor must NOT be told an auto-decline is final while the
        # human escalation is pending — it reads as "in review".
        assert to_vendor_visible_status("declined", "auto") == "manual_review"

    def test_human_confirmed_decline_shows_declined(self):
        assert to_vendor_visible_status("declined", "8ce1…user") == "declined"
        assert to_vendor_visible_status("declined", None) == "declined"

    def test_other_statuses_unchanged(self):
        assert to_vendor_visible_status("approved", "auto") == "approved"
        assert to_vendor_visible_status("under_review", None) == "manual_review"
        assert to_vendor_visible_status("started", None) == "started"
        assert to_vendor_visible_status("withdrawn", None) == "declined"


class TestMarkVendorReprocessing:
    def _app(self, status):
        return SimpleNamespace(status=status, status_updated_at=None)

    @pytest.mark.parametrize("status", ["declined", "under_review", "underwriting"])
    def test_reprocessable_states_move_to_under_review(self, status):
        app = self._app(status)
        mark_vendor_reprocessing(app)
        assert app.status == "under_review"
        assert app.status_updated_at is not None

    @pytest.mark.parametrize("status", ["approved", "withdrawn", "expired", "started", "verifying"])
    def test_other_states_are_refused(self, status):
        with pytest.raises(InvalidStateTransition):
            mark_vendor_reprocessing(self._app(status))


class TestResolveRateBps:
    _cfg = parse_pricing_config(_PRICING)

    def test_none_returns_product_default_without_gate(self):
        assert vo.resolve_rate_bps(self._cfg, None, role="staff", enforce_role=True) == 1999

    def test_default_rate_passthrough_is_not_a_rate_edit(self):
        assert vo.resolve_rate_bps(self._cfg, 1999, role="staff", enforce_role=True) == 1999

    def test_custom_rate_out_of_band_422_even_for_admin(self):
        with pytest.raises(HTTPException) as exc:
            vo.resolve_rate_bps(self._cfg, 2500, role="admin", enforce_role=True)
        assert exc.value.status_code == 422

    def test_custom_rate_role_gated_403_for_staff(self):
        with pytest.raises(HTTPException) as exc:
            vo.resolve_rate_bps(self._cfg, 1499, role="staff", enforce_role=True)
        assert exc.value.status_code == 403

    def test_custom_rate_allowed_for_admin_within_band(self):
        assert vo.resolve_rate_bps(self._cfg, 1499, role="admin", enforce_role=True) == 1499

    def test_preview_skips_role_gate_but_not_band(self):
        assert vo.resolve_rate_bps(self._cfg, 1499, role="staff", enforce_role=False) == 1499
        with pytest.raises(HTTPException):
            vo.resolve_rate_bps(self._cfg, 500, role="staff", enforce_role=False)


# --- intake endpoint ----------------------------------------------------------


class TestVendorIntake:
    def test_checklist_invariant_violation_is_422(self, harness):
        client, ctx = harness
        body = _intake_body(ctx.product.id, amount_financed_cents=1_599_999)
        resp = client.post(f"{_BASE}/applications", json=body)
        assert resp.status_code == 422
        assert "money model" in resp.text

    def test_first_due_before_start_is_422(self, harness):
        client, ctx = harness
        body = _intake_body(ctx.product.id, first_due_date="2026-07-15")
        resp = client.post(f"{_BASE}/applications", json=body)
        assert resp.status_code == 422

    def test_term_outside_product_bounds_is_422(self, harness):
        client, ctx = harness
        resp = client.post(
            f"{_BASE}/applications", json=_intake_body(ctx.product.id, term_months=48)
        )
        assert resp.status_code == 422
        assert "range" in resp.json()["detail"]

    def test_unoffered_preferred_frequency_is_422(self, harness):
        client, ctx = harness
        resp = client.post(
            f"{_BASE}/applications",
            json=_intake_body(ctx.product.id, preferred_payment_frequency="weekly"),
        )
        assert resp.status_code == 422

    def test_inactive_product_is_404(self, harness):
        client, ctx = harness
        ctx.product.status = "archived"
        resp = client.post(f"{_BASE}/applications", json=_intake_body(ctx.product.id))
        assert resp.status_code == 404

    def test_custom_rate_403_for_staff_201_for_admin(self, harness, monkeypatch):
        client, ctx = harness
        monkeypatch.setattr(
            vo, "_find_or_create_patient",
            lambda db, body: SimpleNamespace(id=ctx.created.patient_id),
        )
        body = _intake_body(ctx.product.id, requested_annual_rate_bps=1499)

        resp = client.post(f"{_BASE}/applications", json=body)
        assert resp.status_code == 403

        ctx.role = "admin"
        resp = client.post(f"{_BASE}/applications", json=body)
        assert resp.status_code == 201, resp.text
        assert ctx.created.requested_annual_rate_bps == 1499

    def test_successful_intake_full_flow(self, harness, monkeypatch):
        client, ctx = harness
        patient = SimpleNamespace(id=ctx.created.patient_id)
        monkeypatch.setattr(vo, "_find_or_create_patient", lambda db, body: patient)

        resp = client.post(f"{_BASE}/applications", json=_intake_body(ctx.product.id))
        assert resp.status_code == 201, resp.text
        out = resp.json()

        # -- response is vendor-safe + carries the hand-off -------------------
        assert out["application_id"] == str(ctx.created.id)
        assert out["status"] == "started"
        assert out["amount_financed_cents"] == 1_600_000
        assert out["verification_channel"] == "email"
        assert f"ref={ctx.created.id}" in out["patient_flow_url"]

        # -- orchestrator got the clinic-sourced, vendor-stamped creation -----
        kwargs = ctx.orchestrator.create_application.call_args.kwargs
        assert kwargs["vendor_id"] == _VENDOR_ID
        assert kwargs["requested_amount_source"] == "clinic"
        assert kwargs["requested_amount_cents"] == 1_600_000
        assert kwargs["clinic_proposed_amount_cents"] == 1_600_000
        assert kwargs["patient_id"] == patient.id

        # -- checklist fields persisted on the row (migration 052) ------------
        c = ctx.created
        assert c.treatment_cost_cents == 2_500_000
        assert c.insurance_coverage_cents == 150_000
        assert c.down_payment_cents == 750_000
        assert c.requested_term_months == 12
        assert c.requested_annual_rate_bps == 1999  # product default, explicit
        assert c.provider_name == "Dr. Khadembashi"
        assert c.residence_province == "BC"
        assert c.preferred_payment_frequency == "bi_weekly"  # alias normalized
        assert c.preferred_payment_amount_cents == 26_000
        assert str(c.loan_start_date) == "2026-07-16"
        assert str(c.first_due_date) == "2026-08-07"
        # unqueried checklist extras -> self_reported
        assert c.self_reported["vendor_intake"]["alt_contact_name"] == "June"
        assert c.self_reported["vendor_intake"]["alt_contact_relationship"] == "Mother"

        # -- audited -----------------------------------------------------------
        events = [
            a.args[0] for a in ctx.db.add.call_args_list
            if a.args and isinstance(a.args[0], PlatformEvent)
        ]
        submitted = [e for e in events if e.event_type == "vendor_application_submitted"]
        assert len(submitted) == 1
        payload = submitted[0].payload
        assert payload["actor"]["type"] == "vendor"
        assert payload["metadata"]["vendor_id"] == str(_VENDOR_ID)
        assert payload["after"]["amount_financed_cents"] == 1_600_000

        # -- borrower journey kicked off (PaySpyre decides; vendor originates) --
        ctx.auth_service.request_magic_link.assert_called_once_with(ctx.created.id, "email")

    def test_phone_contact_sends_sms_magic_link(self, harness, monkeypatch):
        client, ctx = harness
        monkeypatch.setattr(
            vo, "_find_or_create_patient",
            lambda db, body: SimpleNamespace(id=ctx.created.patient_id),
        )
        body = _intake_body(ctx.product.id, patient_contact="+16045550123")
        resp = client.post(f"{_BASE}/applications", json=body)
        assert resp.status_code == 201, resp.text
        assert resp.json()["verification_channel"] == "sms"
        ctx.auth_service.request_magic_link.assert_called_once_with(ctx.created.id, "sms")


# --- preview endpoint ----------------------------------------------------------


class TestPaymentPreview:
    def _body(self, product_id, **overrides):
        body = {
            "credit_product_id": str(product_id),
            "amount_cents": 1_600_000,
            "term_months": 12,
            "frequency": "monthly",
        }
        body.update(overrides)
        return body

    def test_preview_math_holds_together(self, harness):
        client, ctx = harness
        resp = client.post(f"{_BASE}/applications/preview", json=self._body(ctx.product.id))
        assert resp.status_code == 200, resp.text
        out = resp.json()

        assert out["num_payments"] == 12
        assert out["annual_rate_bps"] == 1999
        # fees: $25 origination + $1 x 12 payments = $37.00
        assert out["fees_cents"] == 2500 + 100 * 12
        # Principal + Interest + Fees = Total (the Turnkey equation strip)
        assert (
            out["principal_cents"] + out["interest_cents"] + out["fees_cents"]
            == out["total_of_payments_cents"]
        )
        # With fees > 0 the Canadian regulatory APR exceeds the contract rate.
        assert out["apr_bps"] > out["annual_rate_bps"]
        # Full amortization schedule preview, terminating at zero balance.
        assert len(out["schedule"]) == out["num_payments"]
        assert out["schedule"][-1]["balance_cents"] == 0
        # Schedule internal consistency on the first row.
        row = out["schedule"][0]
        assert row["payment_cents"] == row["principal_cents"] + row["interest_cents"]
        # Commission: no model in PricingConfig yet — flagged, never fabricated.
        assert out["commission_cents"] is None
        assert "flagged" in out["commission_note"].lower()
        # The vendor-relevant fee lines are surfaced.
        assert {f["fee_type"] for f in out["fee_lines"]} == {"origination", "administration"}

    def test_frequency_alias_is_normalized(self, harness):
        client, ctx = harness
        resp = client.post(
            f"{_BASE}/applications/preview",
            json=self._body(ctx.product.id, frequency="bi-weekly"),
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["frequency"] == "bi_weekly"
        assert out["num_payments"] == 26  # 12 months bi-weekly

    def test_unoffered_frequency_is_422(self, harness):
        client, ctx = harness
        resp = client.post(
            f"{_BASE}/applications/preview",
            json=self._body(ctx.product.id, frequency="weekly"),
        )
        assert resp.status_code == 422

    def test_amount_outside_product_bounds_is_422(self, harness):
        client, ctx = harness
        resp = client.post(
            f"{_BASE}/applications/preview",
            json=self._body(ctx.product.id, amount_cents=10_000_000),
        )
        assert resp.status_code == 422

    def test_custom_rate_within_band_no_role_gate(self, harness):
        # Preview never persists; staff may preview any IN-BAND rate.
        client, ctx = harness
        resp = client.post(
            f"{_BASE}/applications/preview",
            json=self._body(ctx.product.id, annual_rate_bps=1499),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["annual_rate_bps"] == 1499

    def test_custom_rate_out_of_band_is_422(self, harness):
        client, ctx = harness
        resp = client.post(
            f"{_BASE}/applications/preview",
            json=self._body(ctx.product.id, annual_rate_bps=9999),
        )
        assert resp.status_code == 422

    def test_nothing_is_persisted(self, harness):
        client, ctx = harness
        client.post(f"{_BASE}/applications/preview", json=self._body(ctx.product.id))
        ctx.db.add.assert_not_called()
        ctx.db.commit.assert_not_called()


# --- request-reprocessing endpoint ---------------------------------------------


class TestRequestReprocessing:
    def _wire(self, ctx, application):
        ctx.db.query.return_value.filter.return_value.first.return_value = application

    def _app_row(self, status="declined", decision_by="auto", requested=False):
        return SimpleNamespace(
            id=uuid.uuid4(),
            patient_id=uuid.uuid4(),
            status=status,
            decision_by=decision_by,
            vendor_reprocessing_requested=requested,
            status_updated_at=None,
        )

    def test_auto_declined_app_moves_to_review_and_is_audited(self, harness):
        client, ctx = harness
        row = self._app_row(status="declined", decision_by="auto")
        self._wire(ctx, row)

        resp = client.post(f"{_BASE}/applications/{row.id}/request-reprocessing")
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["reprocessing_requested"] is True
        # Vendor-visible status: in review, never a raw platform status.
        assert out["status"] == "manual_review"
        assert row.status == "under_review"
        assert row.vendor_reprocessing_requested is True

        events = [
            a.args[0] for a in ctx.db.add.call_args_list
            if a.args and isinstance(a.args[0], PlatformEvent)
        ]
        assert [e.event_type for e in events] == ["vendor_reprocessing_requested"]
        payload = events[0].payload
        assert payload["before"] == {"status": "declined"}
        assert payload["after"]["vendor_reprocessing_requested"] is True
        ctx.db.commit.assert_called()

    def test_query_is_vendor_scoped(self, harness):
        client, ctx = harness
        row = self._app_row()
        self._wire(ctx, row)
        client.post(f"{_BASE}/applications/{row.id}/request-reprocessing")

        # The single lookup filters on id AND the caller's vendor_id — the
        # cross-vendor case is indistinguishable from a missing row.
        filter_args = ctx.db.query.return_value.filter.call_args.args
        assert len(filter_args) == 2
        rendered = " ".join(str(a.right.value) for a in filter_args)
        assert str(_VENDOR_ID) in rendered

    def test_cross_vendor_or_missing_is_404(self, harness):
        client, ctx = harness
        self._wire(ctx, None)
        resp = client.post(f"{_BASE}/applications/{uuid.uuid4()}/request-reprocessing")
        assert resp.status_code == 404

    @pytest.mark.parametrize("status", ["approved", "started", "verifying", "withdrawn"])
    def test_ineligible_status_is_409(self, harness, status):
        client, ctx = harness
        row = self._app_row(status=status, decision_by=None)
        self._wire(ctx, row)
        resp = client.post(f"{_BASE}/applications/{row.id}/request-reprocessing")
        assert resp.status_code == 409
        assert row.vendor_reprocessing_requested is False

    def test_repeat_request_is_idempotent(self, harness):
        client, ctx = harness
        row = self._app_row(status="under_review", decision_by=None, requested=True)
        self._wire(ctx, row)
        resp = client.post(f"{_BASE}/applications/{row.id}/request-reprocessing")
        assert resp.status_code == 200
        assert resp.json()["reprocessing_requested"] is True
        ctx.db.add.assert_not_called()  # no duplicate audit event

    def test_requires_auth(self, harness):
        client, ctx = harness
        ctx.app.dependency_overrides.pop(get_current_clinic_user, None)
        resp = client.post(f"{_BASE}/applications/{uuid.uuid4()}/request-reprocessing")
        assert resp.status_code in (401, 403)


# --- silent escalation on the dashboard surface --------------------------------


class TestDashboardSilentEscalation:
    def test_outcome_bucket_hides_pending_auto_decline(self):
        from app.api.clinic.v1.endpoints.dashboard_applications import _outcome_bucket

        assert _outcome_bucket("declined", "auto") == "in_review"
        assert _outcome_bucket("declined", "human-id") == "declined"
        assert _outcome_bucket("declined", None) == "declined"
        assert _outcome_bucket("approved", "auto") == "approved"


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
