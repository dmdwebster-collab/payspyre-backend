"""Tests for the borrower-portal read-only loan API (Borrower Portal §4).

Exercises ``app/api/applicant/v1/endpoints/loans.py`` against the REAL migrated
Postgres test DB (the ``db_session`` fixture from conftest). We seed a patient +
approved application + a PlatformLoan with mixed-state schedule items (some paid,
one upcoming, one late), a couple of payments, and a statement, then assert:

  * ``GET /loans`` lists it with the correct next_due + days_past_due,
  * ``GET /loans/{id}`` shape incl. apr_percent + installments_paid,
  * schedule / payments / statements rows,
  * CROSS-PATIENT scoping: patient B's loan 404s on detail for patient A and
    never appears in patient A's ``/loans``.

The applicant router isn't wired into the app yet (the team wires it + the
borrower-login auth separately), so this test mounts ``loans.router`` onto a
fresh FastAPI app under ``/api/applicant/v1`` and overrides ``get_db`` with the
test session — the same shape the real router will mount under.

We mint the patient JWT exactly like ``PatientAuthService._issue_jwt``: sign
``{sub: patient_id, app_ids: [...]}`` with ``settings.PATIENT_JWT_SECRET`` /
HS256. (The endpoints scope by ``claims.patient_id``, NOT app_ids.)

Run JUST this file (the suite shares a remote DB and must not be run wholesale):
    source .venv/bin/activate && \
        python -m pytest tests/test_borrower_loans.py -x -q
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from app.api.applicant.v1.endpoints import loans as loans_module
from app.core.config import settings
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanPayment,
    PlatformLoanScheduleItem,
    PlatformLoanStatement,
)
from app.models.platform.patient import PlatformPatient
from app.services.auth.patient_auth_service import JWT_ALGORITHM

_BASE = "/api/applicant/v1"

# Fixed "today" reference for deterministic days_past_due assertions.
_TODAY = datetime.now(timezone.utc).date()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _product(db) -> PlatformCreditProduct:
    # conftest re-seeds dental_full_arch_v1 after truncate; reuse it.
    return (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
    )


def _mk_patient(db, first="Jordan", last="Lee") -> PlatformPatient:
    p = PlatformPatient(legal_first_name=first, legal_last_name=last)
    db.add(p)
    db.flush()
    return p


def _mk_application(db, product, patient, amount=1_800_000) -> PlatformCreditApplication:
    app = PlatformCreditApplication(
        patient_id=patient.id,
        credit_product_id=product.id,
        credit_product_version=1,
        requested_amount_cents=amount,
        requested_amount_source="clinic",
        status="approved",
    )
    db.add(app)
    db.flush()
    return app


def _mk_loan(
    db,
    application,
    *,
    status="active",
    principal=1_000_000,
    balance=600_000,
    rate_bps=1299,
    term_months=12,
) -> PlatformLoan:
    loan = PlatformLoan(
        application_id=application.id,
        principal_cents=principal,
        annual_rate_bps=rate_bps,
        term_months=term_months,
        status=status,
        principal_balance_cents=balance,
        agreement_status="signed",
        disbursement_status="completed",
        disbursed_at=datetime.now(timezone.utc),
        currency="CAD",
    )
    db.add(loan)
    db.flush()
    return loan


def _mk_item(db, loan, n, due_date, *, total=100_000, status="scheduled", paid=0, principal=80_000):
    item = PlatformLoanScheduleItem(
        loan_id=loan.id,
        installment_number=n,
        due_date=due_date,
        principal_cents=principal,
        interest_cents=total - principal,
        total_cents=total,
        status=status,
        paid_cents=paid,
    )
    db.add(item)
    db.flush()
    return item


def _mk_payment(db, loan, amount, received_at, method="zumrails"):
    p = PlatformLoanPayment(
        loan_id=loan.id, amount_cents=amount, received_at=received_at, method=method
    )
    db.add(p)
    db.flush()
    return p


def _mk_statement(db, loan, period_start, period_end, *, opening, closing, prin, intr):
    s = PlatformLoanStatement(
        loan_id=loan.id,
        period_start=period_start,
        period_end=period_end,
        opening_balance_cents=opening,
        closing_balance_cents=closing,
        principal_paid_cents=prin,
        interest_paid_cents=intr,
    )
    db.add(s)
    db.flush()
    return s


def _seed_full_loan(db, patient):
    """Patient -> approved application -> loan with mixed-state schedule, two
    payments, one statement. Returns the loan.

    Schedule (relative to today):
      * #1 paid           (due 60d ago)
      * #2 paid           (due 30d ago)
      * #3 LATE/unpaid    (due 10d ago)   <- earliest unpaid -> next payment, dpd=10
      * #4 scheduled      (due 20d ahead)
    """
    product = _product(db)
    app = _mk_application(db, product, patient)
    loan = _mk_loan(db, app)

    _mk_item(db, loan, 1, _TODAY - timedelta(days=60), status="paid", paid=100_000)
    _mk_item(db, loan, 2, _TODAY - timedelta(days=30), status="paid", paid=100_000)
    _mk_item(db, loan, 3, _TODAY - timedelta(days=10), status="late", paid=0, total=120_000)
    _mk_item(db, loan, 4, _TODAY + timedelta(days=20), status="scheduled", paid=0)

    _mk_payment(db, loan, 100_000, datetime.now(timezone.utc) - timedelta(days=55))
    _mk_payment(db, loan, 100_000, datetime.now(timezone.utc) - timedelta(days=25))

    _mk_statement(
        db,
        loan,
        _TODAY - timedelta(days=60),
        _TODAY - timedelta(days=30),
        opening=1_000_000,
        closing=800_000,
        prin=160_000,
        intr=40_000,
    )
    db.commit()
    return loan


def _auth_headers(patient_id, app_ids=None) -> dict:
    """Mint a patient JWT the same way PatientAuthService._issue_jwt does."""
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(patient_id),
        "app_ids": [str(a) for a in (app_ids or [])],
        "iat": now,
        "exp": now + timedelta(hours=24),
    }
    token = jwt.encode(claims, settings.PATIENT_JWT_SECRET, algorithm=JWT_ALGORITHM)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Fixture: a TestClient with loans.router mounted + get_db overridden.
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db_session):
    app = FastAPI()
    app.include_router(loans_module.router, prefix=_BASE)

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_list_loans_shape_and_next_due(client, db_session):
    patient = _mk_patient(db_session)
    loan = _seed_full_loan(db_session, patient)

    r = client.get(f"{_BASE}/loans", headers=_auth_headers(patient.id))
    assert r.status_code == 200, r.text
    loans = r.json()["loans"]
    assert len(loans) == 1
    row = loans[0]
    assert row["loan_id"] == str(loan.id)
    assert row["status"] == "active"
    assert row["principal_cents"] == 1_000_000
    assert row["principal_balance_cents"] == 600_000
    assert row["currency"] == "CAD"
    assert row["disbursed_at"] is not None
    # next payment = earliest unpaid (installment #3), due 10d ago
    assert row["next_due_date"] == (_TODAY - timedelta(days=10)).isoformat()
    assert row["next_due_cents"] == 120_000  # total - paid
    assert row["days_past_due"] == 10


def test_loan_detail_shape(client, db_session):
    patient = _mk_patient(db_session)
    loan = _seed_full_loan(db_session, patient)

    r = client.get(f"{_BASE}/loans/{loan.id}", headers=_auth_headers(patient.id))
    assert r.status_code == 200, r.text
    body = r.json()
    # summary fields carry through
    assert body["loan_id"] == str(loan.id)
    assert body["days_past_due"] == 10
    assert body["next_due_cents"] == 120_000
    # detail-only fields
    assert body["annual_rate_bps"] == 1299
    assert body["apr_percent"] == pytest.approx(12.99)
    assert body["term_months"] == 12
    assert body["agreement_status"] == "signed"
    assert body["disbursement_status"] == "completed"
    assert body["amount_paid_cents"] == 200_000  # two 100k payments
    assert body["installments_total"] == 4
    assert body["installments_paid"] == 2
    assert body["product_name"] == "Dental Full Arch v1"


def test_schedule_rows_ordered(client, db_session):
    patient = _mk_patient(db_session)
    loan = _seed_full_loan(db_session, patient)

    r = client.get(f"{_BASE}/loans/{loan.id}/schedule", headers=_auth_headers(patient.id))
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert [row["installment_number"] for row in rows] == [1, 2, 3, 4]
    assert rows[2]["status"] == "late"
    assert rows[2]["total_cents"] == 120_000
    assert rows[0]["paid_cents"] == 100_000


def test_payments_rows_most_recent_first(client, db_session):
    patient = _mk_patient(db_session)
    loan = _seed_full_loan(db_session, patient)

    r = client.get(f"{_BASE}/loans/{loan.id}/payments", headers=_auth_headers(patient.id))
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert len(rows) == 2
    # most-recent first (received_at desc): the -25d payment precedes the -55d one
    assert rows[0]["received_at"] > rows[1]["received_at"]
    assert all(row["amount_cents"] == 100_000 for row in rows)
    assert rows[0]["method"] == "zumrails"


def test_statements_rows(client, db_session):
    patient = _mk_patient(db_session)
    loan = _seed_full_loan(db_session, patient)

    r = client.get(f"{_BASE}/loans/{loan.id}/statements", headers=_auth_headers(patient.id))
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert len(rows) == 1
    s = rows[0]
    assert s["opening_balance_cents"] == 1_000_000
    assert s["closing_balance_cents"] == 800_000
    assert s["principal_paid_cents"] == 160_000
    assert s["interest_paid_cents"] == 40_000


# --- cross-patient scoping --------------------------------------------------


def test_other_patients_loan_404_on_detail(client, db_session):
    patient_a = _mk_patient(db_session, first="Alice")
    patient_b = _mk_patient(db_session, first="Bob")
    _seed_full_loan(db_session, patient_a)
    loan_b = _seed_full_loan(db_session, patient_b)

    # Patient A asks for patient B's loan -> 404 (not 403; don't reveal existence).
    r = client.get(f"{_BASE}/loans/{loan_b.id}", headers=_auth_headers(patient_a.id))
    assert r.status_code == 404, r.text

    # And the sub-resources are equally invisible.
    for sub in ("schedule", "payments", "statements"):
        rr = client.get(
            f"{_BASE}/loans/{loan_b.id}/{sub}", headers=_auth_headers(patient_a.id)
        )
        assert rr.status_code == 404, rr.text


def test_other_patients_loan_absent_from_list(client, db_session):
    patient_a = _mk_patient(db_session, first="Alice")
    patient_b = _mk_patient(db_session, first="Bob")
    loan_a = _seed_full_loan(db_session, patient_a)
    loan_b = _seed_full_loan(db_session, patient_b)

    r = client.get(f"{_BASE}/loans", headers=_auth_headers(patient_a.id))
    assert r.status_code == 200, r.text
    ids = {row["loan_id"] for row in r.json()["loans"]}
    assert str(loan_a.id) in ids
    assert str(loan_b.id) not in ids


def test_missing_jwt_rejected(client, db_session):
    r = client.get(f"{_BASE}/loans")
    assert r.status_code in (401, 422)  # missing required Authorization header


def test_nonexistent_loan_404(client, db_session):
    patient = _mk_patient(db_session)
    _seed_full_loan(db_session, patient)
    r = client.get(f"{_BASE}/loans/{uuid.uuid4()}", headers=_auth_headers(patient.id))
    assert r.status_code == 404, r.text
