"""Integration tests for the vendor webhook receiver (P6.6) via FastAPI TestClient."""
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
from app.services.flow_orchestrator import CONSENT_TO_VERIFICATION_TYPE, FlowOrchestrator
from app.services.verifications.mock_dispatcher import MockVerificationDispatcher

_BASE = "/api/webhooks/v1"


@pytest.fixture
def secrets(monkeypatch):
    vals = {"didit": "test_didit_secret", "flinks": "test_flinks_secret", "equifax": "test_equifax_secret"}
    monkeypatch.setattr(settings, "DIDIT_WEBHOOK_SECRET", vals["didit"])
    monkeypatch.setattr(settings, "FLINKS_WEBHOOK_SECRET", vals["flinks"])
    monkeypatch.setattr(settings, "EQUIFAX_WEBHOOK_SECRET", vals["equifax"])
    return vals


def _seed_product_id(db: Session) -> uuid.UUID:
    return (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
        .id
    )


def _setup_pending_verification(db: Session, purpose: str = "id_verification"):
    """Create patient + application + granted consent + one pending verification."""
    orch = FlowOrchestrator(db, consent_service, MockVerificationDispatcher())
    patient = PlatformPatient(email=f"wh-{uuid.uuid4().hex[:8]}@example.com")
    db.add(patient)
    db.commit()
    db.refresh(patient)
    app_row = orch.create_application(
        patient_id=patient.id,
        credit_product_id=_seed_product_id(db),
        requested_amount_cents=2_500_000,
        requested_amount_source="clinic",
    )
    orch.record_consent_grant(app_row.id, purpose, ip_address="203.0.113.1", user_agent="test")
    verif = orch.initiate_verification(app_row.id, purpose)
    return app_row.id, verif.id, verif.verification_type


def _body(app_id, verif_id, vtype, result="passed", rich=None):
    return {
        "verification_type": vtype,
        "result": result,
        "application_id": str(app_id),
        "verification_id": str(verif_id),
        "rich_payload": rich or {"confidence": 0.95},
    }


def _signed(client, vendor, body_dict, secret, *, delta=0, nonce=None, raw_override=None):
    raw_body = raw_override if raw_override is not None else json.dumps(body_dict).encode()
    timestamp = str(int(time.time()) + delta)
    nonce = nonce or str(uuid.uuid4())
    sig = hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return client.post(
        f"{_BASE}/{vendor}/verification",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Signature": sig,
                 "X-Timestamp": timestamp, "X-Nonce": nonce},
    )


def _count(db, event_type, nonce):
    return db.execute(
        text("SELECT count(*) FROM platform_events WHERE event_type=:t AND payload @> :k"),
        {"t": event_type, "k": json.dumps({"nonce": nonce})},
    ).scalar()


# --- happy paths (all three vendors) ---------------------------------------


class TestHappyPath:
    @pytest.mark.parametrize("vendor", ["didit", "flinks", "equifax"])
    def test_valid_webhook_returns_200(self, client: TestClient, db_session: Session, secrets, vendor):
        app_id, verif_id, vtype = _setup_pending_verification(db_session)
        nonce = str(uuid.uuid4())
        r = _signed(client, vendor, _body(app_id, verif_id, vtype), secrets[vendor], nonce=nonce)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "accepted"
        assert _count(db_session, "webhook_received", nonce) == 1


# --- rejections ------------------------------------------------------------


class TestRejections:
    def test_invalid_signature_returns_401(self, client, db_session, secrets):
        app_id, verif_id, vtype = _setup_pending_verification(db_session)
        # Sign with the wrong secret.
        r = _signed(client, "didit", _body(app_id, verif_id, vtype), "wrong_secret")
        assert r.status_code == 401
        # webhook_rejected emitted; verification NOT completed.
        rejected = db_session.execute(
            text("SELECT count(*) FROM platform_events WHERE event_type='webhook_rejected'")
        ).scalar()
        assert rejected >= 1

    def test_missing_signature_returns_401(self, client, db_session, secrets):
        app_id, verif_id, vtype = _setup_pending_verification(db_session)
        body = json.dumps(_body(app_id, verif_id, vtype)).encode()
        r = client.post(
            f"{_BASE}/didit/verification",
            content=body,
            headers={"Content-Type": "application/json", "X-Timestamp": str(int(time.time())),
                     "X-Nonce": str(uuid.uuid4())},
        )
        assert r.status_code == 401

    def test_expired_timestamp_returns_401(self, client, db_session, secrets):
        app_id, verif_id, vtype = _setup_pending_verification(db_session)
        r = _signed(client, "didit", _body(app_id, verif_id, vtype), secrets["didit"], delta=-400)
        assert r.status_code == 401

    def test_future_timestamp_returns_401(self, client, db_session, secrets):
        app_id, verif_id, vtype = _setup_pending_verification(db_session)
        r = _signed(client, "didit", _body(app_id, verif_id, vtype), secrets["didit"], delta=600)
        assert r.status_code == 401

    def test_rejected_event_has_vendor_and_reason(self, client, db_session, secrets):
        app_id, verif_id, vtype = _setup_pending_verification(db_session)
        _signed(client, "flinks", _body(app_id, verif_id, vtype), "wrong_secret")
        row = db_session.execute(
            text("SELECT payload FROM platform_events WHERE event_type='webhook_rejected' "
                 "ORDER BY occurred_at DESC LIMIT 1")
        ).first()
        assert row is not None
        assert row[0]["vendor"] == "flinks"
        assert "reason" in row[0]

    def test_application_status_unchanged_on_rejection(self, client, db_session, secrets):
        app_id, verif_id, vtype = _setup_pending_verification(db_session)  # status -> verifying
        _signed(client, "didit", _body(app_id, verif_id, vtype), "wrong_secret")  # 401
        app = db_session.get(PlatformCreditApplication, app_id)
        db_session.refresh(app)
        assert app.status == "verifying"  # not mutated by a rejected webhook


# --- replay / unknown / malformed ------------------------------------------


class TestReplayAndEdges:
    def test_idempotent_replay_returns_202(self, client, db_session, secrets):
        app_id, verif_id, vtype = _setup_pending_verification(db_session)
        nonce = str(uuid.uuid4())
        first = _signed(client, "didit", _body(app_id, verif_id, vtype), secrets["didit"], nonce=nonce)
        assert first.status_code == 200
        second = _signed(client, "didit", _body(app_id, verif_id, vtype), secrets["didit"], nonce=nonce)
        assert second.status_code == 202
        # webhook_received emitted exactly once.
        assert _count(db_session, "webhook_received", nonce) == 1

    def test_unknown_vendor_returns_404(self, client, db_session, secrets):
        app_id, verif_id, vtype = _setup_pending_verification(db_session)
        r = _signed(client, "unknown_corp", _body(app_id, verif_id, vtype), "whatever")
        assert r.status_code == 404

    def test_malformed_body_returns_400(self, client, db_session, secrets):
        # Valid signature over non-JSON bytes → passes verify, fails parse.
        r = _signed(client, "didit", {}, secrets["didit"], raw_override=b"not json at all")
        assert r.status_code == 400
