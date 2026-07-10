"""Contract test: a product CREATED through the admin API must be QUOTABLE
through the applicant API, with fees flowing into the disclosure (P0 WS-B).

This is the regression test for the pricing_config <-> quote schema mismatch
(seeds/tests wrote ``origination_fee_pct``/``apr_range`` while the quote engine
read ``fees_cents``/``apr_bps`` — fees silently vanished from every quote).

DB-FREE by design: FastAPI dependency overrides swap ``get_db`` for an
in-memory fake session (this suite must never touch the shared test DB).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.applicant.v1.router import applicant_router
from app.api.v1.endpoints.credit_products import router as admin_products_router
from app.core.auth import get_current_user
from app.db.base import get_db
from app.models.platform.credit_product import PlatformCreditProduct
from app.services import loan_quote

_ADMIN_BASE = "/api/v1/credit-products"
_APPLICANT_BASE = "/api/applicant/v1/products"

_VALID_MATRIX = {
    "identity": {"required": True, "methods": ["email_otp", "id_doc_scan"], "min_confidence": 0.85},
    "income": {"required": True, "methods": ["bank_link", "t4"], "require_bank_link": False,
               "min_stated_income_cents": 4000000},
    "bureau": {"soft_pull_required": True, "hard_pull_required": True, "min_score": 640,
               "max_score_age_days": 90},
    "affordability": {"max_dti": 0.42, "min_payment_to_income_ratio": 0.20,
                      "max_loan_to_income_ratio": 2.5},
}


# ---------------------------------------------------------------------------
# In-memory fake Session — just enough of the SQLAlchemy surface for the
# create -> read -> quote path, plus JSONB simulation (values stored as the
# JSON round-trip of what the endpoint persisted).
# ---------------------------------------------------------------------------

class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class FakeSession:
    def __init__(self):
        self.products: list[PlatformCreditProduct] = []
        self.events: list = []

    def add(self, obj):
        if isinstance(obj, PlatformCreditProduct):
            self.products.append(obj)
        else:
            self.events.append(obj)

    def commit(self):
        now = datetime.now(timezone.utc)
        for p in self.products:
            if p.id is None:
                p.id = uuid.uuid4()
            if p.version is None:
                p.version = 1
            if p.created_at is None:
                p.created_at = now
            if p.updated_at is None:
                p.updated_at = now
            # Simulate the JSONB round trip: whatever the endpoint persisted is
            # what a later reader gets back after json (de)serialization.
            if p.pricing_config is not None:
                p.pricing_config = json.loads(json.dumps(p.pricing_config))

    def refresh(self, obj):
        pass

    def rollback(self):
        pass

    def query(self, model):
        assert model is PlatformCreditProduct
        return _FakeQuery(self.products)


def _fake_admin():
    role = type("Role", (), {"name": "admin"})()
    user_role = type("UserRole", (), {"role": role})()
    return type("U", (), {"id": uuid.uuid4(), "email": "admin@test", "role": "admin",
                          "roles": [user_role]})()


@pytest.fixture
def api():
    app = FastAPI()
    app.include_router(admin_products_router, prefix=_ADMIN_BASE)
    app.include_router(applicant_router, prefix="/api/applicant/v1")
    session = FakeSession()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = _fake_admin
    client = TestClient(app)
    yield client, session
    app.dependency_overrides.clear()


def _payload(pricing: dict) -> dict:
    return {
        "code": f"contract_{uuid.uuid4().hex[:8]}",
        "name": "Contract Test Product",
        "vertical": "dental",
        "status": "active",
        "min_amount_cents": 100_000,
        "max_amount_cents": 1_000_000,
        "currency": "CAD",
        "verification_matrix": _VALID_MATRIX,
        "decision_ruleset": "dental_full_arch_v1.yaml",
        "pricing_config": pricing,
        "funding_source": "payspyre_capital",
    }


_TYPED_PRICING = {
    "schema_version": 1,
    "interest": {"annual_rate_bps": 1999, "min_rate_bps": 999, "max_rate_bps": 2399,
                 "rate_edit_roles": ["admin"]},
    "payment_frequencies": ["monthly", "bi_weekly"],
    "term_min_months": 12,
    "term_max_months": 48,
    "term_options": [12, 24, 36, 48],
    "fees": [
        {"fee_type": "administration", "calc": "fixed_cents", "amount": 100,
         "charge_timing": "per_payment"},
        {"fee_type": "nsf", "calc": "fixed_cents", "amount": 4500,
         "charge_timing": "on_event", "add_on": True},
        {"fee_type": "origination", "calc": "fixed_cents", "amount": 2500,
         "charge_timing": "at_origination"},
    ],
}


class TestCreateThenQuoteContract:
    def test_typed_product_round_trips_through_quote(self, api):
        client, _ = api
        created = client.post(_ADMIN_BASE, json=_payload(_TYPED_PRICING))
        assert created.status_code == 201, created.text
        product_id = created.json()["id"]
        stored = created.json()["pricing_config"]
        assert stored["schema_version"] == 1  # normalized typed shape persisted

        # terms: the calculator advertises exactly the configured frequencies+rate
        terms = client.get(f"{_APPLICANT_BASE}/{product_id}/terms")
        assert terms.status_code == 200, terms.text
        body = terms.json()
        assert body["annual_rate_bps"] == 1999
        assert [f["value"] for f in body["frequencies"]] == ["monthly", "bi_weekly"]
        assert body["term_min"] == 12 and body["term_max"] == 48

        # every advertised frequency must actually quote, fees included
        for freq in ("monthly", "bi_weekly"):
            q = client.post(f"{_APPLICANT_BASE}/{product_id}/quote",
                            json={"amount_cents": 500_000, "term_months": 24,
                                  "frequency": freq})
            assert q.status_code == 200, q.text
            data = q.json()
            n = loan_quote.num_payments(24, freq)
            # $25 origination + $1 per scheduled payment; NSF ($45) excluded
            assert data["fees_cents"] == 2500 + 100 * n
            assert data["cost_of_borrowing_cents"] == data["interest_cents"] + data["fees_cents"]
            assert data["apr_bps"] > 1999  # fees push APR above the contract rate
            assert data["exceeds_criminal_rate"] is False

        # per-payment fee makes bi-weekly APR exceed monthly APR (Dave's demo)
        aprs = {}
        for freq in ("monthly", "bi_weekly"):
            q = client.post(f"{_APPLICANT_BASE}/{product_id}/quote",
                            json={"amount_cents": 500_000, "term_months": 24,
                                  "frequency": freq})
            aprs[freq] = q.json()["apr_bps"]
        assert aprs["bi_weekly"] > aprs["monthly"]

    def test_legacy_shape_create_no_longer_drops_fees(self, api):
        """THE MISMATCH, fixed: the seed-022 shape used to quote with fees=0."""
        client, _ = api
        legacy = {"term_options": [24, 36, 48], "apr_range": [8.99, 24.99],
                  "origination_fee_pct": 0.02}
        created = client.post(_ADMIN_BASE, json=_payload(legacy))
        assert created.status_code == 201, created.text
        product_id = created.json()["id"]

        stored = created.json()["pricing_config"]
        assert stored.get("schema_version") == 1  # upgraded on write
        assert stored["interest"]["annual_rate_bps"] == 899  # apr_range floor
        assert stored["fees"][0]["fee_type"] == "origination"
        assert stored["fees"][0]["amount"] == 200  # 2% -> 200 bps, integer

        q = client.post(f"{_APPLICANT_BASE}/{product_id}/quote",
                        json={"amount_cents": 500_000, "term_months": 24,
                              "frequency": "monthly"})
        assert q.status_code == 200, q.text
        data = q.json()
        assert data["fees_cents"] == 10_000  # 2% of $5,000 — previously 0
        assert data["annual_rate_bps"] == 899
        assert data["apr_bps"] > 899

    def test_create_rejects_criminal_rate_per_frequency(self, api):
        client, _ = api
        bad = {
            "schema_version": 1,
            "interest": {"annual_rate_bps": 999, "min_rate_bps": 999, "max_rate_bps": 999},
            "payment_frequencies": ["monthly", "weekly"],
            "term_min_months": 6,
            "term_max_months": 12,
            "fees": [{"fee_type": "administration", "calc": "fixed_cents",
                      "amount": 500, "charge_timing": "per_payment"}],
        }
        r = client.post(_ADMIN_BASE, json=_payload(bad))
        assert r.status_code == 422, r.text
        assert "s.347" in r.json()["detail"]

    def test_create_rejects_enabled_late_fee(self, api):
        client, _ = api
        bad = dict(_TYPED_PRICING)
        bad = json.loads(json.dumps(bad))
        bad["fees"].append({"fee_type": "late", "calc": "fixed_cents", "amount": 2500,
                            "charge_timing": "on_event", "enabled": True})
        r = client.post(_ADMIN_BASE, json=_payload(bad))
        assert r.status_code == 422, r.text
        assert "disabled in Canada" in r.json()["detail"]

    def test_unquotable_garbage_rejected_at_create(self, api):
        client, _ = api
        r = client.post(_ADMIN_BASE, json=_payload(
            {"schema_version": 1, "interest": {"annual_rate_bps": "high"}}))
        assert r.status_code == 422, r.text
