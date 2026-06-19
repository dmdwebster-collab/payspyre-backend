"""Integration tests for the real Flinks webhook path (P7.2b).

POSTs Flinks-shaped JSON with ``flinks-authenticity-key`` against
``/api/webhooks/v1/flinks/verification`` and verifies:
- signature verification (base64 HMAC-SHA256 over raw body)
- ``Tag`` → application_id correlation
- in-translator arithmetic over ``Accounts[].Transactions[]``
- vendor_session_ref upgrade from the Connect-URL placeholder to ``Login.Id``
- composite-nonce replay protection
- KYC ResponseType is acknowledged but skipped (bank_link stays pending)
"""
import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

import app.services.consent_service as consent_service
from app.core.config import settings
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.patient import PlatformPatient
from app.models.platform.verification import PlatformVerification
from app.services.flow_orchestrator import FlowOrchestrator
from app.services.verifications.mock_dispatcher import MockVerificationDispatcher

_BASE = "/api/webhooks/v1/flinks/verification"


@pytest.fixture
def secret(monkeypatch) -> str:
    value = "flinks_integration_secret"
    monkeypatch.setattr(settings, "FLINKS_WEBHOOK_SECRET", value)
    return value


def _seed_product_id(db: Session) -> uuid.UUID:
    return (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
        .id
    )


def _setup_pending_bank(db: Session):
    """Create patient + application + consent + a pending bank_link verification.

    The mock dispatcher writes a ``mock_`` vendor_session_ref; the test then
    POSTs the Flinks webhook which should *upgrade* it to the real Login.Id.
    """
    orch = FlowOrchestrator(db, consent_service, MockVerificationDispatcher())
    patient = PlatformPatient(email=f"flinks-{uuid.uuid4().hex[:8]}@example.com")
    db.add(patient)
    db.commit()
    db.refresh(patient)
    app_row = orch.create_application(
        patient_id=patient.id,
        credit_product_id=_seed_product_id(db),
        requested_amount_cents=2_500_000,
        requested_amount_source="clinic",
    )
    orch.record_consent_grant(app_row.id, "bank_verification", ip_address="203.0.113.1", user_agent="test")
    verif = orch.initiate_verification(app_row.id, "bank_verification")
    return app_row.id, verif.id


def _today_str():
    return datetime.now(timezone.utc).date().strftime("%Y-%m-%d")


def _flinks_body(
    app_id, *, login_id=None, response_type="GetAccountsDetail", http_status=200,
    accounts=None,
):
    if accounts is None:
        accounts = [
            {
                "Id": "acc-1",
                "Title": "Checking",
                "AccountNumber": "001",
                "Balance": {"Available": 4000.0, "Current": 5234.56, "Limit": 0},
                "Transactions": [
                    {"Id": "t1", "Date": _today_str(), "Description": "PAY DEPOSIT",
                     "Credit": 2500.00, "Debit": 0, "Balance": 5234.56},
                ],
                "Currency": "CAD",
            }
        ]
    return {
        "ResponseType": response_type,
        "HttpStatusCode": http_status,
        "Login": {"Id": login_id or str(uuid.uuid4())},
        "Tag": str(app_id),
        "RequestId": str(uuid.uuid4()),
        "Accounts": accounts,
        "Institution": "Royal Bank",
    }


def _post(client, body: dict, secret: str, *, tamper_after_sign: bool = False):
    raw = json.dumps(body).encode()
    sig = base64.b64encode(
        hmac.new(secret.encode(), raw, hashlib.sha256).digest()
    ).decode("ascii")
    if tamper_after_sign:
        raw = raw.replace(b'"PAY DEPOSIT"', b'"PAY EVIL"')
    return client.post(
        _BASE,
        content=raw,
        headers={"Content-Type": "application/json", "flinks-authenticity-key": sig},
    )


