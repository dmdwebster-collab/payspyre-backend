"""HTTP-layer integration tests for the patient API (P2.5).

Closes Section 7 audit Top fix #3: "E2E test for P2 patient endpoints — quick-start
is the front door of the MVP. Service-layer tests prove the function, not the wire."

Mirrors the conventions of `tests/test_credit_products_api.py` and
`tests/test_patient_profile_002_discrepancies.py`. Uses FastAPI TestClient against
the live Supabase test DB. Auth is monkeypatched to a synthetic user — real auth
flow is exercised in `tests/test_auth.py`.

Coverage:
- POST /api/v1/patients/quickstart    (201 success, 400 validation error)
- GET  /api/v1/patients/{id}          (200 with auth, 404 unknown)
- PATCH /api/v1/patients/{id}/fields  (success + discrepancy event)
- GET  /api/v1/patients/{id}/discrepancies (returns event-logged conflicts)
- GET  /api/v1/patients/{id}/fields/{key}/history (returns immutable history)

Also asserts Hard Rule #6 by inspecting the `platform_events` payload after
quickstart and confirming no PII (email, dob, name) leaks into the event row.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.db.base import get_db
from app.main import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _quickstart_payload(suffix: str | None = None) -> dict:
    s = suffix or uuid.uuid4().hex[:8]
    return {
        "legal_first_name": "Test",
        "legal_last_name": f"Patient_{s}",
        "email": f"e2e_{s}@example.com",
        "phone_e164": "+14165551234",
        "dob": "1990-04-12",
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db_session: Session):
    """TestClient with auth and db overridden."""
    fake_user = type("U", (), {"id": uuid.uuid4(), "email": "user@example.com", "role": "patient"})()

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: fake_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /quickstart — front door of the MVP
# ---------------------------------------------------------------------------


class TestQuickstartOverHTTP:
    def test_quickstart_returns_201_with_full_profile(self, client: TestClient):
        payload = _quickstart_payload()
        response = client.post("/api/v1/patients/quickstart", json=payload)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["legal_first_name"] == "Test"
        assert body["email"] == payload["email"]
        # All quickstart fields should be marked source='self_reported'
        assert any(
            f["source"] == "self_reported" and f["field_key"] == "email"
            for f in body["fields"]
        )

    def test_quickstart_missing_required_returns_422(self, client: TestClient):
        bad = _quickstart_payload()
        del bad["email"]
        response = client.post("/api/v1/patients/quickstart", json=bad)
        assert response.status_code == 422

    def test_quickstart_event_payload_contains_no_pii(
        self, client: TestClient, db_session: Session
    ):
        """Hard Rule #6: platform_events payload must never contain raw PII."""
        payload = _quickstart_payload(suffix="pii_check")
        response = client.post("/api/v1/patients/quickstart", json=payload)
        assert response.status_code == 201
        patient_id = response.json()["id"]

        # Inspect the quickstart event row directly
        row = db_session.execute(
            text(
                "SELECT payload::text AS payload FROM platform_events "
                "WHERE event_type = 'patient.quickstart_completed' "
                "AND patient_id = :pid"
            ),
            {"pid": patient_id},
        ).fetchone()
        assert row is not None, "quickstart event was not written"
        payload_text = row.payload.lower()
        assert payload["email"].lower() not in payload_text
        assert payload["legal_last_name"].lower() not in payload_text
        assert "1990-04-12" not in payload_text
        # Sanity: payload should record the source tag
        assert "self_reported" in payload_text


# ---------------------------------------------------------------------------
# GET /{id}
# ---------------------------------------------------------------------------


