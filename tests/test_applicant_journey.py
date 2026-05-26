"""End-to-end applicant journey through the HTTP API (P6.5), live Supabase.

The conftest ``db_session`` fixture truncates per-function, so a single test
method walks the full journey (create → auth → consents → verify → decide)
rather than 14 separate state-sharing methods. Decline / manual-review paths are
separate methods that re-run the flow with different bureau scores.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import get_notification_dispatcher
from app.main import app
from app.models.platform.credit_product import PlatformCreditProduct
from app.services.flow_orchestrator import CONSENT_TO_VERIFICATION_TYPE
from app.services.mock_notification_dispatcher import MockNotificationDispatcher
from app.services.verifications.mock_dispatcher import MockVerificationDispatcher

_BASE = "/api/applicant/v1"
_PURPOSES = ["id_verification", "soft_bureau_pull", "bank_verification", "hard_bureau_pull"]


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


def _rich(purpose: str, score: int) -> dict:
    vtype = CONSENT_TO_VERIFICATION_TYPE[purpose]
    override = {"credit_score": score} if vtype in ("bureau_soft", "bureau_hard") else None
    return MockVerificationDispatcher().simulate_callback(vtype, "passed", override)["rich_payload"]


def _auth(client, db, dispatcher) -> tuple[str, dict]:
    resp = client.post(
        f"{_BASE}/applications",
        json={
            "patient_profile": {"legal_first_name": "Jo", "email": f"journey-{uuid.uuid4().hex[:8]}@example.com"},
            "credit_product_id": str(_product_id(db)),
            "requested_amount_cents": 3_000_000,
            "requested_amount_source": "clinic",
            "contact_method": "email",
        },
    )
    assert resp.status_code == 201, resp.text
    app_id = resp.json()["application_id"]
    token = dispatcher._sent[-1]["token"]
    ex = client.post(f"{_BASE}/auth/magic-link/exchange", json={"application_id": app_id, "token": token})
    assert ex.status_code == 200, ex.text
    return app_id, {"Authorization": f"Bearer {ex.json()['jwt']}"}


def _drive(client, db, dispatcher, score: int) -> tuple[str, dict]:
    app_id, headers = _auth(client, db, dispatcher)
    for p in _PURPOSES:
        assert client.post(f"{_BASE}/applications/{app_id}/consents/{p}", headers=headers).status_code == 200
    for p in _PURPOSES:
        assert client.post(
            f"{_BASE}/applications/{app_id}/verifications/{p}/initiate", headers=headers
        ).status_code == 200
    for p in _PURPOSES:
        r = client.post(
            f"{_BASE}/applications/{app_id}/verifications/{p}/callback",
            headers=headers,
            json={"vendor_event_id": f"evt-{p}-{uuid.uuid4().hex[:8]}", "result": "passed",
                  "rich_payload": _rich(p, score)},
        )
        assert r.status_code == 200, r.text
    return app_id, headers


class TestApplicantJourney:
    def test_full_journey_approved(self, client: TestClient, db_session: Session, dispatcher):
        # 1. create application (new patient)
        resp = client.post(
            f"{_BASE}/applications",
            json={
                "patient_profile": {"legal_first_name": "Jo", "email": f"j-{uuid.uuid4().hex[:8]}@example.com"},
                "credit_product_id": str(_product_id(db_session)),
                "requested_amount_cents": 3_000_000,
                "requested_amount_source": "clinic",
                "contact_method": "sms",
            },
        )
        assert resp.status_code == 201, resp.text
        app_id = resp.json()["application_id"]

        # 2. magic-link event written
        issued = db_session.execute(
            text("SELECT count(*) FROM platform_events WHERE event_type='magic_link_issued' "
                 "AND application_id=:aid"),
            {"aid": app_id},
        ).scalar()
        assert issued == 1

        # 3. exchange → JWT ; 4. JWT scopes the application
        token = dispatcher._sent[-1]["token"]
        ex = client.post(f"{_BASE}/auth/magic-link/exchange", json={"application_id": app_id, "token": token})
        assert ex.status_code == 200
        headers = {"Authorization": f"Bearer {ex.json()['jwt']}"}

        # 5. GET status started
        r = client.get(f"{_BASE}/applications/{app_id}", headers=headers)
        assert r.status_code == 200 and r.json()["status"] == "started"

        # 6. required consents
        req = client.get(f"{_BASE}/applications/{app_id}/consents", headers=headers)
        assert req.status_code == 200
        assert set(req.json()["required"]) == set(_PURPOSES)

        # 7. grant all consents
        for p in _PURPOSES:
            assert client.post(f"{_BASE}/applications/{app_id}/consents/{p}", headers=headers).status_code == 200

        # 8-11. initiate all four verifications (pending)
        for p in _PURPOSES:
            rv = client.post(f"{_BASE}/applications/{app_id}/verifications/{p}/initiate", headers=headers)
            assert rv.status_code == 200 and rv.json()["status"] == "pending"

        # 12-13. callbacks; the last one (all terminal) triggers the decision
        decided = False
        for p in _PURPOSES:
            rc = client.post(
                f"{_BASE}/applications/{app_id}/verifications/{p}/callback",
                headers=headers,
                json={"vendor_event_id": f"evt-{p}", "result": "passed", "rich_payload": _rich(p, 720)},
            )
            assert rc.status_code == 200, rc.text
            decided = decided or rc.json()["decided"]
        assert decided is True

        # status approved (seed product, score 720 ≥ 680, clean identity/bank)
        r = client.get(f"{_BASE}/applications/{app_id}", headers=headers)
        assert r.json()["status"] == "approved"

        # 14. submit is idempotent — returns the existing decision
        s = client.post(f"{_BASE}/applications/{app_id}/submit", headers=headers)
        assert s.status_code == 200
        assert s.json()["already_decided"] is True
        assert s.json()["status"] == "approved"

    def test_journey_decline_path(self, client, db_session, dispatcher):
        app_id, headers = _drive(client, db_session, dispatcher, score=550)
        r = client.get(f"{_BASE}/applications/{app_id}", headers=headers)
        assert r.json()["status"] == "declined"

    def test_journey_manual_review_path(self, client, db_session, dispatcher):
        app_id, headers = _drive(client, db_session, dispatcher, score=640)
        r = client.get(f"{_BASE}/applications/{app_id}", headers=headers)
        assert r.json()["status"] == "under_review"
