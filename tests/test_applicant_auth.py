"""Unit tests for PatientAuthService (P6.5) — magic-link + JWT. Live Supabase, no HTTP."""
import uuid
from datetime import datetime, timezone

import pytest
from jose import JWTError, jwt
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.patient import PlatformPatient
from app.services.auth.patient_auth_service import (
    JWT_ALGORITHM,
    InvalidMagicLinkToken,
    PatientAuthService,
)
from app.services.notifications.mock_notification_dispatcher import MockNotificationDispatcher


def _make_patient(db: Session) -> PlatformPatient:
    p = PlatformPatient(email=f"auth-{uuid.uuid4().hex[:8]}@example.com")
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
        status="started",
    )
    db.add(app)
    db.commit()
    db.refresh(app)
    return app


def _service(db: Session) -> tuple[PatientAuthService, MockNotificationDispatcher]:
    dispatcher = MockNotificationDispatcher(db)
    return PatientAuthService(db, dispatcher), dispatcher


def _issued_event(db: Session, application_id) -> dict | None:
    row = db.execute(
        text(
            "SELECT payload FROM platform_events WHERE event_type='magic_link_issued' "
            "AND application_id=:aid ORDER BY occurred_at DESC LIMIT 1"
        ),
        {"aid": str(application_id)},
    ).first()
    return row[0] if row else None


class TestRequestMagicLink:
    def test_request_sms_writes_event(self, db_session: Session):
        patient = _make_patient(db_session)
        app = _make_application(db_session, patient.id)
        service, dispatcher = _service(db_session)
        service.request_magic_link(patient.id, app.id, "sms")

        payload = _issued_event(db_session, app.id)
        assert payload is not None
        assert payload["contact_method"] == "sms"
        assert payload["consumed"] is False
        assert len(payload["token_hash"]) == 64  # sha256 hex
        ttl = datetime.fromisoformat(payload["ttl_expires_at"])
        secs = (ttl - datetime.now(timezone.utc)).total_seconds()
        assert 800 < secs <= 900  # ~15 min in the future

    def test_request_email_writes_event(self, db_session: Session):
        patient = _make_patient(db_session)
        app = _make_application(db_session, patient.id)
        service, _ = _service(db_session)
        service.request_magic_link(patient.id, app.id, "email")
        payload = _issued_event(db_session, app.id)
        assert payload["contact_method"] == "email"


class TestExchangeMagicLink:
    def test_exchange_valid_token_returns_jwt(self, db_session: Session):
        patient = _make_patient(db_session)
        app = _make_application(db_session, patient.id)
        service, dispatcher = _service(db_session)
        service.request_magic_link(patient.id, app.id, "sms")
        raw_token = dispatcher._sent[-1]["token"]

        result = service.exchange_magic_link(app.id, raw_token)
        assert "jwt" in result and "expires_at" in result
        decoded = jwt.decode(result["jwt"], settings.PATIENT_JWT_SECRET, algorithms=[JWT_ALGORITHM])
        assert decoded["sub"] == str(patient.id)
        assert str(app.id) in decoded["app_ids"]

    def test_exchange_expired_token_raises_401(self, db_session: Session):
        patient = _make_patient(db_session)
        app = _make_application(db_session, patient.id)
        service, dispatcher = _service(db_session)
        # Backdate the issuance via a negative TTL.
        dispatcher.send_magic_link(patient.id, app.id, "sms", "EXPIRED1", ttl_seconds=-10)
        db_session.commit()
        with pytest.raises(InvalidMagicLinkToken, match="expired"):
            service.exchange_magic_link(app.id, "EXPIRED1")

    def test_exchange_consumed_token_raises_401(self, db_session: Session):
        patient = _make_patient(db_session)
        app = _make_application(db_session, patient.id)
        service, dispatcher = _service(db_session)
        service.request_magic_link(patient.id, app.id, "sms")
        raw_token = dispatcher._sent[-1]["token"]

        service.exchange_magic_link(app.id, raw_token)  # first use
        with pytest.raises(InvalidMagicLinkToken, match="consumed"):
            service.exchange_magic_link(app.id, raw_token)  # replay

    def test_exchange_wrong_token_raises_401(self, db_session: Session):
        patient = _make_patient(db_session)
        app = _make_application(db_session, patient.id)
        service, _ = _service(db_session)
        service.request_magic_link(patient.id, app.id, "sms")
        with pytest.raises(InvalidMagicLinkToken):
            service.exchange_magic_link(app.id, "WRONG9")


class TestJwtClaims:
    def test_jwt_contains_all_nonterminal_app_ids(self, db_session: Session):
        patient = _make_patient(db_session)
        app1 = _make_application(db_session, patient.id)
        app2 = _make_application(db_session, patient.id)
        service, dispatcher = _service(db_session)
        service.request_magic_link(patient.id, app1.id, "sms")
        raw_token = dispatcher._sent[-1]["token"]
        result = service.exchange_magic_link(app1.id, raw_token)
        decoded = jwt.decode(result["jwt"], settings.PATIENT_JWT_SECRET, algorithms=[JWT_ALGORITHM])
        assert {str(app1.id), str(app2.id)} == set(decoded["app_ids"])

    def test_jwt_expires_at_is_24h(self, db_session: Session):
        patient = _make_patient(db_session)
        app = _make_application(db_session, patient.id)
        service, dispatcher = _service(db_session)
        service.request_magic_link(patient.id, app.id, "sms")
        raw_token = dispatcher._sent[-1]["token"]
        result = service.exchange_magic_link(app.id, raw_token)
        expires_at = datetime.fromisoformat(result["expires_at"])
        delta = (expires_at - datetime.now(timezone.utc)).total_seconds()
        assert abs(delta - 86_400) < 5

    def test_jwt_signed_with_correct_secret(self, db_session: Session):
        patient = _make_patient(db_session)
        app = _make_application(db_session, patient.id)
        service, dispatcher = _service(db_session)
        service.request_magic_link(patient.id, app.id, "sms")
        raw_token = dispatcher._sent[-1]["token"]
        result = service.exchange_magic_link(app.id, raw_token)
        # correct secret decodes
        jwt.decode(result["jwt"], settings.PATIENT_JWT_SECRET, algorithms=[JWT_ALGORITHM])
        # wrong secret raises
        with pytest.raises(JWTError):
            jwt.decode(result["jwt"], "the-wrong-secret", algorithms=[JWT_ALGORITHM])
