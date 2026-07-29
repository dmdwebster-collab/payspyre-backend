"""ACTIVATION REWORK WAVE 6 — the borrower still hears from us.

Before the cutover every customer-facing lifecycle notification hung off events
that only exist once a LOAN exists (``loan_agreement_sent`` /
``loan_agreement_signed`` / ``loan_disbursed``), and the approval email was built
from the booked loan. After the cutover none of those fire until activation, so
without wiring the pre-loan events the borrower would be told NOTHING for the
whole new lifecycle — approved, please sign, signed, live.

These tests pin that wiring against the real test DB.
"""
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.event import PlatformEvent  # noqa: F401  (WORM audit rows)
from app.models.platform.loan_offer import PlatformLoanOffer
from app.models.platform.patient import PlatformPatient
from app.services.notification_processor import NotificationProcessor


def _patient(db: Session) -> PlatformPatient:
    p = PlatformPatient(
        email=f"w6-{uuid.uuid4().hex[:8]}@example.com",
        phone_e164="+15555550123",
        legal_first_name="Dana",
        legal_last_name="Okafor",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _application(db: Session, patient: PlatformPatient, *, status: str):
    from app.models.platform.credit_product import PlatformCreditProduct

    product = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
    )
    row = PlatformCreditApplication(
        patient_id=patient.id,
        credit_product_id=product.id,
        credit_product_version=product.version,
        requested_amount_cents=1_800_000,
        requested_amount_source="clinic",
        status=status,
        decision={"outcome": "approved", "amount_cents": 1_800_000,
                  "apr_bps": 1299, "term_months": 24},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _emit(db: Session, *, event_type: str, application, payload: dict) -> int:
    ev = PlatformEvent(
        event_type=event_type,
        actor="system",
        patient_id=application.patient_id,
        application_id=application.id,
        payload={"v": 1, "application_id": str(application.id), **payload},
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev.id


def _sent_types(db: Session, sid: int) -> list[str]:
    rows = db.execute(
        text(
            "SELECT payload FROM platform_events WHERE event_type='notification_sent' "
            "AND payload->>'source_event_id' = :s"
        ),
        {"s": str(sid)},
    ).all()
    return sorted(r[0]["notification_type"] for r in rows)


class TestPreLoanNotifications:
    def test_approval_email_sends_with_no_loan_booked(self, db_session: Session):
        """The approval email used to be built from the booked loan and was
        SKIPPED when none existed — which, after the cutover, is always."""
        patient = _patient(db_session)
        application = _application(db_session, patient, status="offer_acceptance")
        sid = _emit(
            db_session,
            event_type="decision_made",
            application=application,
            payload={"after": {"status": "approved", "decision": "approved"}},
        )
        res = NotificationProcessor(db_session).run()
        assert res.sent == 1
        assert _sent_types(db_session, sid) == ["application_approved"]

    def test_application_agreement_events_notify_the_borrower(self, db_session: Session):
        patient = _patient(db_session)
        application = _application(db_session, patient, status="agreement_signature")
        s1 = _emit(db_session, event_type="application_agreement_sent",
                   application=application, payload={"after": {}})
        s2 = _emit(db_session, event_type="application_agreement_signed",
                   application=application, payload={"after": {}})
        res = NotificationProcessor(db_session).run()
        assert res.sent == 2
        assert _sent_types(db_session, s1) == ["offer_accepted_signing"]
        assert _sent_types(db_session, s2) == ["agreement_signed"]

    def test_loan_activated_sends_the_activation_notice(self, db_session: Session):
        """``activate_loan`` emits ``loan_activated``, never ``loan_disbursed``
        (activation IS the disbursement) — it must map to the same notice."""
        from app.models.platform.loan import PlatformLoan

        patient = _patient(db_session)
        application = _application(db_session, patient, status="active")
        loan = PlatformLoan(
            application_id=application.id, principal_cents=1_800_000,
            annual_rate_bps=1299, term_months=24, status="active",
            principal_balance_cents=1_800_000, booked_at_activation=True,
        )
        db_session.add(loan)
        db_session.commit()
        sid = _emit(
            db_session,
            event_type="loan_activated",
            application=application,
            payload={"loan_id": str(loan.id), "after": {"status": "active"}},
        )
        res = NotificationProcessor(db_session).run()
        assert res.sent == 1
        assert _sent_types(db_session, sid) == ["loan_activated"]

    def test_offer_terms_drive_the_context_when_an_offer_is_accepted(
        self, db_session: Session
    ):
        """With no loan to read, the email's terms come from the ACCEPTED offer."""
        from datetime import datetime, timedelta, timezone

        patient = _patient(db_session)
        application = _application(db_session, patient, status="agreement_signature")
        db_session.add(PlatformLoanOffer(
            application_id=application.id,
            credit_product_id=application.credit_product_id,
            amount_cents=1_650_000, term_months=18, annual_rate_bps=1499,
            payment_frequency="monthly", status="accepted",
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            created_by="admin-1",
        ))
        db_session.commit()

        processor = NotificationProcessor(db_session)
        ctx = processor._approval_context(
            {"application_id": application.id, "patient_id": patient.id}
        )
        assert ctx is not None
        assert ctx["amount"] == "$16,500.00"
        assert ctx["term"] == "18 months"
        assert ctx["interest_rate"] == "14.99%"
        assert ctx["monthly_payment"] != "$0.00"
        assert f"/applications/{application.id}/agreement" in ctx["agreement_url"]
        assert ctx["_loan_id"] is None
