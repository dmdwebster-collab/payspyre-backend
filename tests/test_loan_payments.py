"""Tests for borrower payments (Pay Now / Zumrails collection) — service layer."""
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanPayment,
    PlatformLoanScheduleItem,
    PlatformLoanTransaction,
)
from app.models.platform.patient import PlatformPatient
from app.services import loan_payments
from app.services.payments.zumrails_adapter import TransactionResult, TransactionStatus


# --- seed helpers -----------------------------------------------------------


def _patient(db: Session, *, zum="zum-user-1") -> PlatformPatient:
    p = PlatformPatient(email="b@example.com", phone_e164="+15555550123",
                        legal_first_name="Bo", legal_last_name="Lee",
                        zumrails_recipient_id=zum)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _application(db: Session, patient) -> uuid.UUID:
    from app.models.platform.credit_product import PlatformCreditProduct
    from app.models.platform.credit_application import PlatformCreditApplication
    product = db.query(PlatformCreditProduct).filter(
        PlatformCreditProduct.code == "dental_full_arch_v1").first()
    row = PlatformCreditApplication(
        patient_id=patient.id, credit_product_id=product.id,
        credit_product_version=product.version, requested_amount_cents=2_500_000,
        requested_amount_source="clinic", status="approved")
    db.add(row); db.commit(); db.refresh(row)
    return row.id


def _loan(db: Session, app_id, *, status="active") -> PlatformLoan:
    loan = PlatformLoan(application_id=app_id, principal_cents=200000,
                        annual_rate_bps=1299, term_months=2, status=status,
                        principal_balance_cents=200000)
    db.add(loan); db.flush()
    for n, due in ((1, date(2026, 8, 1)), (2, date(2026, 9, 1))):
        db.add(PlatformLoanScheduleItem(
            loan_id=loan.id, installment_number=n, due_date=due,
            principal_cents=100000, interest_cents=1000, total_cents=101000, status="scheduled"))
    db.commit(); db.refresh(loan)
    return loan


class _FakeZum:
    def __init__(self, status=TransactionStatus.PENDING, txn="ZTX1"):
        self.status, self.txn, self.calls = status, txn, []

    def create_collection(self, *, payer_id, amount_cents, client_transaction_id, memo=None):
        self.calls.append((payer_id, amount_cents, client_transaction_id))
        return TransactionResult(
            transaction_id=self.txn, status=self.status, raw_status=self.status.value,
            amount_cents=amount_cents, currency="CAD", direction="collection",
            client_transaction_id=client_transaction_id)


def _payments(db, loan):
    return db.query(PlatformLoanPayment).filter(PlatformLoanPayment.loan_id == loan.id).all()


# --- initiation -------------------------------------------------------------


class TestInitiate:
    def test_pending_emits_event_no_payment_yet(self, db_session: Session):
        loan = _loan(db_session, _application(db_session, _patient(db_session)))
        zum = _FakeZum(TransactionStatus.PENDING)
        out = loan_payments.initiate_payment(db_session, loan, 101000, zumrails=zum)
        assert out["transaction_id"] == "ZTX1" and out["status"] == "pending"
        assert zum.calls[0][1] == 101000  # amount passed to Zumrails
        cnt = db_session.execute(text(
            "SELECT count(*) FROM platform_events WHERE event_type='loan_payment_initiated' "
            "AND payload->>'transaction_id'='ZTX1'")).scalar()
        assert cnt == 1
        assert _payments(db_session, loan) == []  # not settled until webhook

    def test_completed_synchronously_records_payment(self, db_session: Session):
        loan = _loan(db_session, _application(db_session, _patient(db_session)))
        zum = _FakeZum(TransactionStatus.COMPLETED)
        loan_payments.initiate_payment(db_session, loan, 101000, zumrails=zum)
        pays = _payments(db_session, loan)
        assert len(pays) == 1 and pays[0].amount_cents == 101000

    def test_inactive_loan_rejected(self, db_session: Session):
        loan = _loan(db_session, _application(db_session, _patient(db_session)),
                     status="pending_disbursement")
        with pytest.raises(loan_payments.PaymentValidationError):
            loan_payments.initiate_payment(db_session, loan, 101000, zumrails=_FakeZum())

    def test_amount_over_outstanding_rejected(self, db_session: Session):
        loan = _loan(db_session, _application(db_session, _patient(db_session)))
        with pytest.raises(loan_payments.PaymentValidationError):
            loan_payments.initiate_payment(db_session, loan, 999_999, zumrails=_FakeZum())

    def test_no_funding_profile_rejected(self, db_session: Session):
        p = _patient(db_session, zum=None)
        loan = _loan(db_session, _application(db_session, p))
        with pytest.raises(loan_payments.PaymentValidationError):
            loan_payments.initiate_payment(db_session, loan, 101000, zumrails=_FakeZum())

    def test_provider_unavailable(self, db_session: Session):
        loan = _loan(db_session, _application(db_session, _patient(db_session)))
        # zumrails=None and no configured provider → _build_zumrails_adapter returns None
        with pytest.raises(loan_payments.PaymentProviderUnavailable):
            loan_payments.initiate_payment(db_session, loan, 101000)


