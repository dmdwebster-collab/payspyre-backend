"""Tests for the vendor dashboard loan-book slice (Phase 2).

Exercises ``app/api/clinic/v1/endpoints/dashboard_loanbook.py`` against a REAL
migrated Postgres test DB (the ``db_session`` fixture from conftest). We seed a
vendor + applications + loans + schedule items + payments, then assert:

  * the ``loan_book_block`` / ``payments_block`` aggregate helpers,
  * the ``days_past_due`` pure helper + delinquency bucketing (spec §4),
  * the paginated ``GET /dashboard/loan-book`` rows + cursor paging,
  * CROSS-VENDOR scoping isolation (vendor A never sees vendor B's loans).

Run JUST this file:
    source .venv/bin/activate && \
        python -m pytest tests/test_dashboard_loanbook.py -x -q
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.api.clinic.v1.endpoints import dashboard_loanbook as dlb
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanPayment,
    PlatformLoanScheduleItem,
)
from app.models.platform.patient import PlatformPatient

_BASE = "/api/clinic/v1"


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _mk_vendor(db, name="Clinic A") -> Vendor:
    v = Vendor(
        business_name=name,
        business_type="corporation",
        contact_name="Owner",
        email=f"{uuid.uuid4().hex}@example.com",
        phone="+15555550100",
        address_line1="1 Main St",
        city="Toronto",
        province="ON",
        postal_code="M1M1M1",
        status="active",
    )
    db.add(v)
    db.flush()
    return v


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


def _mk_application(db, vendor, product, patient, amount=1_800_000) -> PlatformCreditApplication:
    app = PlatformCreditApplication(
        patient_id=patient.id,
        credit_product_id=product.id,
        credit_product_version=1,
        requested_amount_cents=amount,
        requested_amount_source="clinic",
        vendor_id=vendor.id,
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
    balance=None,
    disbursed=True,
    created_at=None,
) -> PlatformLoan:
    loan = PlatformLoan(
        application_id=application.id,
        principal_cents=principal,
        annual_rate_bps=1299,
        term_months=12,
        status=status,
        principal_balance_cents=principal if balance is None else balance,
        disbursement_status="completed" if disbursed else "not_started",
        disbursed_at=datetime.now(timezone.utc) if disbursed else None,
    )
    db.add(loan)
    db.flush()
    if created_at is not None:
        loan.created_at = created_at
        db.flush()
    return loan


def _mk_item(
    db, loan, n, due_date, *, total=100_000, status="scheduled", paid=0, principal=80_000
):
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


def _mk_payment(db, loan, amount, received_at):
    p = PlatformLoanPayment(
        loan_id=loan.id, amount_cents=amount, received_at=received_at, method="manual"
    )
    db.add(p)
    db.flush()
    return p


@pytest.fixture
def client_for(db_session):
    """Return a factory: client_for(vendor_id) -> TestClient scoped to vendor."""
    from app.main import app

    def override_get_db():
        yield db_session

    def _make(vendor_id):
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_clinic_user] = lambda: ClinicPrincipal(
            user=None, vendor_id=vendor_id
        )
        # The loan-book endpoint isn't wired into the production router (router.py
        # is owned by another agent), so mount it on a throwaway app for HTTP.
        test_app = FastAPI()
        test_app.include_router(dlb.router, prefix="/api/clinic/v1")
        test_app.dependency_overrides[get_db] = override_get_db
        test_app.dependency_overrides[get_current_clinic_user] = (
            lambda: ClinicPrincipal(user=None, vendor_id=vendor_id)
        )
        return TestClient(test_app)

    yield _make
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Pure helper: days_past_due + delinquency tier (spec §4)
# ---------------------------------------------------------------------------


class TestDaysPastDue:
    def test_current_when_nothing_overdue(self):
        as_of = date(2026, 6, 22)
        items = [
            type("S", (), {"due_date": date(2026, 7, 1), "status": "scheduled"})(),
        ]
        assert dlb.days_past_due(items, as_of) == 0
        assert dlb.delinquency_tier(0) == "current"

    def test_dpd_from_earliest_unpaid(self):
        as_of = date(2026, 6, 22)
        items = [
            type("S", (), {"due_date": date(2026, 6, 1), "status": "late"})(),
            type("S", (), {"due_date": date(2026, 6, 15), "status": "scheduled"})(),
        ]
        assert dlb.days_past_due(items, as_of) == 21  # from Jun 1

    def test_paid_items_ignored(self):
        as_of = date(2026, 6, 22)
        items = [
            type("S", (), {"due_date": date(2026, 6, 1), "status": "paid"})(),
            type("S", (), {"due_date": date(2026, 6, 20), "status": "scheduled"})(),
        ]
        assert dlb.days_past_due(items, as_of) == 2  # paid Jun-1 ignored

    def test_tier_buckets(self):
        assert dlb.delinquency_tier(0) == "current"
        assert dlb.delinquency_tier(1) == "late"
        assert dlb.delinquency_tier(15) == "late"          # GRACE_DAYS
        assert dlb.delinquency_tier(16) == "late"          # gap < 30 still late
        assert dlb.delinquency_tier(29) == "late"
        assert dlb.delinquency_tier(30) == "delinquent"
        assert dlb.delinquency_tier(120) == "delinquent"


# ---------------------------------------------------------------------------
# loan_book_block
# ---------------------------------------------------------------------------


class TestLoanBookBlock:
    def test_aggregates(self, db_session):
        v = _mk_vendor(db_session)
        prod = _product(db_session)
        pat = _mk_patient(db_session)

        # active loan, balance 600k
        a1 = _mk_application(db_session, v, prod, pat)
        _mk_loan(db_session, a1, status="active", principal=1_000_000, balance=600_000)
        # another active, balance 200k
        a2 = _mk_application(db_session, v, prod, pat)
        _mk_loan(db_session, a2, status="active", principal=500_000, balance=200_000)
        # paid off, disbursed
        a3 = _mk_application(db_session, v, prod, pat)
        _mk_loan(db_session, a3, status="paid_off", principal=300_000, balance=0)
        # delinquent, disbursed
        a4 = _mk_application(db_session, v, prod, pat)
        _mk_loan(db_session, a4, status="delinquent", principal=400_000, balance=400_000)
        # pending disbursement, NOT disbursed
        a5 = _mk_application(db_session, v, prod, pat)
        _mk_loan(
            db_session, a5, status="pending_disbursement", principal=700_000,
            balance=700_000, disbursed=False,
        )
        db_session.flush()

        block = dlb.loan_book_block(db_session, v.id)
        assert block == {
            "active_count": 2,
            "active_principal_balance_cents": 800_000,  # 600k + 200k
            "disbursed_count": 4,                        # all except the pending one
            "disbursed_amount_cents": 1_000_000 + 500_000 + 300_000 + 400_000,
            "paid_off_count": 1,
            "delinquent_count": 1,
        }


# ---------------------------------------------------------------------------
# payments_block
# ---------------------------------------------------------------------------


class TestPaymentsBlock:
    def test_on_time_rate_and_collected_and_tiers(self, db_session):
        v = _mk_vendor(db_session)
        prod = _product(db_session)
        pat = _mk_patient(db_session)
        today = datetime.now(timezone.utc).date()

        frm = datetime.now(timezone.utc) - timedelta(days=30)
        to = datetime.now(timezone.utc) + timedelta(days=5)

        # Loan with 2 items due in window: one paid (on-time), one late.
        a1 = _mk_application(db_session, v, prod, pat)
        l1 = _mk_loan(db_session, a1, status="active")
        _mk_item(db_session, l1, 1, today - timedelta(days=10), status="paid", paid=100_000)
        _mk_item(db_session, l1, 2, today - timedelta(days=5), status="scheduled")
        _mk_payment(db_session, l1, 100_000, datetime.now(timezone.utc) - timedelta(days=9))

        # Delinquent loan: earliest unpaid 40 days ago.
        a2 = _mk_application(db_session, v, prod, pat)
        l2 = _mk_loan(db_session, a2, status="delinquent")
        _mk_item(db_session, l2, 1, today - timedelta(days=40), status="late")

        # Payment outside the window should NOT count toward collected.
        _mk_payment(db_session, l2, 50_000, datetime.now(timezone.utc) - timedelta(days=200))
        db_session.flush()

        block = dlb.payments_block(db_session, v.id, frm, to)

        # 2 items due in window (l1#1, l1#2) — and l2#1 (40d ago) is OUTSIDE the
        # 30-day-back window, so denominator = 2, paid = 1.
        assert block["on_time_rate"] == pytest.approx(0.5)
        assert block["collected_cents"] == 100_000   # only the in-window payment
        # l1 current dpd: earliest unpaid is #2 at -5d -> late tier.
        # l2 current dpd: 40d -> delinquent tier.
        assert block["late_count"] == 1
        assert block["delinquent_count"] == 1

    def test_on_time_rate_none_when_no_due_items(self, db_session):
        v = _mk_vendor(db_session)
        frm = datetime.now(timezone.utc) - timedelta(days=30)
        to = datetime.now(timezone.utc)
        block = dlb.payments_block(db_session, v.id, frm, to)
        assert block["on_time_rate"] is None
        assert block["collected_cents"] == 0
        assert block["late_count"] == 0
        assert block["delinquent_count"] == 0


# ---------------------------------------------------------------------------
# GET /dashboard/loan-book — rows, dpd, pagination
# ---------------------------------------------------------------------------


class TestLoanBookEndpoint:
    def test_rows_and_next_due_and_dpd(self, db_session, client_for):
        v = _mk_vendor(db_session)
        prod = _product(db_session)
        pat = _mk_patient(db_session, first="Sam", last="Rivera")
        today = datetime.now(timezone.utc).date()

        a1 = _mk_application(db_session, v, prod, pat)
        loan = _mk_loan(db_session, a1, status="delinquent", principal=1_000_000, balance=900_000)
        # #1 overdue 20 days, partially paid; #2 future.
        _mk_item(
            db_session, loan, 1, today - timedelta(days=20),
            status="partial", total=100_000, paid=30_000,
        )
        _mk_item(db_session, loan, 2, today + timedelta(days=10), status="scheduled", total=100_000)
        db_session.flush()

        client = client_for(v.id)
        resp = client.get(f"{_BASE}/dashboard/loan-book")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["next_cursor"] is None
        assert len(body["rows"]) == 1
        row = body["rows"][0]
        assert row["loan_id"] == str(loan.id)
        assert row["application_id"] == str(a1.id)
        assert row["patient_name"] == "Sam Rivera"
        assert row["principal_balance_cents"] == 900_000
        assert row["status"] == "delinquent"
        assert row["days_past_due"] == 20
        # next due is the oldest unpaid installment (#1): remaining = 100k - 30k.
        assert row["next_due_cents"] == 70_000
        assert row["next_due_date"] == (today - timedelta(days=20)).isoformat()

    def test_status_filter(self, db_session, client_for):
        v = _mk_vendor(db_session)
        prod = _product(db_session)
        pat = _mk_patient(db_session)
        a1 = _mk_application(db_session, v, prod, pat)
        _mk_loan(db_session, a1, status="active")
        a2 = _mk_application(db_session, v, prod, pat)
        _mk_loan(db_session, a2, status="paid_off", balance=0)
        db_session.flush()

        client = client_for(v.id)
        resp = client.get(f"{_BASE}/dashboard/loan-book", params={"status": "paid_off"})
        assert resp.status_code == 200, resp.text
        rows = resp.json()["rows"]
        assert len(rows) == 1
        assert rows[0]["status"] == "paid_off"

    def test_cursor_pagination(self, db_session, client_for):
        v = _mk_vendor(db_session)
        prod = _product(db_session)
        pat = _mk_patient(db_session)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(3):
            a = _mk_application(db_session, v, prod, pat)
            _mk_loan(db_session, a, status="active", created_at=base + timedelta(days=i))
        db_session.flush()

        client = client_for(v.id)
        # page 1: limit 2 -> 2 rows + cursor
        r1 = client.get(f"{_BASE}/dashboard/loan-book", params={"limit": 2})
        assert r1.status_code == 200, r1.text
        b1 = r1.json()
        assert len(b1["rows"]) == 2
        assert b1["next_cursor"] is not None

        # page 2: remaining 1 row, no further cursor
        r2 = client.get(
            f"{_BASE}/dashboard/loan-book",
            params={"limit": 2, "cursor": b1["next_cursor"]},
        )
        assert r2.status_code == 200, r2.text
        b2 = r2.json()
        assert len(b2["rows"]) == 1
        assert b2["next_cursor"] is None

        # no overlap between pages
        ids1 = {r["loan_id"] for r in b1["rows"]}
        ids2 = {r["loan_id"] for r in b2["rows"]}
        assert ids1.isdisjoint(ids2)

    def test_bad_cursor_422(self, db_session, client_for):
        v = _mk_vendor(db_session)
        db_session.flush()
        client = client_for(v.id)
        resp = client.get(f"{_BASE}/dashboard/loan-book", params={"cursor": "!!!notbase64"})
        assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Cross-vendor scoping isolation
# ---------------------------------------------------------------------------


class TestScopingIsolation:
    def test_helpers_and_endpoint_isolate_vendors(self, db_session, client_for):
        prod = _product(db_session)
        today = datetime.now(timezone.utc).date()

        # Vendor A: one active loan, balance 500k, an in-window payment.
        va = _mk_vendor(db_session, name="Vendor A")
        pa = _mk_patient(db_session, first="Alice", last="A")
        aa = _mk_application(db_session, va, prod, pa)
        la = _mk_loan(db_session, aa, status="active", principal=600_000, balance=500_000)
        _mk_item(db_session, la, 1, today - timedelta(days=3), status="paid", paid=100_000)
        _mk_payment(db_session, la, 100_000, datetime.now(timezone.utc) - timedelta(days=2))

        # Vendor B: a delinquent loan + payment that must NEVER leak into A.
        vb = _mk_vendor(db_session, name="Vendor B")
        pb = _mk_patient(db_session, first="Bob", last="B")
        ab = _mk_application(db_session, vb, prod, pb)
        lb = _mk_loan(db_session, ab, status="delinquent", principal=900_000, balance=900_000)
        _mk_item(db_session, lb, 1, today - timedelta(days=45), status="late")
        _mk_payment(db_session, lb, 777_000, datetime.now(timezone.utc) - timedelta(days=1))
        db_session.flush()

        frm = datetime.now(timezone.utc) - timedelta(days=30)
        to = datetime.now(timezone.utc) + timedelta(days=1)

        # loan_book_block: A sees only its own active loan.
        block_a = dlb.loan_book_block(db_session, va.id)
        assert block_a["active_count"] == 1
        assert block_a["active_principal_balance_cents"] == 500_000
        assert block_a["delinquent_count"] == 0  # B's delinquent loan excluded

        # payments_block: A's collected excludes B's 777k payment.
        pay_a = dlb.payments_block(db_session, va.id, frm, to)
        assert pay_a["collected_cents"] == 100_000
        assert pay_a["delinquent_count"] == 0

        # endpoint: A's loan-book lists exactly A's one loan.
        client_a = client_for(va.id)
        rows_a = client_a.get(f"{_BASE}/dashboard/loan-book").json()["rows"]
        assert len(rows_a) == 1
        assert rows_a[0]["loan_id"] == str(la.id)

        # endpoint: B's loan-book lists exactly B's one loan.
        client_b = client_for(vb.id)
        rows_b = client_b.get(f"{_BASE}/dashboard/loan-book").json()["rows"]
        assert len(rows_b) == 1
        assert rows_b[0]["loan_id"] == str(lb.id)
