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
from app.models.platform.event import PlatformEvent
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


def _seed_loan(db, status="active", balance=1_500_000, agreement_status="not_sent",
               disbursement_status="not_started"):
    app = _seed_application(db, status="approved")
    loan = PlatformLoan(application_id=app.id, principal_cents=1_800_000, annual_rate_bps=1290,
                        term_months=24, status=status, principal_balance_cents=balance, currency="CAD",
                        agreement_status=agreement_status, disbursement_status=disbursement_status)
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


class TestDevMarkSigned:
    def test_dev_mark_loan_signed(self, app_client, db_session):
        app, client = app_client
        loan = _seed_loan(db_session, status="pending_disbursement", agreement_status="not_sent")
        r = client.post(f"{_BASE}/dev/loans/{loan.id}/mark-signed")
        assert r.status_code == 200, r.text
        assert r.json()["agreement_status"] == "signed"

    def test_dev_mark_signed_404_unknown_loan(self, app_client, db_session):
        app, client = app_client
        r = client.post(f"{_BASE}/dev/loans/{uuid.uuid4()}/mark-signed")
        assert r.status_code == 404, r.text


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
        # reason_codes supplied so the request clears schema validation and the
        # already-decided guard (409) is what fires.
        r = client.post(f"{_BASE}/applications/{application.id}/decision",
                        json={"outcome": "declined", "reason_codes": ["insufficient_income"]})
        assert r.status_code == 409
        # override succeeds (WS-E: declines must carry a vetted directory reason code)
        r2 = client.post(f"{_BASE}/applications/{application.id}/decision",
                         json={"outcome": "declined", "override": True,
                               "reason_codes": ["insufficient_income"]})
        assert r2.status_code == 200, r2.text

    def test_decline_requires_directory_reason_code(self, app_client, db_session):
        """WS-E: a staff decline with no reason codes, or with a code that is not
        an active reject-directory code, is a 422 — never a silent decline."""
        app, client = app_client
        application = _seed_application(db_session)
        r = client.post(f"{_BASE}/applications/{application.id}/decision",
                        json={"outcome": "declined"})
        assert r.status_code == 422, r.text
        r2 = client.post(f"{_BASE}/applications/{application.id}/decision",
                         json={"outcome": "declined", "reason_codes": ["not_a_real_code"]})
        assert r2.status_code == 422, r2.text
        db_session.refresh(application)
        assert application.status == "under_review"  # untouched


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

    def test_disburse_request_then_approve(self, app_client, db_session, monkeypatch):
        """Approve disburse on a SIGNED loan → delegates to initiate_disbursement
        (Zumrails is stubbed; the real call is exercised by loan_lifecycle tests)."""
        app, client = app_client
        loan = _seed_loan(db_session, status="pending_disbursement", balance=1_800_000,
                          agreement_status="signed", disbursement_status="not_started")

        called = {}

        def _fake_initiate(db, loan_obj, **kw):
            called["loan_id"] = str(loan_obj.id)
            loan_obj.disbursement_status = "in_progress"
            loan_obj.disbursement_ref = "zr-mock-tx-1"
            return loan_obj

        import app.services.loan_lifecycle as _ll
        monkeypatch.setattr(_ll, "initiate_disbursement", _fake_initiate)

        maker = _admin()
        app.dependency_overrides[get_current_user] = lambda: maker
        pid = client.post(f"{_BASE}/loans/{loan.id}/disburse", json={}).json()["pending_action_id"]
        app.dependency_overrides[get_current_user] = lambda: _admin()
        ok = client.post(f"{_BASE}/pending-actions/{pid}/approve")
        assert ok.status_code == 200, ok.text
        assert called["loan_id"] == str(loan.id)            # delegated, not inline
        assert ok.json()["disbursement_status"] == "in_progress"
        assert ok.json()["disbursement_ref"] == "zr-mock-tx-1"

    def test_disburse_blocked_when_agreement_unsigned(self, app_client, db_session):
        """Audit H1/L4: a manual disburse must not fund an unsigned loan."""
        app, client = app_client
        loan = _seed_loan(db_session, status="pending_disbursement",
                          agreement_status="not_sent", disbursement_status="not_started")
        maker = _admin()
        app.dependency_overrides[get_current_user] = lambda: maker
        pid = client.post(f"{_BASE}/loans/{loan.id}/disburse", json={}).json()["pending_action_id"]
        app.dependency_overrides[get_current_user] = lambda: _admin()
        r = client.post(f"{_BASE}/pending-actions/{pid}/approve")
        assert r.status_code == 409, r.text
        assert "not signed" in r.json()["detail"].lower()

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