# --- settlement (webhook entry points) --------------------------------------


class TestSettlement:
    def test_complete_records_and_is_idempotent(self, db_session: Session):
        loan = _loan(db_session, _application(db_session, _patient(db_session)))
        loan_payments.initiate_payment(db_session, loan, 101000, zumrails=_FakeZum())

        assert loan_payments.on_collection_complete(db_session, "ZTX1") is True
        pays = _payments(db_session, loan)
        assert len(pays) == 1 and pays[0].external_ref == "ZTX1"
        db_session.refresh(loan)
        # WS-A actuals engine: the payment is allocated accrued-interest-first,
        # then principal. This loan was never disbursed (disbursed_at is None),
        # so ZERO per-diem interest has accrued and the ENTIRE 101_000 cash is
        # principal: 200_000 - 101_000 = 99_000. (The schedule's per-installment
        # 100k/1k split no longer drives the money — the ledger does.)
        assert loan.principal_balance_cents == 200_000 - 101_000

        # The immutable ledger row carries the allocation, and its buckets tie
        # out exactly to the cash received (money-conservation invariant).
        txn = (
            db_session.query(PlatformLoanTransaction)
            .filter(PlatformLoanTransaction.loan_id == loan.id)
            .one()
        )
        assert txn.txn_type == "payment" and txn.repayment_mode == "regular"
        assert (
            txn.principal_cents + txn.interest_cents
            + txn.fees_cents + txn.add_on_cents
        ) == txn.amount_cents == 101_000
        assert txn.interest_cents == 0  # no disbursement -> no accrual
        assert txn.principal_cents == 101_000

        # Replay the webhook → no double-apply (record_payment dedups on external_ref).
        assert loan_payments.on_collection_complete(db_session, "ZTX1") is True
        assert len(_payments(db_session, loan)) == 1
        db_session.refresh(loan)
        assert loan.principal_balance_cents == 99_000  # unchanged by the replay
        # And no second ledger row was appended.
        assert (
            db_session.query(PlatformLoanTransaction)
            .filter(PlatformLoanTransaction.loan_id == loan.id)
            .count()
        ) == 1

    def test_complete_unknown_txn_returns_false(self, db_session: Session):
        assert loan_payments.on_collection_complete(db_session, "NOPE") is False

    def test_failed_emits_event_no_payment(self, db_session: Session):
        loan = _loan(db_session, _application(db_session, _patient(db_session)))
        loan_payments.initiate_payment(db_session, loan, 101000, zumrails=_FakeZum())
        assert loan_payments.on_collection_failed(db_session, "ZTX1") is True
        assert _payments(db_session, loan) == []
        cnt = db_session.execute(text(
            "SELECT count(*) FROM platform_events WHERE event_type='loan_payment_failed'")).scalar()
        assert cnt == 1
