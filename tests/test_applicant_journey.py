"""End-to-end applicant journey through the HTTP API (P6.5), live Supabase.

The conftest ``db_session`` fixture truncates per-function, so a single test
method walks the full journey (create → auth → consents → verify → decide)
rather than 14 separate state-sharing methods. Decline / manual-review paths are
separate methods that re-run the flow with different bureau scores.

P7.3 (2026-05-30): the verification-result step used to POST to the now-deleted
applicant callback ``POST /{id}/verifications/{type}/callback``. This file
now calls ``FlowOrchestrator.handle_verification_result(...)`` directly — see
the journey-test contract note in the helper. Vendor wire transport for real
Didit / Flinks / Equifax callbacks is exhaustively covered in
``test_vendor_webhooks_didit.py`` / ``..._flinks.py`` / ``test_vendor_webhooks.py``.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

import app.services.consent_service as consent_service
from app.api.applicant.v1.deps import get_notification_dispatcher
from app.main import app
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.verification import PlatformVerification
from app.services.flow_orchestrator import CONSENT_TO_VERIFICATION_TYPE, FlowOrchestrator
from app.services.mock_notification_dispatcher import MockNotificationDispatcher
from app.services.verifications.mock_dispatcher import MockVerificationDispatcher

_BASE = "/api/applicant/v1"
_PURPOSES = ["id_verification", "soft_bureau_pull", "bank_verification", "hard_bureau_pull"]
_PENDING_STATUSES = ("pending", "in_progress")


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


def _handle_result(db: Session, app_id: str, purpose: str, score: int):
    """Apply a verification result directly via the orchestrator (Option C).

    Mirrors what the deleted applicant callback used to do: look up the pending
    PlatformVerification row for ``(application_id, mapped_vtype)`` and call
    ``orchestrator.handle_verification_result(...)``. Vendor wire transport
    (HMAC, real Didit/Flinks payload shapes) is covered exhaustively in the
    test_vendor_webhooks_* suite; this test exercises the journey state
    machine, not the wire.
    """
    mapped = CONSENT_TO_VERIFICATION_TYPE[purpose]
    verification = (
        db.query(PlatformVerification)
        .filter(
            PlatformVerification.application_id == app_id,
            PlatformVerification.verification_type == mapped,
            PlatformVerification.status.in_(_PENDING_STATUSES),
        )
        .order_by(PlatformVerification.started_at.desc())
        .first()
    )
    assert verification is not None, f"No pending {mapped} verification to apply"
    orch = FlowOrchestrator(db, consent_service, MockVerificationDispatcher())
    return orch.handle_verification_result(
        app_id,
        verification.id,
        vendor_event_id=f"evt-{purpose}-{uuid.uuid4().hex[:8]}",
        result="passed",
        rich_payload=_rich(purpose, score),
    )


def _drive(client, db, dispatcher, score: int) -> tuple[str, dict]:
    app_id, headers = _auth(client, db, dispatcher)
    for p in _PURPOSES:
        assert client.post(f"{_BASE}/applications/{app_id}/consents/{p}", headers=headers).status_code == 200
    # ADM consent is a decision-gate consent (finding #4) — required before submit.
    assert client.post(
        f"{_BASE}/applications/{app_id}/consents/automated_decision_making", headers=headers
    ).status_code == 200
    for p in _PURPOSES:
        assert client.post(
            f"{_BASE}/applications/{app_id}/verifications/{p}/initiate", headers=headers
        ).status_code == 200
    for p in _PURPOSES:
        _handle_result(db, app_id, p, score)
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

        # 6. required consents — the verification purposes PLUS automated_decision_making
        # (surfaced so the client grants it; _decide enforces it before the decision).
        req = client.get(f"{_BASE}/applications/{app_id}/consents", headers=headers)
        assert req.status_code == 200
        assert set(req.json()["required"]) == set(_PURPOSES) | {"automated_decision_making"}

        # 7. grant all consents
        for p in _PURPOSES:
            assert client.post(f"{_BASE}/applications/{app_id}/consents/{p}", headers=headers).status_code == 200
        # 7b. automated-decision-making consent — decision-gate, required before submit (finding #4)
        assert client.post(
            f"{_BASE}/applications/{app_id}/consents/automated_decision_making", headers=headers
        ).status_code == 200

        # 8-11. initiate all four verifications (pending)
        for p in _PURPOSES:
            rv = client.post(f"{_BASE}/applications/{app_id}/verifications/{p}/initiate", headers=headers)
            assert rv.status_code == 200 and rv.json()["status"] == "pending"

        # 12-13. results applied directly via the orchestrator (P7.3 — see _handle_result
        # docstring + the file header note for why this is not an HTTP call).
        # The last terminal result triggers the decision.
        decided = False
        for p in _PURPOSES:
            result = _handle_result(db_session, app_id, p, 720)
            decided = decided or result.decided
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