class TestAuditFixes:
    def test_override_to_declined_blocked_when_loan_booked(self, app_client, db_session):
        """Audit #2: overriding an APPROVED app (with a booked loan) away from
        approved would orphan the loan — block it."""
        app, client = app_client
        loan = _seed_loan(db_session, status="pending_disbursement", balance=1_800_000)
        r = client.post(f"{_BASE}/applications/{loan.application_id}/decision",
                        json={"outcome": "declined", "override": True,
                              "reason_codes": ["insufficient_income"]})
        assert r.status_code == 409, r.text
        assert "booked loan" in r.json()["detail"].lower()

    def test_override_declined_app_without_loan_still_works(self, app_client, db_session):
        """The guard must only fire for APPROVED-with-loan, not block normal overrides."""
        app, client = app_client
        application = _seed_application(db_session, status="declined")
        r = client.post(f"{_BASE}/applications/{application.id}/decision",
                        json={"outcome": "refer", "override": True})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "under_review"

    def test_payment_idempotent_on_external_ref(self, app_client, db_session):
        """Audit #3: a retry with the same external_ref must not double-post."""
        app, client = app_client
        loan = _seed_loan(db_session)
        body = {"amount_cents": 50000, "method": "manual", "external_ref": "ext-abc-123"}
        r1 = client.post(f"{_BASE}/loans/{loan.id}/payments", json=body)
        assert r1.status_code == 200, r1.text
        bal_after_first = r1.json()["principal_balance_cents"]
        r2 = client.post(f"{_BASE}/loans/{loan.id}/payments", json=body)
        assert r2.status_code == 200, r2.text
        assert r2.json().get("idempotent_replay") is True
        assert r2.json()["payment_id"] == r1.json()["payment_id"]  # same receipt
        assert r2.json()["principal_balance_cents"] == bal_after_first  # not double-reduced

    def test_payment_without_external_ref_not_deduped(self, app_client, db_session):
        """No external_ref → caller owns idempotency; two posts are two receipts."""
        app, client = app_client
        loan = _seed_loan(db_session)
        body = {"amount_cents": 10000, "method": "manual"}
        r1 = client.post(f"{_BASE}/loans/{loan.id}/payments", json=body)
        r2 = client.post(f"{_BASE}/loans/{loan.id}/payments", json=body)
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json()["payment_id"] != r2.json()["payment_id"]


class TestMoneyEventDelegation:
    """Audit H1/L5/L6: cockpit actions delegate to the hardened lifecycle entry
    points, so the WORM log gets the proper money events (it didn't before)."""

    def test_approve_emits_loan_booked(self, app_client, db_session):
        """L5: approve → book_loan → a loan_booked money event exists."""
        app, client = app_client
        application = _seed_application(db_session)
        r = client.post(f"{_BASE}/applications/{application.id}/decision",
                        json={"outcome": "approved", "reason_codes": ["ok"]})
        assert r.status_code == 200, r.text
        booked = db_session.query(PlatformEvent).filter(
            PlatformEvent.event_type == "loan_booked").all()
        assert len(booked) >= 1

    def test_charge_off_emits_loan_charged_off(self, app_client, db_session):
        """L6: charge-off approval → charge_off_loan → a loan_charged_off event."""
        app, client = app_client
        loan = _seed_loan(db_session, status="active")
        maker = _admin()
        app.dependency_overrides[get_current_user] = lambda: maker
        pid = client.post(f"{_BASE}/loans/{loan.id}/charge-off",
                          json={"reason_code": "bankruptcy"}).json()["pending_action_id"]
        app.dependency_overrides[get_current_user] = lambda: _admin()
        ok = client.post(f"{_BASE}/pending-actions/{pid}/approve")
        assert ok.status_code == 200, ok.text
        db_session.refresh(loan)
        assert loan.status == "charged_off"
        events = db_session.query(PlatformEvent).filter(
            PlatformEvent.event_type == "loan_charged_off").all()
        assert len(events) >= 1  # the material loss event is now on the WORM log

    def test_charge_off_paid_loan_is_409(self, app_client, db_session):
        """A paid-off loan can't be charged off → graceful 409 (not 500)."""
        app, client = app_client
        loan = _seed_loan(db_session, status="paid_off")
        maker = _admin()
        app.dependency_overrides[get_current_user] = lambda: maker
        pid = client.post(f"{_BASE}/loans/{loan.id}/charge-off", json={}).json()["pending_action_id"]
        app.dependency_overrides[get_current_user] = lambda: _admin()
        r = client.post(f"{_BASE}/pending-actions/{pid}/approve")
        assert r.status_code == 409, r.text


