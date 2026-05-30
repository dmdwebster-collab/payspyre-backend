"""Integration tests for the real Didit webhook path (P7.2b).

POSTs Didit-shaped JSON with ``X-Signature-V2`` + ``X-Timestamp`` against
``/api/webhooks/v1/didit/verification`` and verifies the full pipeline:
signature → schema parse → translation → nonce check → verification lookup →
``handle_verification_result`` → ``platform_events`` recording.

The mock dispatcher creates the pending verification at initiate; the webhook
test then plays a real-shape Didit body referencing the application_id we
seeded.
"""
import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

import app.services.consent_service as consent_service
from app.core.config import settings
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.patient import PlatformPatient
from app.models.platform.verification import PlatformVerification
from app.services.flow_orchestrator import FlowOrchestrator
from app.services.verifications.mock_dispatcher import MockVerificationDispatcher
from app.services.webhooks.signature_verifier import _didit_canonicalize

_BASE = "/api/webhooks/v1/didit/verification"


@pytest.fixture
def secret(monkeypatch) -> str:
    value = "didit_integration_secret"
    monkeypatch.setattr(settings, "DIDIT_WEBHOOK_SECRET", value)
    return value


def _seed_product_id(db: Session) -> uuid.UUID:
    return (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
        .id
    )


def _setup_pending_kyc(db: Session):
    orch = FlowOrchestrator(db, consent_service, MockVerificationDispatcher())
    patient = PlatformPatient(email=f"didit-{uuid.uuid4().hex[:8]}@example.com")
    db.add(patient)
    db.commit()
    db.refresh(patient)
    app_row = orch.create_application(
        patient_id=patient.id,
        credit_product_id=_seed_product_id(db),
        requested_amount_cents=2_500_000,
        requested_amount_source="clinic",
    )
    orch.record_consent_grant(app_row.id, "id_verification", ip_address="203.0.113.1", user_agent="test")
    verif = orch.initiate_verification(app_row.id, "id_verification")
    return app_row.id, verif.id


def _didit_body(app_id, *, status="Approved", event_id=None, session_id=None) -> dict:
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "webhook_type": "status.updated",
        "timestamp": int(time.time()),
        "created_at": int(time.time()),
        "session_id": session_id or str(uuid.uuid4()),
        "status": status,
        "vendor_data": str(app_id),
        "workflow_id": str(uuid.uuid4()),
        "decision": {
            "face_matches": [{"score": 96.1}],
            "id_verifications": [{"document_type": "Passport"}],
            "aml_screenings": [],
            "warnings": [],
        },
    }


def _post(client, body: dict, secret: str, *, ts_delta: int = 0):
    raw = json.dumps(body).encode()
    canonical = _didit_canonicalize(body)
    sig = hmac.new(secret.encode(), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    ts = str(int(time.time()) + ts_delta)
    return client.post(
        _BASE,
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Signature-V2": sig,
            "X-Timestamp": ts,
        },
    )


class TestDiditHappyPath:
    def test_approved_records_completion(self, client: TestClient, db_session: Session, secret):
        app_id, verif_id = _setup_pending_kyc(db_session)
        body = _didit_body(app_id, status="Approved")
        r = _post(client, body, secret)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "accepted"

        # webhook_received recorded with Didit's event_id as the vendor_event_id.
        received = db_session.execute(
            text(
                "SELECT count(*) FROM platform_events WHERE event_type='webhook_received' "
                "AND payload @> :k"
            ),
            {"k": json.dumps({"vendor_event_id": body["event_id"]})},
        ).scalar()
        assert received == 1

        # verification_completed recorded with rich_payload from the translator.
        row = db_session.execute(
            text(
                "SELECT payload FROM platform_events WHERE event_type='verification_completed' "
                "AND application_id=:a LIMIT 1"
            ),
            {"a": str(app_id)},
        ).first()
        assert row is not None
        rich = row[0].get("rich_payload", {})
        assert rich["vendor"] == "didit"
        assert rich["method"] == "document"
        assert rich["result"] == "passed"
        # face_match.score 96.1 → confidence 0.961
        assert rich["confidence"] == pytest.approx(0.961)

    def test_declined_marks_verification_failed(self, client, db_session, secret):
        app_id, verif_id = _setup_pending_kyc(db_session)
        body = _didit_body(app_id, status="Declined")
        r = _post(client, body, secret)
        assert r.status_code == 200

        v = db_session.get(PlatformVerification, verif_id)
        db_session.refresh(v)
        assert v.status == "failed"


class TestDiditNonTerminal:
    @pytest.mark.parametrize("status", ["In Progress", "In Review", "Resubmitted"])
    def test_non_terminal_returns_202_and_leaves_pending(self, client, db_session, secret, status):
        app_id, verif_id = _setup_pending_kyc(db_session)
        body = _didit_body(app_id, status=status)
        r = _post(client, body, secret)
        assert r.status_code == 202
        assert r.json()["status"] == "ignored"

        v = db_session.get(PlatformVerification, verif_id)
        db_session.refresh(v)
        assert v.status == "pending"  # untouched


class TestDiditSignatureFailures:
    def test_wrong_secret_returns_401(self, client, db_session, secret):
        app_id, _ = _setup_pending_kyc(db_session)
        r = _post(client, _didit_body(app_id), "WRONG_SECRET")
        assert r.status_code == 401

    def test_missing_v2_header_returns_401(self, client, db_session, secret):
        app_id, _ = _setup_pending_kyc(db_session)
        body = _didit_body(app_id)
        raw = json.dumps(body).encode()
        r = client.post(
            _BASE,
            content=raw,
            headers={"Content-Type": "application/json", "X-Timestamp": str(int(time.time()))},
        )
        assert r.status_code == 401

    def test_expired_timestamp_returns_401(self, client, db_session, secret):
        app_id, _ = _setup_pending_kyc(db_session)
        r = _post(client, _didit_body(app_id), secret, ts_delta=-400)
        assert r.status_code == 401


class TestDiditReplayAndEdges:
    def test_idempotent_replay_returns_202_already_processed(self, client, db_session, secret):
        app_id, _ = _setup_pending_kyc(db_session)
        body = _didit_body(app_id)  # same event_id both times
        first = _post(client, body, secret)
        assert first.status_code == 200
        second = _post(client, body, secret)
        assert second.status_code == 202
        assert second.json()["status"] == "already_processed"

    def test_missing_vendor_data_returns_400(self, client, db_session, secret):
        body = _didit_body(uuid.uuid4())
        body.pop("vendor_data")
        r = _post(client, body, secret)
        assert r.status_code == 400

    def test_non_uuid_vendor_data_returns_400(self, client, db_session, secret):
        body = _didit_body(uuid.uuid4())
        body["vendor_data"] = "not-a-uuid"
        r = _post(client, body, secret)
        assert r.status_code == 400

    def test_unknown_application_returns_404(self, client, db_session, secret):
        # vendor_data is a valid UUID but no application exists.
        body = _didit_body(uuid.uuid4(), status="Approved")
        r = _post(client, body, secret)
        # No matching PlatformVerification → 404.
        assert r.status_code == 404
