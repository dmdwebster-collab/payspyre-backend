"""Endpoint tests for the borrower loan / Pay Now API (dep-override auth)."""
import uuid
from datetime import date
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import ApplicantClaims, get_current_applicant
from app.db.base import get_db
from app.main import app
from app.models.platform.loan import PlatformLoan, PlatformLoanScheduleItem
from app.models.platform.patient import PlatformPatient
from app.services import loan_payments

_BASE = "/api/applicant/v1/loans"


def _seed(db: Session):
    p = PlatformPatient(email="b@example.com", phone_e164="+15555550123",
                        legal_first_name="Bo", zumrails_recipient_id="zum-1")
    db.add(p); db.commit(); db.refresh(p)
    from app.models.platform.credit_product import PlatformCreditProduct
    from app.models.platform.credit_application import PlatformCreditApplication
    prod = db.query(PlatformCreditProduct).filter(
        PlatformCreditProduct.code == "dental_full_arch_v1").first()
    approw = PlatformCreditApplication(
        patient_id=p.id, credit_product_id=prod.id, credit_product_version=prod.version,
        requested_amount_cents=200000, requested_amount_source="clinic", status="approved")
    db.add(approw); db.commit(); db.refresh(approw)
    loan = PlatformLoan(application_id=approw.id, principal_cents=200000, annual_rate_bps=1299,
                        term_months=2, status="active", principal_balance_cents=200000)
    db.add(loan); db.flush()
    db.add(PlatformLoanScheduleItem(loan_id=loan.id, installment_number=1, due_date=date(2026, 8, 1),
            principal_cents=100000, interest_cents=1000, total_cents=101000, status="scheduled"))
    db.commit(); db.refresh(loan)
    return p, approw.id, loan


@pytest.fixture
def tc():
    return TestClient(app)


def _auth_as(patient_id, app_ids=None):
    app.dependency_overrides[get_current_applicant] = lambda: ApplicantClaims(
        patient_id=patient_id, app_ids=app_ids or [])


def _teardown():
    app.dependency_overrides.pop(get_current_applicant, None)
    app.dependency_overrides.pop(get_db, None)


class TestReads:
    # Read-shape coverage lives in test_borrower_loans.py (the canonical borrower
    # read API, PR #94). This file focuses on Pay Now; the ownership 404 below is
    # kept as a cross-check that the merged endpoints stay patient-scoped.
    def test_loan_outside_scope_is_404(self, tc, db_session):
        _p, _app_id, loan = _seed(db_session)
        app.dependency_overrides[get_db] = lambda: db_session
        _auth_as(uuid.uuid4())  # a different patient → not the loan's owner
        try:
            assert tc.get(f"{_BASE}/{loan.id}").status_code == 404
        finally:
            _teardown()


class TestPayNow:
    def test_pay_now_initiates(self, tc, db_session):
        p, _app_id, loan = _seed(db_session)
        app.dependency_overrides[get_db] = lambda: db_session
        _auth_as(p.id)
        fake = {"transaction_id": "ZTX9", "status": "pending", "amount_cents": 101000}
        try:
            with patch.object(loan_payments, "initiate_payment", return_value=fake) as m:
                r = tc.post(f"{_BASE}/{loan.id}/payments", json={"amount_cents": 101000})
            assert r.status_code == 202
            assert r.json()["transaction_id"] == "ZTX9"
            assert m.call_args.args[2] == 101000  # amount forwarded
        finally:
            _teardown()

    def test_bad_amount_rejected(self, tc, db_session):
        p, _app_id, loan = _seed(db_session)
        app.dependency_overrides[get_db] = lambda: db_session
        _auth_as(p.id)
        try:
            r = tc.post(f"{_BASE}/{loan.id}/payments", json={"amount_cents": 0})
            assert r.status_code == 422  # pydantic gt=0
        finally:
            _teardown()

    def test_provider_unavailable_returns_503(self, tc, db_session):
        p, _app_id, loan = _seed(db_session)
        app.dependency_overrides[get_db] = lambda: db_session
        _auth_as(p.id)
        try:
            # No Zumrails configured → service raises PaymentProviderUnavailable → 503
            r = tc.post(f"{_BASE}/{loan.id}/payments", json={"amount_cents": 101000})
            assert r.status_code == 503
        finally:
            _teardown()