class TestCancelAction:
    """WS-E: staff Cancel — a NON-CREDIT closure (reason-coded, audited, no
    adverse-action notice)."""

    def test_cancel_moves_to_withdrawn_and_audits(self, app_client, db_session):
        app, client = app_client
        application = _seed_application(db_session)
        r = client.post(f"{_BASE}/applications/{application.id}/cancel",
                        json={"reason_code": "customer_request", "note": "called in"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "withdrawn"
        db_session.refresh(application)
        assert application.status == "withdrawn"
        ev = db_session.query(PlatformEvent).filter(
            PlatformEvent.event_type == "application_cancelled",
            PlatformEvent.application_id == application.id).first()
        assert ev is not None
        assert ev.payload["reason_code"] == "customer_request"
        assert ev.payload["borrower_facing_text"]  # snapshot for the notice
        # NON-CREDIT closure: no adverse-action notice event for this application.
        aa = db_session.query(PlatformEvent).filter(
            PlatformEvent.event_type == "adverse_action_notice_sent",
            PlatformEvent.application_id == application.id).first()
        assert aa is None

    def test_cancel_requires_active_directory_code(self, app_client, db_session):
        app, client = app_client
        application = _seed_application(db_session)
        r = client.post(f"{_BASE}/applications/{application.id}/cancel",
                        json={"reason_code": "nope_not_a_code"})
        assert r.status_code == 422, r.text
        db_session.refresh(application)
        assert application.status == "under_review"

    def test_cancel_terminal_application_is_409(self, app_client, db_session):
        app, client = app_client
        application = _seed_application(db_session, status="approved")
        r = client.post(f"{_BASE}/applications/{application.id}/cancel",
                        json={"reason_code": "customer_request"})
        assert r.status_code == 409, r.text


class TestAssignment:
    """WS-E: underwriting queue assignment (audited)."""

    def _seed_user(self, db):
        from app.models.user import User
        u = User(email=f"uw-{uuid.uuid4().hex[:8]}@payspyre.com",
                 first_name="Under", last_name="Writer", is_active=True)
        db.add(u)
        db.commit()
        db.refresh(u)
        return u

    def test_assign_unassign_roundtrip(self, app_client, db_session):
        app, client = app_client
        application = _seed_application(db_session)
        target = self._seed_user(db_session)
        r = client.post(f"{_BASE}/applications/{application.id}/assign",
                        json={"user_id": str(target.id)})
        assert r.status_code == 200, r.text
        db_session.refresh(application)
        assert application.assigned_to_user_id == target.id
        assert application.assigned_at is not None
        ev = db_session.query(PlatformEvent).filter(
            PlatformEvent.event_type == "admin_application_assigned",
            PlatformEvent.application_id == application.id).first()
        assert ev is not None and ev.payload["assigned_to"] == str(target.id)

        r2 = client.post(f"{_BASE}/applications/{application.id}/unassign")
        assert r2.status_code == 200, r2.text
        db_session.refresh(application)
        assert application.assigned_to_user_id is None
        assert application.assigned_at is None

    def test_assign_unknown_user_is_422(self, app_client, db_session):
        app, client = app_client
        application = _seed_application(db_session)
        r = client.post(f"{_BASE}/applications/{application.id}/assign",
                        json={"user_id": str(uuid.uuid4())})
        assert r.status_code == 422, r.text

    def test_queue_assigned_to_filter(self, app_client, db_session):
        app, client = app_client
        application = _seed_application(db_session)
        target = self._seed_user(db_session)
        client.post(f"{_BASE}/applications/{application.id}/assign",
                    json={"user_id": str(target.id)})
        r = client.get(f"{_BASE}/applications", params={"assigned_to": str(target.id)})
        assert r.status_code == 200, r.text
        ids = [row["id"] for row in r.json()]
        assert str(application.id) in ids


class TestDecisionReasonDirectory:
    """WS-E: admin CRUD over platform_decision_reasons (soft-deactivate only)."""

    _DIR = "/api/v1/admin/decision-reasons"

    def test_seeded_directory_lists(self, app_client, db_session):
        app, client = app_client
        r = client.get(self._DIR, params={"kind": "cancel"})
        assert r.status_code == 200, r.text
        codes = {row["code"] for row in r.json()}
        assert {"customer_request", "duplicate_application", "vendor_request",
                "offer_expired", "bank_verification_expired", "other"} <= codes

    def test_create_patch_deactivate(self, app_client, db_session):
        app, client = app_client
        code = f"test_reason_{uuid.uuid4().hex[:8]}"
        r = client.post(self._DIR, json={
            "kind": "reject", "code": code, "internal_label": "Test reason",
            "borrower_facing_text": "Test borrower text.", "sort_order": 99})
        assert r.status_code == 201, r.text
        rid = r.json()["id"]
        # duplicate code → 409
        r_dup = client.post(self._DIR, json={
            "kind": "reject", "code": code, "internal_label": "Dup",
            "borrower_facing_text": "Dup."})
        assert r_dup.status_code == 409, r_dup.text
        # patch wording + soft-deactivate
        r2 = client.patch(f"{self._DIR}/{rid}", json={
            "borrower_facing_text": "Updated text.", "active": False})
        assert r2.status_code == 200, r2.text
        assert r2.json()["active"] is False
        assert r2.json()["borrower_facing_text"] == "Updated text."
        # deactivated codes are rejected by the decline flow
        application = _seed_application(db_session)
        r3 = client.post(f"{_BASE}/applications/{application.id}/decision",
                         json={"outcome": "declined", "reason_codes": [code]})
        assert r3.status_code == 422, r3.text

    def test_invalid_slug_is_422(self, app_client, db_session):
        app, client = app_client
        r = client.post(self._DIR, json={
            "kind": "reject", "code": "Not A Slug!", "internal_label": "x",
            "borrower_facing_text": "y"})
        assert r.status_code == 422, r.text
