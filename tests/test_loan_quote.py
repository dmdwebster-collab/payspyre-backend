"""Loan-terms quote engine + applicant quote endpoints (live test DB)."""
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.applicant.v1.router import applicant_router
from app.db.base import get_db
from app.models.platform.credit_product import PlatformCreditProduct
from app.services import loan_quote

_BASE = "/api/applicant/v1/products"


class TestQuoteMath:
    def test_monthly_amortizes_with_interest(self):
        q = loan_quote.quote_loan(1_000_000, 1290, 12, "monthly")
        assert q.num_payments == 12
        assert q.installment_cents > 0
        # total > principal, and cost of borrowing is the interest
        assert q.total_of_payments_cents > q.amount_cents
        assert q.cost_of_borrowing_cents == q.total_of_payments_cents - q.amount_cents
        # the schedule fully amortizes — last payment clears the balance
        assert q.final_installment_cents > 0

    def test_frequency_changes_payment_count(self):
        monthly = loan_quote.quote_loan(1_000_000, 1290, 24, "monthly")
        biweekly = loan_quote.quote_loan(1_000_000, 1290, 24, "bi_weekly")
        weekly = loan_quote.quote_loan(1_000_000, 1290, 24, "weekly")
        assert monthly.num_payments == 24
        assert biweekly.num_payments == 52   # 24mo * 26/12
        assert weekly.num_payments == 104     # 24mo * 52/12
        # more frequent, smaller, payments
        assert weekly.installment_cents < biweekly.installment_cents < monthly.installment_cents

    def test_zero_rate_is_interest_free(self):
        q = loan_quote.quote_loan(1_200_000, 0, 12, "monthly")
        assert q.cost_of_borrowing_cents == 0
        assert q.total_of_payments_cents == q.amount_cents
        assert q.installment_cents == 100_000  # 1.2m / 12

    def test_fees_fold_into_cost_of_borrowing(self):
        q = loan_quote.quote_loan(1_000_000, 0, 12, "monthly", fees_cents=5_000)
        assert q.cost_of_borrowing_cents == 5_000  # 0 interest + 5000 fees

    def test_apr_equals_contract_rate_when_no_fees(self):
        # an installment loan with no fees discloses its contract rate as the APR
        q = loan_quote.quote_loan(1_000_000, 1290, 12, "monthly")
        assert q.apr_bps == 1290

    def test_apr_exceeds_contract_rate_with_fees(self):
        q = loan_quote.quote_loan(1_000_000, 1290, 12, "monthly", fees_cents=20_000)
        assert q.apr_bps > 1290

    def test_product_terms_defaults(self):
        params = loan_quote.product_terms({"apr_bps": 999, "term_options": [12, 24]})
        assert params["annual_rate_bps"] == 999
        assert params["term_options"] == [12, 24]
        assert {f["value"] for f in params["frequencies"]} == set(loan_quote.FREQUENCIES)

    def test_term_range_from_options(self):
        # the slider range derives from the options when no explicit min/max
        params = loan_quote.product_terms({"term_options": [12, 24, 36, 60]})
        assert params["term_min"] == 12 and params["term_max"] == 60

    def test_term_range_explicit(self):
        # explicit term_min/term_max win (matches Dave's 3–84 month slider)
        params = loan_quote.product_terms({"term_min": 3, "term_max": 84})
        assert params["term_min"] == 3 and params["term_max"] == 84

    def test_rate_from_apr_range_floor_matches_booking(self):
        # apr_range is PERCENT; the calculator uses the floor — same as
        # loan_servicing._resolve_pricing, so the quote matches the booked rate.
        params = loan_quote.product_terms({"apr_range": [8.99, 24.99]})
        assert params["annual_rate_bps"] == 899

    def test_default_rate_matches_booking_default(self):
        # an unconfigured product quotes the same default it would book at (12.99%)
        assert loan_quote.product_terms({})["annual_rate_bps"] == 1299


@pytest.fixture
def client(db_session: Session):
    app = FastAPI()
    app.include_router(applicant_router, prefix="/api/applicant/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _product(db) -> PlatformCreditProduct:
    return db.query(PlatformCreditProduct).filter(
        PlatformCreditProduct.code == "dental_full_arch_v1").first()


class TestQuoteEndpoints:
    def test_terms_then_quote(self, client, db_session):
        product = _product(db_session)
        terms = client.get(f"{_BASE}/{product.id}/terms")
        assert terms.status_code == 200, terms.text
        body = terms.json()
        assert body["term_options"] and body["frequencies"]
        term = body["term_options"][0]
        freq = body["frequencies"][0]["value"]

        amount = (product.min_amount_cents + product.max_amount_cents) // 2
        q = client.post(f"{_BASE}/{product.id}/quote",
                        json={"amount_cents": amount, "term_months": term, "frequency": freq})
        assert q.status_code == 200, q.text
        data = q.json()
        assert data["installment_cents"] > 0
        assert data["total_of_payments_cents"] >= amount
        assert data["frequency_label"]

    def test_amount_out_of_bounds_422(self, client, db_session):
        product = _product(db_session)
        r = client.post(f"{_BASE}/{product.id}/quote",
                        json={"amount_cents": product.max_amount_cents + 1, "term_months": 12, "frequency": "monthly"})
        assert r.status_code == 422

    def test_bad_frequency_422(self, client, db_session):
        product = _product(db_session)
        r = client.post(f"{_BASE}/{product.id}/quote",
                        json={"amount_cents": product.min_amount_cents, "term_months": 12, "frequency": "annually"})
        assert r.status_code == 422

    def test_unknown_product_404(self, client):
        r = client.get(f"{_BASE}/{uuid.uuid4()}/terms")
        assert r.status_code == 404
