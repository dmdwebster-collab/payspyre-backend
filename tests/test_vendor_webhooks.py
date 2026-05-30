"""Integration tests for the vendor webhook receiver — MVP envelope (equifax).

P6.6 shipped one MVP envelope shared by all three vendors. P7.2b split Didit
and Flinks to real, vendor-shaped payloads (see ``test_vendor_webhooks_didit.py``
and ``test_vendor_webhooks_flinks.py``); this file now covers the **equifax**
route, which keeps the MVP envelope + the original ``X-Signature`` /
``X-Timestamp`` / ``X-Nonce`` HMAC scheme until a real Equifax adapter lands.

Why keep equifax on MVP: the bureau path always routes to ``MockBureauAdapter``
(P7.2 scope + the pending subscriber agreement). When real Equifax integration
happens, a new ``test_vendor_webhooks_equifax.py`` replaces this file and the
MVP envelope path can be retired.
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
from app.services.flow_orchestrator import FlowOrchestrator
from app.services.verifications.mock_dispatcher import MockVerificationDispatcher

_BASE = "/api/webhooks/v1"
_VENDOR = "equifax"


@pytest.fixture
def secret(monkeypatch):
    value = "test_equifax_secret"
    monkeypatch.setattr(settings, "EQUIFAX_WEBHOOK_SECRET", value)
    return value


def _seed_product_id(db: Session) -> uuid.UUID:
    return (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
        .id
    )


def _setup_pending_verification(db: Session, purpose: str = "soft_bureau_pull"):
    """Create patient + application + granted consent + one pending verification.

    Defaults to ``soft_bureau_pull`` (equifax-relevant) so the lookup +
    handoff exercises the bureau verification_type rather than KYC.
    """
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
        # The bureau replay adapter needs credit_score + result; mirror what a
        # real bureau callback would carry inside rich_payload.
        "rich_payload": rich or {"credit_score": 720, "result": result, "confidence": 0.95},
    }


def _signed(client, body_dict, secret, *, delta=0, nonce=None, raw_override=None):
    raw_body = raw_override if raw_override is not None else json.dumps(body_dict).encode()
    timestamp = str(int(time.time()) + delta)
    nonce = nonce or str(uuid.uuid4())
    sig = hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return client.post(
        f"{_BASE}/{_VENDOR}/verification",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-Signature": sig,
                 "X-Timestamp": timestamp, "X-Nonce": nonce},
    )


def _count(db, event_type, nonce):
    return db.execute(
        text("SELECT count(*) FROM platform_events WHERE event_type=:t AND payload @> :k"),
        {"t": event_type, "k": json.dumps({"vendor_event_id": nonce})},
    ).scalar()


# --- happy path ------------------------------------------------------------


class TestHappyPath:
    def test_valid_webhook_returns_200(self, client: TestClient, db_session: Session, secret):
        app_id, verif_id, vtype = _setup_pending_verification(db_session)
        nonce = str(uuid.uuid4())
        r = _signed(client, _body(app_id, verif_id, vtype), secret, nonce=nonce)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "accepted"
        assert _count(db_session, "webhook_received", nonce) == 1


# --- rejections ------------------------------------------------------------


class TestRejections:
    def test_invalid_signature_returns_401(self, client, db_session, secret):
        app_id, verif_id, vtype = _setup_pending_verification(db_session)
        r = _signed(client, _body(app_id, verif_id, vtype), "wrong_secret")
        assert r.status_code == 401
        rejected = db_session.execute(
            text("SELECT count(*) FROM platform_events WHERE event_type='webhook_rejected'")
        ).scalar()
        assert rejected >= 1

    def test_missing_signature_returns_401(self, client, db_session, secret):
        app_id, verif_id, vtype = _setup_pending_verification(db_session)
        body = json.dumps(_body(app_id, verif_id, vtype)).encode()
        r = client.post(
            f"{_BASE}/{_VENDOR}/verification",
            content=body,
            headers={"Content-Type": "application/json", "X-Timestamp": str(int(time.time())),
                     "X-Nonce": str(uuid.uuid4())},
        )
        assert r.status_code == 401

    def test_expired_timestamp_returns_401(self, client, db_session, secret):
        app_id, verif_id, vtype = _setup_pending_verification(db_session)
        r = _signed(client, _body(app_id, verif_id, vtype), secret, delta=-400)
        assert r.status_code == 401

    def test_future_timestamp_returns_401(self, client, db_session, secret):
        app_id, verif_id, vtype = _setup_pending_verification(db_session)
        r = _signed(client, _body(app_id, verif_id, vtype), secret, delta=600)
        assert r.status_code == 401

    def test_rejected_event_has_vendor_and_reason(self, client, db_session, secret):
        app_id, verif_id, vtype = _setup_pending_verification(db_session)
        _signed(client, _body(app_id, verif_id, vtype), "wrong_secret")
        row = db_session.execute(
            text("SELECT payload FROM platform_events WHERE event_type='webhook_rejected' "
                 "ORDER BY occurred_at DESC LIMIT 1")
        ).first()
        assert row is not None
        assert row[0]["vendor"] == _VENDOR
        assert "reason" in row[0]

    def test_application_status_unchanged_on_rejection(self, client, db_session, secret):
        app_id, verif_id, vtype = _setup_pending_verification(db_session)  # → verifying
        _signed(client, _body(app_id, verif_id, vtype), "wrong_secret")    # 401
        app = db_session.get(PlatformCreditApplication, app_id)
        db_session.refresh(app)
        assert app.status == "verifying"


# --- replay / unknown / malformed ------------------------------------------


class TestReplayAndEdges:
    def test_idempotent_replay_returns_202(self, client, db_session, secret):
        app_id, verif_id, vtype = _setup_pending_verification(db_session)
        nonce = str(uuid.uuid4())
        first = _signed(client, _body(app_id, verif_id, vtype), secret, nonce=nonce)
        assert first.status_code == 200
        second = _signed(client, _body(app_id, verif_id, vtype), secret, nonce=nonce)
        assert second.status_code == 202
        # webhook_received emitted exactly once.
        assert _count(db_session, "webhook_received", nonce) == 1

    def test_unknown_vendor_returns_404(self, client, db_session, secret):
        app_id, verif_id, vtype = _setup_pending_verification(db_session)
        # Sign with the equifax secret but POST to /unknown_corp/.
        raw = json.dumps(_body(app_id, verif_id, vtype)).encode()
        ts = str(int(time.time()))
        sig = hmac.new(secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
        r = client.post(
            f"{_BASE}/unknown_corp/verification",
            content=raw,
            headers={"Content-Type": "application/json", "X-Signature": sig,
                     "X-Timestamp": ts, "X-Nonce": str(uuid.uuid4())},
        )
        assert r.status_code == 404

    def test_malformed_body_returns_400(self, client, db_session, secret):
        # Valid signature over non-JSON bytes → passes verify, fails parse.
        r = _signed(client, {}, secret, raw_override=b"not json at all")
        assert r.status_code == 400
