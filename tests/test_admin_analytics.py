"""Lender/admin portal Phase 4 — portfolio analytics (live test DB)."""
import uuid
from datetime import datetime, timezone, date
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
from app.models.platform.loan import PlatformLoan, PlatformLoanPayment, PlatformLoanScheduleItem
from app.models.platform.patient import PlatformPatient

_BASE = "/api/v1/admin/analytics"


def _admin():
    return SimpleNamespace(id=uuid.uuid4(), roles=[SimpleNamespace(role=SimpleNamespace(name="admin"))])


def _product_id(db):
    return db.query(PlatformCreditProduct).filter(PlatformCreditProduct.code == "dental_full_arch_v1").first().id


def _loan(db, *, status="active", principal=1_800_000, balance=1_500_000, rate_bps=1290, disbursed=None):
    patient = PlatformPatient(email=f"an-{uuid.uuid4().hex[:8]}@example.com",
                              legal_first_name="A", legal_last_name="B")
    db.add(patient); db.commit(); db.refresh(patient)
    app = PlatformCreditApplication(patient_id=patient.id, credit_product_id=_product_id(db),
                                    credit_product_version=1, requested_amount_cents=principal,
                                    requested_amount_source="clinic", status="approved")
    db.add(app); db.commit(); db.refresh(app)
    loan = PlatformLoan(application_id=app.id, principal_cents=principal, annual_rate_bps=rate_bps,
                        term_months=24, status=status, principal_balance_cents=balance, currency="CAD",
                        disbursed_at=disbursed or datetime(2026, 5, 15, tzinfo=timezone.utc))
    db.add(loan); db.commit(); db.refresh(loan)
    return loan


@pytest.fixture
def client(db_session: Session):
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = _admin
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_portfolio_summary(client, db_session):
    _loan(db_session, status="active", principal=2_000_000, balance=1_800_000, rate_bps=1200)
    _loan(db_session, status="delinquent", principal=1_000_000, balance=900_000, rate_bps=1500)
    _loan(db_session, status="charged_off", principal=500_000, balance=0)
    r = client.get(f"{_BASE}/portfolio")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["originated_count"] >= 3
    assert b["outstanding_principal_cents"] >= 2_700_000  # 1.8m + 0.9m live
    assert b["charged_off_count"] >= 1
    assert b["delinquent_principal_cents"] >= 900_000
    # weighted APR sits between the two live loans' rates
    assert 1200 <= b["weighted_avg_apr_bps"] <= 1500
    assert b["delinquency_rate"] is not None
    assert "active" in b["by_status"]


def test_vintage_cohorts(client, db_session):
    _loan(db_session, disbursed=datetime(2026, 3, 10, tzinfo=timezone.utc))
    _loan(db_session, disbursed=datetime(2026, 3, 20, tzinfo=timezone.utc))
    _loan(db_session, status="charged_off", principal=600_000, balance=0,
          disbursed=datetime(2026, 4, 5, tzinfo=timezone.utc))
    r = client.get(f"{_BASE}/vintage")
    assert r.status_code == 200, r.text
    cohorts = {c["cohort"]: c for c in r.json()}
    assert cohorts["2026-03"]["originated_count"] == 2
    assert cohorts["2026-04"]["charged_off_count"] == 1
    assert cohorts["2026-04"]["charge_off_rate"] == 1.0


def test_originations_trend(client, db_session):
    _loan(db_session, disbursed=datetime(2026, 2, 1, tzinfo=timezone.utc))
    r = client.get(f"{_BASE}/originations?months=12")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert any(p["month"] == "2026-02" and p["count"] >= 1 for p in rows)


def test_cei(client, db_session):
    loan = _loan(db_session)
    db_session.add(PlatformLoanScheduleItem(loan_id=loan.id, installment_number=1,
                                            due_date=date.today(), principal_cents=70000,
                                            interest_cents=5000, total_cents=75000, status="scheduled", paid_cents=0))
    db_session.add(PlatformLoanPayment(loan_id=loan.id, amount_cents=75000,
                                       received_at=datetime.now(timezone.utc), method="manual"))
    db_session.commit()
    r = client.get(f"{_BASE}/cei?months=3")
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


def test_requires_auth(client, db_session):
    # staff is allowed (read); a bare patient is not — flip the override
    client.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=uuid.uuid4(), roles=[SimpleNamespace(role=SimpleNamespace(name="patient"))])
    r = client.get(f"{_BASE}/portfolio")
    assert r.status_code == 403
