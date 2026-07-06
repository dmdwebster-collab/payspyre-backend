"""Endpoint tests for the consolidated disclosure acceptance (Dave's spec).

``POST /api/applicant/v1/applications/{id}/consents/accept-all`` records consent
for ALL currently-required purposes in one call, writes a single
``consolidated_disclosure_accepted`` PlatformEvent, and is idempotent.

Because the consolidated-disclosure router is registered by the orchestrator (not
edited into ``router.py`` directly), this module mounts ``disclosure.router`` on
the shared app for the duration of the test session — additive, never removing
the existing per-item consent routes.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import get_notification_dispatcher
from app.api.applicant.v1.endpoints import disclosure
from app.main import app
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.event import PlatformEvent
from app.models.platform.consent import PlatformConsent
from app.services.mock_notification_dispatcher import MockNotificationDispatcher

_BASE = "/api/applicant/v1"


@pytest.fixture(scope="module", autouse=True)
def _mount_disclosure_router():
    """Mount the consolidated-disclosure router (orchestrator does this in prod).

    The literal ``.../consents/accept-all`` path must be matched BEFORE the
    existing ``.../consents/{purpose}`` catch-all, so the new routes are inserted
    at the FRONT of the app's route table (FastAPI matches in registration order).
    """
    accept_all_path = f"{_BASE}/applications/{{application_id}}/consents/accept-all"
    existing = {r.path for r in app.router.routes if hasattr(r, "path")}
    if accept_all_path not in existing:
        before = len(app.router.routes)
        app.include_router(disclosure.router, prefix=_BASE)
        # Move the freshly appended disclosure route(s) to the front.
        new_routes = app.router.routes[before:]
        del app.router.routes[before:]
        app.router.routes[0:0] = new_routes
    yield


@pytest.fixture
def dispatcher(db_session: Session):
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


def _create_body(db: Session):
    return {
        "patient_profile": {
            "legal_first_name": "Pat",
            "email": f"disc-{uuid.uuid4().hex[:8]}@example.com",
        },
        "credit_product_id": str(_product_id(db)),
        "requested_amount_cents": 2_500_000,
        "requested_amount_source": "clinic",
        "contact_method": "email",
    }


def _create_and_auth(client: TestClient, db: Session, dispatcher: MockNotificationDispatcher):
    resp = client.post(f"{_BASE}/applications", json=_create_body(db))
    assert resp.status_code == 201, resp.text
    app_id = resp.json()["application_id"]
    token = dispatcher._sent[-1]["token"]
    ex = client.post(
        f"{_BASE}/auth/magic-link/exchange", json={"application_id": app_id, "token": token}
    )
    assert ex.status_code == 200, ex.text
    return app_id, {"Authorization": f"Bearer {ex.json()['jwt']}"}


def _required(client: TestClient, app_id: str, headers: dict) -> list[str]:
    r = client.get(f"{_BASE}/applications/{app_id}/consents", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["required"]


class TestAcceptAll:
    def test_grants_every_required_purpose_in_one_call(self, client, db_session, dispatcher):
        app_id, headers = _create_and_auth(client, db_session, dispatcher)
        required = _required(client, app_id, headers)
        assert required, "fixture product must require at least one consent purpose"
        # Policy (Dave, 2026-07): automated_decision_making is NOT gated behind a
        # discrete borrower consent, so it is no longer in the required set.
        assert "automated_decision_making" not in required

        r = client.post(f"{_BASE}/applications/{app_id}/consents/accept-all", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["accepted"] is True
        assert set(body["granted_purposes"]) == set(required)

        # Verify via the consent store: every required purpose has an active grant.
        app_uuid = uuid.UUID(app_id)
        rows = (
            db_session.query(PlatformConsent)
            .filter(
                PlatformConsent.application_id == app_uuid,
                PlatformConsent.consent_granted.is_(True),
                PlatformConsent.revoked_at.is_(None),
            )
            .all()
        )
        granted_purposes = {row.purpose for row in rows}
        assert set(required).issubset(granted_purposes)

    def test_writes_consolidated_disclosure_accepted_event(self, client, db_session, dispatcher):
        app_id, headers = _create_and_auth(client, db_session, dispatcher)
        required = _required(client, app_id, headers)

        r = client.post(
            f"{_BASE}/applications/{app_id}/consents/accept-all",
            headers=headers,
            json={"accepted": True, "disclosure_version": "consolidated_v1"},
        )
        assert r.status_code == 200, r.text

        app_uuid = uuid.UUID(app_id)
        events = (
            db_session.query(PlatformEvent)
            .filter(
                PlatformEvent.application_id == app_uuid,
                PlatformEvent.event_type == "consolidated_disclosure_accepted",
            )
            .all()
        )
        assert len(events) == 1
        after = events[0].payload["after"]
        assert after["accepted"] is True
        assert set(after["purposes"]) == set(required)
        assert after["disclosure_version"] == "consolidated_v1"
        # The canonical application disclaimer (purpose + version) is recorded on the
        # acceptance event for an auditable trail of the exact language attested to.
        assert after["application_disclaimer_purpose"] == "application_disclaimer"
        assert after["application_disclaimer_version"] == "v1_2026-07"

    def test_idempotent_reaccept_is_noop(self, client, db_session, dispatcher):
        app_id, headers = _create_and_auth(client, db_session, dispatcher)
        required = _required(client, app_id, headers)

        first = client.post(f"{_BASE}/applications/{app_id}/consents/accept-all", headers=headers)
        assert first.status_code == 200, first.text
        second = client.post(f"{_BASE}/applications/{app_id}/consents/accept-all", headers=headers)
        assert second.status_code == 200, second.text
        assert set(second.json()["granted_purposes"]) == set(required)

        app_uuid = uuid.UUID(app_id)
        # No duplicate active consent rows per purpose.
        rows = (
            db_session.query(PlatformConsent)
            .filter(
                PlatformConsent.application_id == app_uuid,
                PlatformConsent.consent_granted.is_(True),
                PlatformConsent.revoked_at.is_(None),
            )
            .all()
        )
        by_purpose: dict[str, int] = {}
        for row in rows:
            by_purpose[row.purpose] = by_purpose.get(row.purpose, 0) + 1
        for purpose in required:
            assert by_purpose.get(purpose) == 1, f"duplicate active consent for {purpose}"

    def test_get_disclosure_serves_canonical_application_disclaimer(
        self, client, db_session, dispatcher
    ):
        """TASK 1: the canonical full application disclaimer is served in-flow,
        versioned and verbatim (Dave's exact wording)."""
        app_id, headers = _create_and_auth(client, db_session, dispatcher)
        r = client.get(f"{_BASE}/applications/{app_id}/disclosure", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["purpose"] == "application_disclaimer"
        assert body["version"] == "v1_2026-07"
        assert "By submitting this application, you:" in body["text"]
        assert "Accept and agree to our Terms & Conditions and Privacy Policy." in body["text"]

    def test_not_owned_returns_403(self, client, db_session, dispatcher):
        _app_id, headers = _create_and_auth(client, db_session, dispatcher)
        other = uuid.uuid4()
        r = client.post(f"{_BASE}/applications/{other}/consents/accept-all", headers=headers)
        assert r.status_code == 403

    def test_no_jwt_returns_401(self, client, db_session, dispatcher):
        app_id, _ = _create_and_auth(client, db_session, dispatcher)
        r = client.post(f"{_BASE}/applications/{app_id}/consents/accept-all")
        assert r.status_code in (401, 422)