class TestFlinksHappyPath:
    def test_accounts_detail_records_completion(
        self, client: TestClient, db_session: Session, secret
    ):
        app_id, verif_id = _setup_pending_bank(db_session)
        login_id = str(uuid.uuid4())
        body = _flinks_body(app_id, login_id=login_id)
        r = _post(client, body, secret)
        assert r.status_code == 200, r.text

        # webhook_received with composite nonce.
        nonce = f"flinks:{login_id}:GetAccountsDetail"
        received = db_session.execute(
            text(
                "SELECT count(*) FROM platform_events WHERE event_type='webhook_received' "
                "AND payload @> :k"
            ),
            {"k": json.dumps({"vendor_event_id": nonce})},
        ).scalar()
        assert received == 1

    def test_vendor_session_ref_upgraded_from_placeholder_to_login_id(
        self, client, db_session, secret
    ):
        app_id, verif_id = _setup_pending_bank(db_session)
        v = db_session.get(PlatformVerification, verif_id)
        original_ref = v.vendor_session_ref  # mock_xxx
        assert original_ref is not None and original_ref.startswith("mock_")

        login_id = str(uuid.uuid4())
        _post(client, _flinks_body(app_id, login_id=login_id), secret)

        db_session.refresh(v)
        assert v.vendor_session_ref == login_id

    def test_rich_payload_contains_recurrence_derivations(
        self, client, db_session, secret
    ):
        """Bank metrics flow through the webhook → transaction-analysis engine.

        Income is now derived from RECURRING deposit streams (P8.x engine), not a
        naive sum of credits — so a single one-off deposit is correctly NOT income,
        while a recurring payroll stream is detected. (Engine internals are unit-tested
        exhaustively in tests/test_transaction_analysis.py.)
        """
        def _rich(aid):
            row = db_session.execute(
                text(
                    "SELECT payload FROM platform_events WHERE "
                    "event_type='verification_completed' AND application_id=:a LIMIT 1"
                ),
                {"a": str(aid)},
            ).first()
            assert row is not None
            return row[0].get("rich_payload", {})

        # Single non-recurring deposit → not counted as income; balance still flows through.
        app_id, _ = _setup_pending_bank(db_session)
        _post(client, _flinks_body(app_id), secret)
        rich = _rich(app_id)
        assert rich["vendor"] == "flinks"
        assert rich["monthly_income_cents"] == 0  # one-off deposit is not recurring income
        assert rich["avg_balance_cents"] == 523456  # Balance.Current 5234.56

        # Recurring bi-weekly payroll → detected as income through the webhook → engine.
        app_id2, _ = _setup_pending_bank(db_session)
        today = datetime.now(timezone.utc).date()
        payroll = [
            {
                "Id": "acc-r",
                "Title": "Checking",
                "AccountNumber": "002",
                "Balance": {"Available": 3000.0, "Current": 3000.0, "Limit": 0},
                "Transactions": [
                    {
                        "Id": f"p{i}",
                        "Date": (today - timedelta(days=14 * i)).strftime("%Y-%m-%d"),
                        "Description": "PAYROLL DEPOSIT",
                        "Credit": 2000.00,
                        "Debit": 0,
                        "Balance": 3000.0,
                    }
                    for i in range(6)
                ],
                "Currency": "CAD",
            }
        ]
        _post(client, _flinks_body(app_id2, accounts=payroll), secret)
        assert _rich(app_id2)["monthly_income_cents"] > 0  # recurring payroll detected


class TestFlinksKYCSkip:
    def test_kyc_response_returns_202_and_leaves_pending(self, client, db_session, secret):
        app_id, verif_id = _setup_pending_bank(db_session)
        body = _flinks_body(app_id, response_type="KYC")
        r = _post(client, body, secret)
        assert r.status_code == 202
        assert r.json()["status"] == "ignored"

        v = db_session.get(PlatformVerification, verif_id)
        db_session.refresh(v)
        assert v.status == "pending"


class TestFlinksSignatureFailures:
    def test_wrong_secret_returns_401(self, client, db_session, secret):
        app_id, _ = _setup_pending_bank(db_session)
        body = _flinks_body(app_id)
        raw = json.dumps(body).encode()
        sig = base64.b64encode(
            hmac.new(b"WRONG", raw, hashlib.sha256).digest()
        ).decode("ascii")
        r = client.post(
            _BASE,
            content=raw,
            headers={"Content-Type": "application/json", "flinks-authenticity-key": sig},
        )
        assert r.status_code == 401

    def test_body_tampering_returns_401(self, client, db_session, secret):
        app_id, _ = _setup_pending_bank(db_session)
        r = _post(client, _flinks_body(app_id), secret, tamper_after_sign=True)
        assert r.status_code == 401

    def test_missing_authenticity_key_returns_401(self, client, db_session, secret):
        app_id, _ = _setup_pending_bank(db_session)
        raw = json.dumps(_flinks_body(app_id)).encode()
        r = client.post(
            _BASE, content=raw,
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 401


class TestFlinksReplayAndEdges:
    def test_idempotent_replay_returns_202(self, client, db_session, secret):
        app_id, _ = _setup_pending_bank(db_session)
        login_id = str(uuid.uuid4())
        body = _flinks_body(app_id, login_id=login_id)
        first = _post(client, body, secret)
        assert first.status_code == 200
        second = _post(client, body, secret)
        assert second.status_code == 202
        assert second.json()["status"] == "already_processed"

    def test_missing_tag_returns_400(self, client, db_session, secret):
        body = _flinks_body(uuid.uuid4())
        body.pop("Tag")
        r = _post(client, body, secret)
        assert r.status_code == 400

    def test_non_uuid_tag_returns_400(self, client, db_session, secret):
        body = _flinks_body(uuid.uuid4())
        body["Tag"] = "not-a-uuid"
        r = _post(client, body, secret)
        assert r.status_code == 400

    def test_unknown_application_returns_404(self, client, db_session, secret):
        body = _flinks_body(uuid.uuid4())  # valid UUID Tag but no application
        r = _post(client, body, secret)
        assert r.status_code == 404
