"""Fail-closed s.347 cap enforcement at the loan-booking chokepoint."""
import uuid

import pytest
from sqlalchemy.orm import Session

from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.patient import PlatformPatient
from app.services.loan_servicing import create_loan_from_application


def _product_id(db: Session):
    p = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
    )
    assert p is not None
    return p.id


def _approved_app(db: Session, decision):
    patient = PlatformPatient(
        email=f"crc-{uuid.uuid4().hex[:8]}@example.com",
        legal_first_name="Jordan", legal_last_name="Lee",
    )
    db.add(patient)
    db.commit()
    db.refresh(patient)
    app = PlatformCreditApplication(
        patient_id=patient.id, credit_product_id=_product_id(db), credit_product_version=1,
        requested_amount_cents=1_800_000, requested_amount_source="clinic",
        status="approved", decision=decision,
    )
    db.add(app)
    db.commit()
    db.refresh(app)
    return app


def test_booking_refuses_criminal_rate(db_session: Session):
    # decision forces a 36% rate → APR over the 35% s.347 cap → booking must refuse
    app = _approved_app(db_session, decision={"apr_bps": 3600})
    # The guard runs before any loan/schedule rows are added, so this refuses
    # fail-closed with nothing persisted.
    with pytest.raises(ValueError, match="Criminal Code"):
        create_loan_from_application(db_session, app)


def test_booking_allows_normal_rate(db_session: Session):
    app = _approved_app(db_session, decision={"apr_bps": 1290})
    loan = create_loan_from_application(db_session, app)
    assert loan.annual_rate_bps == 1290
    assert loan.status == "pending_disbursement"
