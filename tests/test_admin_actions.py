"""Lender/admin portal Phase 2 — write actions + maker-checker (live test DB)."""
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.api import api_router
from app.core.auth import get_current_user
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.loan import PlatformLoan, PlatformLoanScheduleItem
from app.models.platform.patient import PlatformPatient

_BASE = "/api/v1/admin"


def _admin(uid=None):
    return SimpleNamespace(id=uid or uuid.uuid4(),
                           roles=[SimpleNamespace(role=SimpleNamespace(name="admin"))])


def _product_id(db):
    p = db.query(PlatformCreditProduct).filter(PlatformCreditProduct.code == "dental_full_arch_v1").first()
    assert p is not None
    return p.id


def _seed_application(db, status="under_review"):
    patient = PlatformPatient(email=f"act-{uuid.uuid4().hex[:8]}@example.com",
                              legal_first_name="Jordan", legal_last_name="Lee")
    db.add(patient)
    db.commit()
    db.refresh(patient)
    app = PlatformCreditApplication(
        patient_id=patient.id, credit_product_id=_product_id(db), credit_product_version=1,
        requested_amount_cents=1_800_000, requested_amount_source="clinic", status=status,
    )
    db.add(app)
    db.commit()
    db.refresh(app)
    return app


def _seed_loan(db, status="active", balance=1_500_000):
    app = _seed_application(db, status="approved")
    loan = PlatformLoan(application_id=app.id, principal_cents=1_800_000, annual_rate_bps=1290,
                        term_months=24, status=status, principal_balance_cents=balance, currency="CAD")
    db.add(loan)
    db.commit()
    db.refresh(loan)
    db.add(PlatformLoanScheduleItem(loan_id=loan.id, installment_number=1,
                                    due_date=__import__("datetime").date.today(),
                                    principal_cents=70000, interest_cents=5000, total_cents=75000,
                                    status="scheduled", paid_cents=0))
    db.commit()
    return loan


@pytest.fixture
def app_client(db_session: Session):
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = _admin
    yield app, TestClient(app)
    app.dependency_overrides.clear()


class TestDecision:
    def test_approve_books_a_loan(self, app_client, db_session):
        app, client = app_client
        application = _seed_application(db_session)
        r = client.post(f"{_BASE}/applications/{application.id}/decision",
                        json={"outcome": "approved", "reason_codes": ["strong_credit"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "approved"
        assert body["loan_id"]  # a loan was booked

    def test_decline_sets_status(self, app_client, db_session):
        app, client = app_client
        application = _seed_application(db_session)
        r = client.post(f"{_BASE}/applications/{application.id}/decision",
                        json={"outcome": "declined", "reason_codes": ["insufficient_income"]})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "declined"

    def test_already_decided_409_without_override(self, app_client, db_session):
        app, client = app_client
        application = _seed_application(db_session, status="approved")
        r = client.post(f"{_BASE}/applications/{application.id}/decision", json={"outcome": "declined"})
        assert r.status_code == 409
        # override succeeds
        r2 = client.post(f"{_BASE}/applications/{application.id}/decision",
                         json={"outcome": "declined", "override": True})
        assert r2.status_code == 200, r2.text


class TestServicingWrites:
    def test_record_payment_reduces_balance(self, app_client, db_session):
        app, client = app_client
        loan = _seed_loan(db_session)
        r = client.post(f"{_BASE}/loans/{loan.id}/payments", json={"amount_cents": 75000, "method": "manual"})
        assert r.status_code == 200, r.text
        assert r.json()["payment_id"]

    def test_payoff_quote(self, app_client, db_session):
        app, client = app_client
        loan = _seed_loan(db_session)
        r = client.get(f"{_BASE}/loans/{loan.id}/payoff-quote")
        assert r.status_code == 200, r.text
        assert "payoff_cents" in r.json()


class TestMakerChecker:
    def test_charge_off_requires_second_approver(self, app_client, db_session):
        app, client = app_client
        loan = _seed_loan(db_session)
        maker = _admin()
        app.dependency_overrides[get_current_user] = lambda: maker
        req = client.post(f"{_BASE}/loans/{loan.id}/charge-off", json={"reason_code": "bankruptcy"})
        assert req.status_code == 200, req.text
        pending_id = req.json()["pending_action_id"]

        # appears in the pending queue
        pend = client.get(f"{_BASE}/pending-actions").json()
        assert any(p["id"] == pending_id and p["action"] == "charge_off" for p in pend)

        # the SAME admin cannot approve their own request
        same = client.post(f"{_BASE}/pending-actions/{pending_id}/approve")
        assert same.status_code == 403

        # a DIFFERENT admin approves → executes the charge-off
        app.dependency_overrides[get_current_user] = lambda: _admin()
        ok = client.post(f"{_BASE}/pending-actions/{pending_id}/approve")
        assert ok.status_code == 200, ok.text
        assert ok.json()["loan_status"] == "charged_off"
        db_session.refresh(loan)
        assert loan.status == "charged_off"

        # already decided → 409
        again = client.post(f"{_BASE}/pending-actions/{pending_id}/approve")
        assert again.status_code == 409

    def test_disburse_request_then_approve(self, app_client, db_session):
        app, client = app_client
        loan = _seed_loan(db_session, status="pending_disbursement", balance=1_800_000)
        maker = _admin()
        app.dependency_overrides[get_current_user] = lambda: maker
        pid = client.post(f"{_BASE}/loans/{loan.id}/disburse", json={}).json()["pending_action_id"]
        app.dependency_overrides[get_current_user] = lambda: _admin()
        ok = client.post(f"{_BASE}/pending-actions/{pid}/approve")
        assert ok.status_code == 200, ok.text
        assert ok.json()["disbursement_status"] == "in_progress"

    def test_reject_does_not_execute(self, app_client, db_session):
        app, client = app_client
        loan = _seed_loan(db_session)
        maker = _admin()
        app.dependency_overrides[get_current_user] = lambda: maker
        pid = client.post(f"{_BASE}/loans/{loan.id}/charge-off", json={}).json()["pending_action_id"]
        app.dependency_overrides[get_current_user] = lambda: _admin()
        rej = client.post(f"{_BASE}/pending-actions/{pid}/reject", json={"note": "premature"})
        assert rej.status_code == 200
        db_session.refresh(loan)
        assert loan.status != "charged_off"
