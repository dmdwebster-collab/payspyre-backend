"""Tests for the dunning scan — WS3 (Postgres-backed)."""
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.platform.loan import PlatformLoan, PlatformLoanScheduleItem
from app.models.platform.patient import PlatformPatient
from app.services.dunning import DunningPolicy, run_dunning_scan
from app.services.notification_processor import NotificationProcessor

AS_OF = date(2026, 7, 1)


def _patient(db: Session, email="dest@example.com") -> PlatformPatient:
    p = PlatformPatient(email=email, phone_e164="+15555550123",
                        legal_first_name="Jordan", legal_last_name="Lee")
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _application(db: Session, patient: PlatformPatient) -> uuid.UUID:
    from app.models.platform.credit_product import PlatformCreditProduct
    from app.models.platform.credit_application import PlatformCreditApplication

    product = db.query(PlatformCreditProduct).filter(
        PlatformCreditProduct.code == "dental_full_arch_v1"
    ).first()
    row = PlatformCreditApplication(
        patient_id=patient.id, credit_product_id=product.id,
        credit_product_version=product.version, requested_amount_cents=2_500_000,
        requested_amount_source="clinic", status="approved",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


def _loan(db: Session, app_id, status="active") -> PlatformLoan:
    loan = PlatformLoan(application_id=app_id, principal_cents=2_500_000,
                        annual_rate_bps=1299, term_months=24, status=status,
                        principal_balance_cents=2_500_000)
    db.add(loan)
    db.flush()
    db.refresh(loan)
    return loan


def _item(db: Session, loan, *, n: int, due: date, status="scheduled",
          total=111800, paid=0):
    db.add(PlatformLoanScheduleItem(
        loan_id=loan.id, installment_number=n, due_date=due,
        principal_cents=100000, interest_cents=11800, total_cents=total,
        paid_cents=paid, status=status))
    db.commit()


def _events(db, event_type):
    return db.execute(
        text("SELECT payload FROM platform_events WHERE event_type=:t ORDER BY id"),
        {"t": event_type},
    ).fetchall()


class TestScanMilestones:
    def test_emits_reminder_and_overdue_on_milestones(self, db_session: Session):
        p = _patient(db_session)
        loan = _loan(db_session, _application(db_session, p))
        _item(db_session, loan, n=1, due=AS_OF + timedelta(days=7))   # reminder (7d)
        _item(db_session, loan, n=2, due=AS_OF + timedelta(days=2))   # reminder (2d)
        _item(db_session, loan, n=3, due=AS_OF - timedelta(days=7), status="late")    # overdue 7
        _item(db_session, loan, n=4, due=AS_OF - timedelta(days=14), status="late")   # overdue 14
        _item(db_session, loan, n=5, due=AS_OF - timedelta(days=3), status="late")    # no milestone
        _item(db_session, loan, n=6, due=AS_OF + timedelta(days=5))   # no milestone

        res = run_dunning_scan(db_session, AS_OF)
        db_session.commit()
        assert res.reminders_emitted == 2
        assert res.overdue_emitted == 2
        assert len(_events(db_session, "payment_due_reminder")) == 2
        assert len(_events(db_session, "payment_overdue")) == 2

    def test_reminder_payload_shape_and_channels(self, db_session: Session):
        p = _patient(db_session)
        loan = _loan(db_session, _application(db_session, p))
        _item(db_session, loan, n=1, due=AS_OF + timedelta(days=7), total=111800, paid=1800)
        run_dunning_scan(db_session, AS_OF)
        db_session.commit()
        payload = _events(db_session, "payment_due_reminder")[0][0]
        assert payload["channels"] == ["email"]          # policy default
        assert payload["dunning_key"].endswith(":due-7")
        ctx = payload["context"]
        assert ctx["borrower_name"] == "Jordan Lee"
        assert ctx["payment_amount"] == "$1,100.00"       # 111800 - 1800 outstanding
        assert ctx["days_until_due"] == 7
        assert "late_fee" not in ctx                      # no-fee policy → no fee language

    def test_overdue_channels_email_only(self, db_session: Session):
        p = _patient(db_session)
        loan = _loan(db_session, _application(db_session, p))
        _item(db_session, loan, n=1, due=AS_OF - timedelta(days=7), status="late")
        run_dunning_scan(db_session, AS_OF)
        db_session.commit()
        payload = _events(db_session, "payment_overdue")[0][0]
        assert payload["channels"] == ["email"]    # email-only today; SMS is opt-in (backlog)
        assert payload["context"]["days_overdue"] == 7


class TestIdempotency:
    def test_rerun_emits_nothing(self, db_session: Session):
        p = _patient(db_session)
        loan = _loan(db_session, _application(db_session, p))
        _item(db_session, loan, n=1, due=AS_OF + timedelta(days=7))
        run_dunning_scan(db_session, AS_OF)
        db_session.commit()
        res2 = run_dunning_scan(db_session, AS_OF)
        db_session.commit()
        assert res2.reminders_emitted == 0
        assert res2.skipped_duplicate == 1
        assert len(_events(db_session, "payment_due_reminder")) == 1


class TestActiveOnly:
    def test_non_chaseable_loan_skipped(self, db_session: Session):
        p = _patient(db_session)
        # pending_disbursement loan should not be chased.
        loan = _loan(db_session, _application(db_session, p), status="pending_disbursement")
        _item(db_session, loan, n=1, due=AS_OF + timedelta(days=7))
        res = run_dunning_scan(db_session, AS_OF)
        db_session.commit()
        assert res.reminders_emitted == 0


class TestPolicyIsolation:
    def test_custom_policy_changes_milestones(self, db_session: Session):
        p = _patient(db_session)
        loan = _loan(db_session, _application(db_session, p))
        _item(db_session, loan, n=1, due=AS_OF + timedelta(days=5))
        # Default policy (7d, 2d) → nothing at 5d; custom (5,) → one reminder.
        assert run_dunning_scan(db_session, AS_OF).reminders_emitted == 0
        res = run_dunning_scan(db_session, AS_OF, DunningPolicy(reminder_days_before=(5,)))
        db_session.commit()
        assert res.reminders_emitted == 1


class TestEndToEnd:
    def test_dunning_then_processor_sends(self, db_session: Session):
        # Proves the two halves connect: scan emits → processor renders + sends.
        p = _patient(db_session)
        loan = _loan(db_session, _application(db_session, p))
        _item(db_session, loan, n=1, due=AS_OF - timedelta(days=7), status="late")
        run_dunning_scan(db_session, AS_OF)
        db_session.commit()

        res = NotificationProcessor(db_session).run()
        assert res.sent == 1  # email only (current channel policy)
        sent = db_session.execute(
            text("SELECT payload FROM platform_events WHERE event_type='notification_sent' "
                 "ORDER BY id")
        ).fetchall()
        channels = sorted(s[0]["channel"] for s in sent)
        assert channels == ["email"]
        assert all(s[0]["notification_type"] == "payment_overdue" for s in sent)
