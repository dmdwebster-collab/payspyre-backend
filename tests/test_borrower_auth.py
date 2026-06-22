"""Borrower-portal email login (PatientAuthService). Live test DB, no HTTP.

Phase 0 of the borrower portal (docs/borrower_portal_spec.md §2): a returning
borrower signs in by EMAIL (patient-level magic link) and gets a JWT whose
``sub`` is their patient_id. Reuses the magic-link machinery keyed on patient_id.
"""
import uuid

import pytest
from jose import jwt
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.patient import PlatformPatient
from app.services.auth.patient_auth_service import (
    JWT_ALGORITHM,
    InvalidMagicLinkToken,
    MagicLinkLocked,
    PatientAuthService,
    _MAX_FAILED_ATTEMPTS,
)
from app.services.mock_notification_dispatcher import MockNotificationDispatcher


def _make_patient(db: Session, email: str) -> PlatformPatient:
    p = PlatformPatient(email=email)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _seed_product_id(db: Session) -> uuid.UUID:
    product = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
    )
    assert product is not None
    return product.id


def _make_application(db: Session, patient_id) -> PlatformCreditApplication:
    app = PlatformCreditApplication(
        patient_id=patient_id,
        credit_product_id=_seed_product_id(db),
        credit_product_version=1,
        requested_amount_cents=2_500_000,
        requested_amount_source="clinic",
        status="approved",  # a borrower's app is terminal/approved
    )
    db.add(app)
    db.commit()
    db.refresh(app)
    return app


def _service(db: Session):
    dispatcher = MockNotificationDispatcher(db)
    return PatientAuthService(db, dispatcher), dispatcher


def _seed_borrower(db: Session, email: str):
    patient = _make_patient(db, email)
    _make_application(db, patient.id)
    return patient


class TestBorrowerLogin:
    def test_request_then_exchange_returns_patient_jwt(self, db_session: Session):
        email = f"borrower-{uuid.uuid4().hex[:8]}@example.com"
        patient = _seed_borrower(db_session, email)
        service, dispatcher = _service(db_session)

        msg = service.request_borrower_login(email)
        assert "sign-in link" in msg["message"]
        token = dispatcher._sent[-1]["token"]

        result = service.exchange_borrower_login(email, token)
        claims = jwt.decode(result["jwt"], settings.PATIENT_JWT_SECRET, algorithms=[JWT_ALGORITHM])
        assert claims["sub"] == str(patient.id)

    def test_email_match_is_case_insensitive(self, db_session: Session):
        email = f"Mixed-{uuid.uuid4().hex[:8]}@Example.com"
        _seed_borrower(db_session, email)
        service, dispatcher = _service(db_session)
        service.request_borrower_login(email.upper())
        token = dispatcher._sent[-1]["token"]
        # exchange with a different casing still resolves the same patient
        result = service.exchange_borrower_login(email.lower(), token)
        assert result["jwt"]

    def test_unknown_email_request_is_silent_no_link_sent(self, db_session: Session):
        service, dispatcher = _service(db_session)
        before = len(dispatcher._sent)
        msg = service.request_borrower_login(f"nobody-{uuid.uuid4().hex}@example.com")
        assert "sign-in link" in msg["message"]  # same generic message
        assert len(dispatcher._sent) == before  # nothing dispatched

    def test_unknown_email_exchange_401(self, db_session: Session):
        service, _ = _service(db_session)
        with pytest.raises(InvalidMagicLinkToken):
            service.exchange_borrower_login(f"nobody-{uuid.uuid4().hex}@example.com", "ABCD1234")

    def test_wrong_token_rejected(self, db_session: Session):
        email = f"borrower-{uuid.uuid4().hex[:8]}@example.com"
        _seed_borrower(db_session, email)
        service, _ = _service(db_session)
        service.request_borrower_login(email)
        with pytest.raises(InvalidMagicLinkToken):
            service.exchange_borrower_login(email, "WRONG999")

    def test_token_is_single_use_replay_rejected(self, db_session: Session):
        email = f"borrower-{uuid.uuid4().hex[:8]}@example.com"
        _seed_borrower(db_session, email)
        service, dispatcher = _service(db_session)
        service.request_borrower_login(email)
        token = dispatcher._sent[-1]["token"]
        service.exchange_borrower_login(email, token)  # first use OK
        with pytest.raises(InvalidMagicLinkToken):
            service.exchange_borrower_login(email, token)  # replay rejected

    def test_per_patient_lockout_after_max_failures(self, db_session: Session):
        email = f"borrower-{uuid.uuid4().hex[:8]}@example.com"
        _seed_borrower(db_session, email)
        service, dispatcher = _service(db_session)
        service.request_borrower_login(email)
        good = dispatcher._sent[-1]["token"]
        for _ in range(_MAX_FAILED_ATTEMPTS):
            with pytest.raises(InvalidMagicLinkToken):
                service.exchange_borrower_login(email, "WRONG999")
        # now locked — even the correct token is refused with 429-class error
        with pytest.raises(MagicLinkLocked):
            service.exchange_borrower_login(email, good)
