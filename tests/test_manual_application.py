"""Manual credit-application path (applicant API).

Covers ``POST /api/applicant/v1/applications/{id}/manual`` — the integration-
failure fallback where the borrower hand-enters the fields normally pulled from
Didit/Flinks and the application is routed to manual review.

Mirrors tests/test_applicant_journey.py for the patient + application + JWT
fixture. Live test DB (per-function TRUNCATE in conftest). Run ONLY this file.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import get_notification_dispatcher
from app.api.applicant.v1.endpoints import manual_application as manual_application_module
from app.main import app
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.services.mock_notification_dispatcher import MockNotificationDispatcher

_BASE = "/api/applicant/v1"

# The router assembly (app/api/applicant/v1/router.py) is owned by the
# orchestrator and registers this router in the real app. For this isolated test
# we ensure the route is mounted without editing that module — include it on the
# live app once if it isn't already present.
_MANUAL_PATH = f"{_BASE}/applications/{{application_id}}/manual"
if not any(getattr(r, "path", None) == _MANUAL_PATH for r in app.router.routes):
    app.include_router(manual_application_module.router, prefix=_BASE)

_MANUAL_FIELDS = {
    "legal_name": "Jordan Q. Public",
    "date_of_birth": "1990-04-15",
    "address": "123 Test St, Toronto, ON",
    "employer_name": "Acme Dental",
    "monthly_income_cents": 600000,
    "monthly_shelter_cents": 180000,
    "monthly_non_discretionary_expenses_cents": 90000,
}


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
    """Create an application (new patient) and exchange the magic link for a JWT."""
    resp = client.post(
        f"{_BASE}/applications",
        json={
            "patient_profile": {
                "legal_first_name": "Jo",
                "email": f"manual-{uuid.uuid4().hex[:8]}@example.com",
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


class TestManualApplication:
    def test_manual_submission_persists_marks_under_review_and_writes_event(
        self, client: TestClient, db_session: Session, dispatcher
    ):
        app_id, headers = _auth(client, db_session, dispatcher)

        resp = client.post(
            f"{_BASE}/applications/{app_id}/manual",
            json=_MANUAL_FIELDS,
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "under_review"
        assert body["manual_application"] is True
        assert body["application_id"] == app_id

        # Reload the row from the DB (the endpoint committed).
        db_session.expire_all()
        application = (
            db_session.query(PlatformCreditApplication)
            .filter(PlatformCreditApplication.id == app_id)
            .first()
        )

        # status + manual-review marker
        assert application.status == "under_review"
        assert application.flow_state.get("manual_application") is True

        # manual fields persisted into self_reported
        manual = application.self_reported.get("manual")
        assert manual is not None
        assert manual["legal_name"] == _MANUAL_FIELDS["legal_name"]
        assert manual["date_of_birth"] == _MANUAL_FIELDS["date_of_birth"]
        assert manual["address"] == _MANUAL_FIELDS["address"]
        assert manual["employer_name"] == _MANUAL_FIELDS["employer_name"]
        assert manual["monthly_income_cents"] == _MANUAL_FIELDS["monthly_income_cents"]
        assert manual["monthly_shelter_cents"] == _MANUAL_FIELDS["monthly_shelter_cents"]
        assert (
            manual["monthly_non_discretionary_expenses_cents"]
            == _MANUAL_FIELDS["monthly_non_discretionary_expenses_cents"]
        )

        # NOT auto-decided: a manual application awaits a human decision.
        assert application.decision is None

        # event written
        count = db_session.execute(
            text(
                "SELECT count(*) FROM platform_events "
                "WHERE event_type = 'manual_application_submitted' "
                "AND application_id = :aid"
            ),
            {"aid": app_id},
        ).scalar()
        assert count == 1

    def test_not_owned_application_is_forbidden_or_not_found(
        self, client: TestClient, db_session: Session, dispatcher
    ):
        # JWT scoped to app A; submit against a different, unowned application id.
        _app_a, headers = _auth(client, db_session, dispatcher)
        other_id = str(uuid.uuid4())

        resp = client.post(
            f"{_BASE}/applications/{other_id}/manual",
            json=_MANUAL_FIELDS,
            headers=headers,
        )
        assert resp.status_code in (403, 404), resp.text

    def test_missing_required_field_is_unprocessable(
        self, client: TestClient, db_session: Session, dispatcher
    ):
        app_id, headers = _auth(client, db_session, dispatcher)

        incomplete = dict(_MANUAL_FIELDS)
        del incomplete["employer_name"]

        resp = client.post(
            f"{_BASE}/applications/{app_id}/manual",
            json=incomplete,
            headers=headers,
        )
        assert resp.status_code == 422, resp.text

    def test_resubmission_is_idempotent(
        self, client: TestClient, db_session: Session, dispatcher
    ):
        app_id, headers = _auth(client, db_session, dispatcher)

        first = client.post(
            f"{_BASE}/applications/{app_id}/manual",
            json=_MANUAL_FIELDS,
            headers=headers,
        )
        assert first.status_code == 200, first.text

        updated = dict(_MANUAL_FIELDS, legal_name="Jordan Renamed")
        second = client.post(
            f"{_BASE}/applications/{app_id}/manual",
            json=updated,
            headers=headers,
        )
        assert second.status_code == 200, second.text
        assert second.json()["status"] == "under_review"

        db_session.expire_all()
        application = (
            db_session.query(PlatformCreditApplication)
            .filter(PlatformCreditApplication.id == app_id)
            .first()
        )
        assert application.self_reported["manual"]["legal_name"] == "Jordan Renamed"
