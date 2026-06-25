"""Demo simulation runner — drives the real engine end to end (live test DB)."""
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.api import api_router
from app.core.auth import get_current_user
from app.core.config import settings
from app.db.base import get_db
from app.services import demo_simulation

_BASE = "/api/v1/admin"


def _admin():
    return SimpleNamespace(id=uuid.uuid4(), roles=[SimpleNamespace(role=SimpleNamespace(name="admin"))])


@pytest.fixture
def client(db_session: Session):
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = _admin
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestSimulationService:
    def test_approved_run_books_loan_with_real_amortization(self, db_session):
        trace = demo_simulation.run_demo_application(db_session, score=720, amount_cents=2_000_000)
        assert trace["status"] == "approved", trace
        loan = trace["loan"]
        # the calculation engine produced a real amortized schedule
        assert loan["installments"] == loan_term(loan)
        assert loan["monthly_payment_cents"] > 0
        assert loan["total_interest_cents"] > 0  # interest actually accrues
        assert loan["total_repayment_cents"] > loan["principal_cents"]
        # a payment was posted and the balance moved
        assert trace["payment"]["balance_after_cents"] < loan["principal_cents"]
        # a statement + payoff were produced
        assert "statement" in trace and "payoff" in trace
        assert trace["payoff"]["payoff_cents"] > 0
        # the trace is a readable step-by-step lifecycle
        steps = [s["step"] for s in trace["steps"]]
        assert "loan_booked" in steps and "payment_posted" in steps

    def test_decline_run_makes_no_loan(self, db_session):
        trace = demo_simulation.run_demo_application(db_session, score=540)
        assert trace["status"] == "declined", trace
        assert "loan" not in trace


def loan_term(loan: dict) -> int:
    return loan["term_months"]


class TestEndpoints:
    def test_run_demo_endpoint(self, client):
        r = client.post(f"{_BASE}/dev/run-demo-application", json={"score": 720, "amount_cents": 1_500_000})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "approved"
        assert body["loan"]["total_interest_cents"] > 0

    def test_system_mode_simulation(self, client, monkeypatch):
        monkeypatch.setattr(settings, "USE_REAL_ADAPTERS", False)
        r = client.get(f"{_BASE}/system/mode")
        assert r.status_code == 200, r.text
        assert r.json()["mode"] == "simulation"

    def test_system_mode_live(self, client, monkeypatch):
        monkeypatch.setattr(settings, "USE_REAL_ADAPTERS", True)
        r = client.get(f"{_BASE}/system/mode")
        assert r.json()["mode"] == "live"
