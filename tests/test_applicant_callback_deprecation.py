"""The deprecated applicant callback (P6.6) still works and advertises deprecation."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import get_notification_dispatcher
from app.main import app
from app.models.platform.credit_product import PlatformCreditProduct
from app.services.mock_notification_dispatcher import MockNotificationDispatcher

_BASE = "/api/applicant/v1"


@pytest.fixture
def dispatcher(db_session: Session):
    disp = MockNotificationDispatcher(db_session)
    app.dependency_overrides[get_notification_dispatcher] = lambda: disp
    yield disp
    app.dependency_overrides.pop(get_notification_dispatcher, None)


def _product_id(db: Session):
    return db.query(PlatformCreditProduct).filter(
        PlatformCreditProduct.code == "dental_full_arch_v1"
    ).first().id


def _setup(client: TestClient, db: Session, dispatcher):
    resp = client.post(
        f"{_BASE}/applications",
        json={
            "patient_profile": {"legal_first_name": "Dep", "email": f"dep-{uuid.uuid4().hex[:8]}@example.com"},
            "credit_product_id": str(_product_id(db)),
            "requested_amount_cents": 2_500_000,
            "requested_amount_source": "clinic",
            "contact_method": "email",
        },
    )
    app_id = resp.json()["application_id"]
    token = dispatcher._sent[-1]["token"]
    jwt = client.post(
        f"{_BASE}/auth/magic-link/exchange", json={"application_id": app_id, "token": token}
    ).json()["jwt"]
    headers = {"Authorization": f"Bearer {jwt}"}
    client.post(f"{_BASE}/applications/{app_id}/consents/id_verification", headers=headers)
    client.post(f"{_BASE}/applications/{app_id}/verifications/id_verification/initiate", headers=headers)
    return app_id, headers


def _callback(client: TestClient, app_id, headers):
    return client.post(
        f"{_BASE}/applications/{app_id}/verifications/id_verification/callback",
        headers=headers,
        json={"vendor_event_id": f"dep-{uuid.uuid4().hex[:8]}", "result": "passed",
              "rich_payload": {"confidence": 0.95}},
    )


class TestCallbackDeprecation:
    def test_callback_still_works(self, client: TestClient, db_session: Session, dispatcher):
        app_id, headers = _setup(client, db_session, dispatcher)
        r = _callback(client, app_id, headers)
        assert r.status_code == 200, r.text
        assert "verification_id" in r.json()

    def test_callback_has_deprecation_header(self, client: TestClient, db_session: Session, dispatcher):
        app_id, headers = _setup(client, db_session, dispatcher)
        r = _callback(client, app_id, headers)
        assert r.headers.get("Deprecation") == "true"

    def test_callback_has_sunset_header(self, client: TestClient, db_session: Session, dispatcher):
        app_id, headers = _setup(client, db_session, dispatcher)
        r = _callback(client, app_id, headers)
        assert "Sunset" in r.headers
