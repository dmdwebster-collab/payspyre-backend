"""Endpoint tests for the applicant API (P6.5) via FastAPI TestClient.

The ``client`` fixture (conftest) overrides ``get_db`` to use the test session.
Here we additionally override ``get_notification_dispatcher`` with a single
instance the test holds, so we can read the plaintext magic-link token from its
``_sent`` list (it's never exposed over HTTP).
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import get_notification_dispatcher
from app.main import app
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.patient import PlatformPatient
from app.services.mock_notification_dispatcher import MockNotificationDispatcher

_BASE = "/api/applicant/v1"


@pytest.fixture
def dispatcher(db_session: Session):
    """A single dispatcher instance the test can introspect, injected into the app."""
    disp = MockNotificationDispatcher(db_session)
    app.dependency_overrides[get_notification_dispatcher] = lambda: disp
    yield disp
    app.dependency_overrides.pop(get_notification_dispatcher, None)


def _product_id(db: Session) -> uuid.UUID:
    return (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
        .id
    )


def _create_body(db: Session, **profile):
    if not profile:
        profile = {"legal_first_name": "Pat", "email": f"api-{uuid.uuid4().hex[:8]}@example.com"}
    return {
        "patient_profile": profile,
        "credit_product_id": str(_product_id(db)),
        "requested_amount_cents": 2_500_000,
        "requested_amount_source": "clinic",
        "contact_method": "email",
    }


def _create_and_auth(client: TestClient, db: Session, dispatcher: MockNotificationDispatcher):
    """POST /applications, exchange the magic link, return (application_id, auth_headers)."""
    resp = client.post(f"{_BASE}/applications", json=_create_body(db))
    assert resp.status_code == 201, resp.text
    app_id = resp.json()["application_id"]
    token = dispatcher._sent[-1]["token"]
    ex = client.post(
        f"{_BASE}/auth/magic-link/exchange", json={"application_id": app_id, "token": token}
    )
    assert ex.status_code == 200, ex.text
    jwt = ex.json()["jwt"]
    return app_id, {"Authorization": f"Bearer {jwt}"}


# --- POST /applications ----------------------------------------------------


class TestCreateApplication:
    def test_create_new_patient(self, client: TestClient, db_session: Session, dispatcher):
        resp = client.post(f"{_BASE}/applications", json=_create_body(db_session))
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "application_id" in body
        assert body["auth_challenge"]["method"] == "email"

    def test_create_matches_existing_patient_by_phone(self, client, db_session, dispatcher):
        phone = "+15145550199"
        existing = PlatformPatient(phone_e164=phone, email="phone-match@example.com")
        db_session.add(existing)
        db_session.commit()
        db_session.refresh(existing)
        body = _create_body(db_session, phone_e164=phone, legal_first_name="Phone")
        resp = client.post(f"{_BASE}/applications", json=body)
        assert resp.status_code == 201
        # No new patient was created with that phone.
        count = db_session.query(PlatformPatient).filter(PlatformPatient.phone_e164 == phone).count()
        assert count == 1

    def test_create_triggers_magic_link_event(self, client, db_session, dispatcher):
        client.post(f"{_BASE}/applications", json=_create_body(db_session))
        assert len(dispatcher._sent) == 1
        assert dispatcher._sent[-1]["contact_method"] == "email"


# --- auth endpoints --------------------------------------------------------


class TestAuthEndpoints:
    def test_magic_link_request_returns_202(self, client, db_session, dispatcher):
        resp = client.post(f"{_BASE}/applications", json=_create_body(db_session))
        app_id = resp.json()["application_id"]
        r = client.post(
            f"{_BASE}/auth/magic-link/request",
            json={"application_id": app_id, "contact_method": "sms"},
        )
        assert r.status_code == 202, r.text

    def test_magic_link_request_invalid_method_returns_422(self, client, db_session, dispatcher):
        resp = client.post(f"{_BASE}/applications", json=_create_body(db_session))
        app_id = resp.json()["application_id"]
        r = client.post(
            f"{_BASE}/auth/magic-link/request",
            json={"application_id": app_id, "contact_method": "carrier_pigeon"},
        )
        assert r.status_code == 422

    def test_magic_link_exchange_happy_path(self, client, db_session, dispatcher):
        app_id, headers = _create_and_auth(client, db_session, dispatcher)
        assert headers["Authorization"].startswith("Bearer ")

    def test_magic_link_exchange_bad_token_returns_401(self, client, db_session, dispatcher):
        resp = client.post(f"{_BASE}/applications", json=_create_body(db_session))
        app_id = resp.json()["application_id"]
        r = client.post(
            f"{_BASE}/auth/magic-link/exchange", json={"application_id": app_id, "token": "NOPE12"}
        )
        assert r.status_code == 401


# --- GET /applications/{id} ------------------------------------------------


class TestGetApplication:
    def test_happy_path(self, client, db_session, dispatcher):
        app_id, headers = _create_and_auth(client, db_session, dispatcher)
        r = client.get(f"{_BASE}/applications/{app_id}", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "started"

    def test_wrong_app_id_returns_403(self, client, db_session, dispatcher):
        _app_id, headers = _create_and_auth(client, db_session, dispatcher)
        other = uuid.uuid4()
        r = client.get(f"{_BASE}/applications/{other}", headers=headers)
        assert r.status_code == 403

    def test_no_jwt_returns_401(self, client, db_session, dispatcher):
        app_id, _ = _create_and_auth(client, db_session, dispatcher)
        r = client.get(f"{_BASE}/applications/{app_id}")
        assert r.status_code in (401, 422)  # missing required Authorization header


# --- consents --------------------------------------------------------------


class TestConsents:
    def test_grant_consent_happy_path(self, client, db_session, dispatcher):
        app_id, headers = _create_and_auth(client, db_session, dispatcher)
        r = client.post(f"{_BASE}/applications/{app_id}/consents/id_verification", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["purpose"] == "id_verification"
        assert r.json()["granted"] is True

    def test_grant_consent_wrong_app_returns_403(self, client, db_session, dispatcher):
        _app_id, headers = _create_and_auth(client, db_session, dispatcher)
        r = client.post(
            f"{_BASE}/applications/{uuid.uuid4()}/consents/id_verification", headers=headers
        )
        assert r.status_code == 403


# --- verifications ---------------------------------------------------------


class TestInitiateVerification:
    def test_without_consent_returns_422(self, client, db_session, dispatcher):
        app_id, headers = _create_and_auth(client, db_session, dispatcher)
        r = client.post(
            f"{_BASE}/applications/{app_id}/verifications/id_verification/initiate", headers=headers
        )
        assert r.status_code == 422  # ConsentMissingError

    def test_happy_path(self, client, db_session, dispatcher):
        app_id, headers = _create_and_auth(client, db_session, dispatcher)
        client.post(f"{_BASE}/applications/{app_id}/consents/id_verification", headers=headers)
        r = client.post(
            f"{_BASE}/applications/{app_id}/verifications/id_verification/initiate", headers=headers
        )
        assert r.status_code == 200, r.text
        assert r.json()["verification_type"] == "kyc_id"  # mapped
        assert r.json()["status"] == "pending"


# --- submit ----------------------------------------------------------------


class TestSubmit:
    def test_still_pending_returns_409(self, client, db_session, dispatcher):
        app_id, headers = _create_and_auth(client, db_session, dispatcher)
        client.post(f"{_BASE}/applications/{app_id}/consents/id_verification", headers=headers)
        client.post(
            f"{_BASE}/applications/{app_id}/verifications/id_verification/initiate", headers=headers
        )
        r = client.post(f"{_BASE}/applications/{app_id}/submit", headers=headers)
        assert r.status_code == 409
