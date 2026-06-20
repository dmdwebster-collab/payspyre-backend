"""Regression tests for the LMS audit fixes:
- statement principal/interest split across multiple periods (shared-state replay)
- delinquency cure on payment (delinquent -> active)
- product amount-bounds enforcement at application creation
"""
import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.services import loan_servicing
from app.services.loan_servicing import _split_payments_windowed, record_payment


# --- fakes (mirror tests/test_loan_servicing.py) ---------------------------

class _Item:
    def __init__(self, n, principal, interest, status="scheduled", due_date=None):
        self.installment_number = n
        self.principal_cents = principal
        self.interest_cents = interest
        self.total_cents = principal + interest
        self.status = status
        self.paid_cents = 0
        self.due_date = due_date


class _Payment:
    def __init__(self, amount_cents, received_at):
        self.amount_cents = amount_cents
        self.received_at = received_at


class _Loan:
    def __init__(self, schedule, principal_balance_cents, status="active"):
        self.id = "loan-1"
        self.schedule = schedule
        self.principal_balance_cents = principal_balance_cents
        self.status = status


class _FakeSession:
    def add(self, obj): pass
    def commit(self): pass
    def refresh(self, obj): pass


# --- statement split across periods ----------------------------------------

def test_split_payments_windowed_allocates_later_payment_to_correct_installment():
    # inst1 = 900 principal / 100 interest; inst2 = 950 principal / 50 interest.
    loan = _Loan([_Item(1, 900, 100), _Item(2, 950, 50)], principal_balance_cents=0)
    pA = _Payment(1000, datetime(2026, 1, 15, tzinfo=timezone.utc))  # pays inst1
    pB = _Payment(1000, datetime(2026, 2, 15, tzinfo=timezone.utc))  # pays inst2

    def in_jan(p): return p.received_at.month == 1
    def after_jan(p): return p.received_at.month > 1
    def in_feb(p): return p.received_at.month == 2
    def after_feb(p): return p.received_at.month > 2

    # January statement: in-window = pA (inst1), after-window principal = pB (inst2).
    p_in, i_in, p_after = _split_payments_windowed(loan, [pA, pB], in_jan, after_jan)
    assert (p_in, i_in, p_after) == (900, 100, 950)

    # February statement: pA already consumed inst1, so pB must split as inst2's
    # (950 principal / 50 interest) — NOT 900/100 (the pre-fix zero-state bug).
    p_in, i_in, p_after = _split_payments_windowed(loan, [pA, pB], in_feb, after_feb)
    assert (p_in, i_in, p_after) == (950, 50, 0)


# --- delinquency cure ------------------------------------------------------

def test_payment_cures_delinquent_loan_to_active():
    past = date(2026, 1, 1)      # overdue
    future = date(2099, 1, 1)    # not yet due
    inst1 = _Item(1, 100, 10, status="late", due_date=past)
    inst2 = _Item(2, 100, 10, status="scheduled", due_date=future)
    loan = _Loan([inst1, inst2], principal_balance_cents=200, status="delinquent")

    # Pay off the overdue installment exactly (110).
    record_payment(_FakeSession(), loan, 110, datetime.now(timezone.utc), "manual")

    assert inst1.status == "paid"
    assert loan.status == "active"  # cured: no overdue-unpaid installment remains


def test_delinquent_loan_stays_delinquent_if_overdue_remains():
    past = date(2026, 1, 1)
    inst1 = _Item(1, 100, 10, status="late", due_date=past)
    inst2 = _Item(2, 100, 10, status="late", due_date=past)  # also overdue
    loan = _Loan([inst1, inst2], principal_balance_cents=200, status="delinquent")

    # Pay only the first installment; the second remains overdue + unpaid.
    record_payment(_FakeSession(), loan, 110, datetime.now(timezone.utc), "manual")

    assert inst1.status == "paid"
    assert loan.status == "delinquent"  # NOT cured — inst2 still overdue


# --- amount bounds at application creation ----------------------------------

def test_create_application_rejects_out_of_bounds_amount(db_session: Session):
    from app.models.platform.credit_product import PlatformCreditProduct
    from app.models.platform.patient import PlatformPatient
    from app.services import consent_service
    from app.services.flow_orchestrator import FlowOrchestrator, InvalidAmountError
    from app.services.verifications.mock_dispatcher import MockVerificationDispatcher

    product = (
        db_session.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
    )
    assert product is not None
    patient = PlatformPatient(email=f"bounds-{uuid.uuid4().hex[:8]}@example.com")
    db_session.add(patient)
    db_session.commit()

    orch = FlowOrchestrator(db_session, consent_service, MockVerificationDispatcher())

    # Above max.
    with pytest.raises(InvalidAmountError):
        orch.create_application(
            patient_id=patient.id,
            credit_product_id=product.id,
            requested_amount_cents=product.max_amount_cents + 1,
            requested_amount_source="clinic",
        )
    # Below min.
    with pytest.raises(InvalidAmountError):
        orch.create_application(
            patient_id=patient.id,
            credit_product_id=product.id,
            requested_amount_cents=product.min_amount_cents - 1,
            requested_amount_source="clinic",
        )
    # In-bounds succeeds.
    app = orch.create_application(
        patient_id=patient.id,
        credit_product_id=product.id,
        requested_amount_cents=product.min_amount_cents,
        requested_amount_source="clinic",
    )
    assert app.requested_amount_cents == product.min_amount_cents
