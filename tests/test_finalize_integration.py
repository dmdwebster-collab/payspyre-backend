"""DB-backed integration tests for the applicant Review & Finalize API.

Covers:
  * GET  /api/applicant/v1/applications/{id}/detail  — the applicant's own full
    canonical application (SIN only as last-3).
  * POST /api/applicant/v1/applications/{id}/finalize — persists corrected fields
    onto the canonical columns, replaces secondary-income lines, routes into
    underwriting (manual_review in simulation mode), and writes the event.

Mirrors tests/test_manual_application.py for the patient+application+JWT fixture.
Live test DB (per-function TRUNCATE in conftest). Runs in CI against the migrated
043 schema. Run ONLY this file locally if you have the test DB.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import get_notification_dispatcher
from app.main import app
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.services.mock_notification_dispatcher import MockNotificationDispatcher

_BASE = "/api/applicant/v1"


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


def _auth(client: TestClient, db: Session, dispatcher) -> tuple[str, dict]:
    resp = client.post(
        f"{_BASE}/applications",
        json={
            "patient_profile": {
                "legal_first_name": "Jo",
                "email": f"finalize-{uuid.uuid4().hex[:8]}@example.com",
            },
            "credit_product_id": str(_product_id(db)),
            "requested_amount_cents": 3_000_000,
            "requested_amount_source": "clinic",
            "contact_method": "email",
        },
    )
    assert resp.status_code == 201, resp.text
    app_id = resp.json()["application_id"]
    token = dispatcher._sent[-1]["token"]
    ex = client.post(
        f"{_BASE}/auth/magic-link/exchange",
        json={"application_id": app_id, "token": token},
    )
    assert ex.status_code == 200, ex.text
    return app_id, {"Authorization": f"Bearer {ex.json()['jwt']}"}


_FINALIZE_FIELDS = {
    "first_name": "Jordan",
    "last_name": "Public",
    "date_of_birth": "1990-04-15",
    "email": "jordan@example.com",
    "residence_city": "Kelowna",
    "residence_province": "BC",
    "income_type": "employed_full_time",
    "net_monthly_income_cents": 620000,
    "car_ownership": "financing",
    "monthly_car_payment_cents": 45000,
    "secondary_incomes": [
        {
            "income_type": "self_employed",
            "net_monthly_income_cents": 120000,
            "description": "freelance",
        }
    ],
}


class TestFinalize:
    def test_detail_returns_canonical_sections(self, client, db_session, dispatcher):
        app_id, headers = _auth(client, db_session, dispatcher)
        resp = client.get(f"{_BASE}/applications/{app_id}/detail", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        for section in ("personal", "identification", "residence", "primary_income",
                        "secondary_incomes", "financial"):
            assert section in body
        # SIN never present beyond last-3.
        assert "sin_last3" in body["identification"]
        assert "sin" not in body["identification"]
        assert "sin_encrypted" not in str(body)

    def test_finalize_persists_fields_routes_manual_and_writes_event(
        self, client, db_session, dispatcher
    ):
        app_id, headers = _auth(client, db_session, dispatcher)

        resp = client.post(
            f"{_BASE}/applications/{app_id}/finalize",
            json=_FINALIZE_FIELDS,
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["finalized"] is True
        # Simulation mode (default: USE_REAL_ADAPTERS off) → MANUAL underwriting.
        assert body["routed_to"] == "manual_review"
        assert body["status"] == "under_review"

        db_session.expire_all()
        application = (
            db_session.query(PlatformCreditApplication)
            .filter(PlatformCreditApplication.id == app_id)
            .first()
        )
        # Fields written to canonical columns (not JSONB).
        assert application.first_name == "Jordan"
        assert application.residence_city == "Kelowna"
        assert application.net_monthly_income_cents == 620000
        assert str(application.income_type) == "employed_full_time"
        assert str(application.car_ownership) == "financing"
        # Secondary-income child row created.
        assert len(application.secondary_incomes) == 1
        assert str(application.secondary_incomes[0].income_type) == "self_employed"
        # flow_state finalized marker + status routed via orchestrator.
        assert application.flow_state.get("finalized") is True
        assert application.status == "under_review"
        # Not auto-decided — manual application awaits human review.
        assert application.decision is None
        # Event written (keys only, PII-free).
        count = db_session.execute(
            text(
                "SELECT count(*) FROM platform_events "
                "WHERE event_type = 'application_finalized' AND application_id = :aid"
            ),
            {"aid": app_id},
        ).scalar()
        assert count == 1

    def test_finalize_real_adapter_mode_routes_to_verification(
        self, client, db_session, dispatcher, monkeypatch
    ):
        from app.core.config import settings

        monkeypatch.setattr(settings, "USE_REAL_ADAPTERS", True)
        app_id, headers = _auth(client, db_session, dispatcher)
        resp = client.post(
            f"{_BASE}/applications/{app_id}/finalize",
            json={"first_name": "Casey"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["routed_to"] == "verification"
        assert body["status"] == "verifying"

    def test_finalize_secondary_incomes_replaced_on_resubmit(
        self, client, db_session, dispatcher
    ):
        app_id, headers = _auth(client, db_session, dispatcher)
        client.post(
            f"{_BASE}/applications/{app_id}/finalize",
            json=_FINALIZE_FIELDS,
            headers=headers,
        )
        # Re-finalize with an empty list → all secondary lines removed.
        resp = client.post(
            f"{_BASE}/applications/{app_id}/finalize",
            json={"secondary_incomes": []},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        db_session.expire_all()
        application = (
            db_session.query(PlatformCreditApplication)
            .filter(PlatformCreditApplication.id == app_id)
            .first()
        )
        assert application.secondary_incomes == []

    def test_finalize_refused_on_terminal(self, client, db_session, dispatcher):
        app_id, headers = _auth(client, db_session, dispatcher)
        # Force a terminal status directly in the DB (bypassing the API) to simulate
        # an already-decided application.
        db_session.execute(
            text("UPDATE platform_credit_applications SET status = 'approved' WHERE id = :aid"),
            {"aid": app_id},
        )
        db_session.commit()
        resp = client.post(
            f"{_BASE}/applications/{app_id}/finalize",
            json={"first_name": "Nope"},
            headers=headers,
        )
        assert resp.status_code == 409, resp.text

    def test_finalize_requires_app_scope(self, client, db_session, dispatcher):
        app_id, headers = _auth(client, db_session, dispatcher)
        # A JWT for a DIFFERENT application must be rejected (403).
        other_id, other_headers = _auth(client, db_session, dispatcher)
        resp = client.post(
            f"{_BASE}/applications/{app_id}/finalize",
            json={"first_name": "X"},
            headers=other_headers,
        )
        assert resp.status_code == 403, resp.text