class TestGetProfileOverHTTP:
    def test_get_unknown_returns_404(self, client: TestClient):
        response = client.get(f"/api/v1/patients/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_get_existing_returns_profile(self, client: TestClient):
        created = client.post(
            "/api/v1/patients/quickstart", json=_quickstart_payload()
        ).json()
        response = client.get(f"/api/v1/patients/{created['id']}")
        assert response.status_code == 200
        assert response.json()["id"] == created["id"]


# ---------------------------------------------------------------------------
# PATCH /{id}/fields with conflicting source -> discrepancy
# ---------------------------------------------------------------------------


class TestFieldUpdateOverHTTP:
    def test_patch_field_writes_new_source_value(self, client: TestClient):
        created = client.post(
            "/api/v1/patients/quickstart", json=_quickstart_payload()
        ).json()
        response = client.patch(
            f"/api/v1/patients/{created['id']}/fields",
            json={
                "field_key": "legal_first_name",
                "value": "Different",
                "source": "id_doc",
                "confidence": 0.95,
            },
        )
        assert response.status_code == 200
        assert "updated" in response.json()["message"].lower()

    def test_patch_with_conflicting_source_logs_discrepancy_event(
        self, client: TestClient, db_session: Session
    ):
        """Conflicting source values should write a discrepancy event to platform_events."""
        created = client.post(
            "/api/v1/patients/quickstart", json=_quickstart_payload()
        ).json()
        # self_reported says "Test"; id_doc says "Mismatch"
        client.patch(
            f"/api/v1/patients/{created['id']}/fields",
            json={
                "field_key": "legal_first_name",
                "value": "Mismatch",
                "source": "id_doc",
            },
        )
        # Look for the discrepancy event in platform_events
        rows = db_session.execute(
            text(
                "SELECT event_type FROM platform_events "
                "WHERE patient_id = :pid "
                "ORDER BY created_at ASC"
            ),
            {"pid": created["id"]},
        ).fetchall()
        event_types = [r.event_type for r in rows]
        # At minimum the quickstart event should be present and a discrepancy
        # event should also be present (exact event_type depends on service impl;
        # accept either 'patient.field_discrepancy_detected' or any *discrepancy*).
        assert any("quickstart" in et for et in event_types)
        assert any("discrepancy" in et.lower() for et in event_types), event_types


# ---------------------------------------------------------------------------
# GET /{id}/discrepancies
# ---------------------------------------------------------------------------


class TestDiscrepanciesOverHTTP:
    def test_discrepancies_endpoint_returns_200(self, client: TestClient):
        created = client.post(
            "/api/v1/patients/quickstart", json=_quickstart_payload()
        ).json()
        client.patch(
            f"/api/v1/patients/{created['id']}/fields",
            json={
                "field_key": "legal_first_name",
                "value": "Mismatch",
                "source": "id_doc",
            },
        )
        response = client.get(f"/api/v1/patients/{created['id']}/discrepancies")
        assert response.status_code == 200
        body = response.json()
        assert body["patient_id"] == created["id"]
        assert isinstance(body["discrepancies"], list)
        assert isinstance(body["total_count"], int)

    def test_discrepancies_unknown_patient_returns_404(self, client: TestClient):
        response = client.get(f"/api/v1/patients/{uuid.uuid4()}/discrepancies")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /{id}/fields/{key}/history
# ---------------------------------------------------------------------------


class TestFieldHistoryOverHTTP:
    def test_history_returns_all_values_newest_first(self, client: TestClient):
        created = client.post(
            "/api/v1/patients/quickstart", json=_quickstart_payload()
        ).json()
        # Add a second value from a different source
        client.patch(
            f"/api/v1/patients/{created['id']}/fields",
            json={
                "field_key": "legal_first_name",
                "value": "FromIdDoc",
                "source": "id_doc",
            },
        )
        response = client.get(
            f"/api/v1/patients/{created['id']}/fields/legal_first_name/history"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["field_key"] == "legal_first_name"
        assert len(body["history"]) >= 2
        # Newest first per service docstring
        assert body["history"][0]["source"] == "id_doc"

    def test_history_unknown_patient_returns_404(self, client: TestClient):
        response = client.get(
            f"/api/v1/patients/{uuid.uuid4()}/fields/legal_first_name/history"
        )
        assert response.status_code == 404
