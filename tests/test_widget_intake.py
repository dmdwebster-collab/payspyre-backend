"""Embedded pre-qual widget intake — server-to-server application creation."""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.api import api_router
from app.core.config import settings
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.services import loan_quote

_URL = "/api/v1/widget/pre-qualification"


@pytest.fixture
def client(db_session: Session):
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _product(db: Session) -> PlatformCreditProduct:
    return (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
    )


def _body(p: PlatformCreditProduct, *, score=760):
    params = loan_quote.product_terms(p.pricing_config)
    amount = (p.min_amount_cents + p.max_amount_cents) // 2
    return {
        "applicant": {
            "first_name": "David", "last_name": "Test",
            "email": f"w-{uuid.uuid4().hex[:8]}@example.com",
            "date_of_birth": "1990-09-04", "credit_score": score,
        },
        "financing": {
            "product_code": p.code, "amount_cents": amount, "term_months": params["term_min"],
            "frequency": "monthly", "province": "BC",
            "vendor": "TEST VENDOR 2", "store_provider": "TV2 - Provider 2",
        },
        "income": {
            "income_type": "Employed", "net_monthly_income_cents": 1_335_000,
            "housing_cents": 250_000, "vehicle_cents": 50_000, "other_expenses_cents": 75_000,
        },
        "widget_outcome": "Approved",
    }


def test_inert_without_key(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "WIDGET_API_KEY", "")
    r = client.post(_URL, json=_body(_product(db_session)))
    assert r.status_code == 403, r.text


def test_intake_creates_application_records_widget_and_returns_quote(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "WIDGET_API_KEY", "secret")
    r = client.post(_URL, headers={"X-Widget-Key": "secret"}, json=_body(_product(db_session)))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["prequalified"] is True
    assert d["quote"]["installment_cents"] > 0 and d["quote"]["apr_bps"] is not None
    # the application is now a real row in the platform (lands in the cockpit)
    app = (
        db_session.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == d["application_id"])
        .first()
    )
    assert app is not None
    assert app.self_reported["widget"]["outcome"] == "Approved"
    assert app.flow_state["widget_prequalification"] is True


def test_below_product_min_score_is_referred_not_prequalified(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "WIDGET_API_KEY", "secret")
    p = _product(db_session)
    min_score = ((p.verification_matrix or {}).get("bureau") or {}).get("min_score")
    if not min_score:
        pytest.skip("product has no configured min_score")
    r = client.post(_URL, headers={"X-Widget-Key": "secret"}, json=_body(p, score=min_score - 50))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["prequalified"] is False and d["reasons"]


def test_unknown_product_404(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "WIDGET_API_KEY", "secret")
    body = _body(_product(db_session))
    body["financing"]["product_code"] = "NO_SUCH_PRODUCT"
    r = client.post(_URL, headers={"X-Widget-Key": "secret"}, json=body)
    assert r.status_code == 404, r.text
