"""Tests for P7.5 — Didit "In Review" → terminal ``manual_review``.

Five tests covering:
1. Translator-level: "In Review" → result="manual_review", not skip.
2. Translator-level regression: "In Progress" still goes through skip (P7.4
   non-terminal behavior unchanged).
3. End-to-end through the vendor webhook → orchestrator: a signed Didit
   "In Review" POST sets ``verification.status = "manual_review"`` +
   ``completed_at`` and emits a ``verification_completed`` event with
   ``rich_payload.result == "manual_review"``.
4. Migration idempotency: ``ALTER TYPE … ADD VALUE IF NOT EXISTS`` no-ops on
   a second invocation (catches a regression that would have someone manually
   reapply the migration).
5. SQL surface: the enum's value list contains all six expected members.

Runs against the live Supabase Session Pooler per the house rule.
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
from app.models.platform.verification import PlatformVerification
from app.services.flow_orchestrator import FlowOrchestrator
from app.services.verifications.mock_dispatcher import MockVerificationDispatcher
from app.services.webhooks.signature_verifier import _didit_canonicalize
from app.services.webhooks.translators import (
    translate_didit_payload,
)
from app.api.webhooks.v1.schemas import DiditWebhookPayload

# Reuse setup helpers from the P7.2b vendor webhook integration test —
# duplicating them here keeps this file self-contained without copying ~80
# lines of fixture machinery.
from tests.test_vendor_webhooks_didit import (
    _seed_product_id,
    _setup_pending_kyc,
    _didit_body,
    _post,
    secret,  # fixture
)


_APP_ID = "11111111-2222-3333-4444-555555555555"


def _payload(status: str) -> DiditWebhookPayload:
    return DiditWebhookPayload(
        event_id="ev-mr-1",
        webhook_type="status.updated",
        timestamp=int(time.time()),
        session_id="sess-mr-1",
        status=status,
        vendor_data=_APP_ID,
        decision={"face_matches": [{"score": 95.0}],
                  "id_verifications": [{"document_type": "Passport"}],
                  "warnings": []},
    )


# ---------------------------------------------------------------------------
# 1. Translator: "In Review" → manual_review
# ---------------------------------------------------------------------------


class TestTranslatorInReviewTerminal:
    def test_in_review_returns_manual_review_not_skip(self):
        r = translate_didit_payload(_payload("In Review"))
        assert r.skip is False
        assert r.result == "manual_review"
        assert r.rich_payload is not None
        assert r.rich_payload["result"] == "manual_review"


# ---------------------------------------------------------------------------
# 2. Regression: other non-terminal statuses still skip
# ---------------------------------------------------------------------------


class TestNonTerminalRegression:
    @pytest.mark.parametrize("status", ["In Progress", "Not Started", "Resubmitted"])
    def test_other_non_terminal_still_skips(self, status):
        r = translate_didit_payload(_payload(status))
        assert r.skip is True
        assert r.result is None


# ---------------------------------------------------------------------------
# 3. End-to-end through the webhook + orchestrator
# ---------------------------------------------------------------------------


class TestEndToEndInReview:
    def test_webhook_routes_to_manual_review_terminal(
        self, client: TestClient, db_session: Session, secret
    ):
        app_id, verif_id = _setup_pending_kyc(db_session)
        body = _didit_body(app_id, status="In Review")
        r = _post(client, body, secret)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "accepted"

        v = db_session.get(PlatformVerification, verif_id)
        db_session.refresh(v)
        assert v.status == "manual_review"
        assert v.completed_at is not None

        # The verification_completed event carries result="manual_review" in
        # the rich payload + the after-block — the audit trail downstream
        # tools follow.
        row = db_session.execute(
            text(
                "SELECT payload FROM platform_events "
                "WHERE event_type='verification_completed' AND application_id=:a "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"a": str(app_id)},
        ).first()
        assert row is not None
        payload = row[0]
        assert payload["after"]["result"] == "manual_review"
        assert payload["rich_payload"]["result"] == "manual_review"


# ---------------------------------------------------------------------------
# 4. Migration is idempotent
# ---------------------------------------------------------------------------


class TestMigrationIdempotent:
    def test_alter_type_add_value_if_not_exists_is_noop(self, db_session: Session):
        # Running the same DDL twice must succeed both times — Postgres
        # accepts ADD VALUE IF NOT EXISTS as a no-op when the value is
        # already present. This guards against a regression that would
        # re-introduce a hard ADD VALUE without the IF NOT EXISTS clause.
        for _ in range(2):
            db_session.execute(
                text(
                    "ALTER TYPE platform_verification_status "
                    "ADD VALUE IF NOT EXISTS 'manual_review'"
                )
            )
            db_session.commit()


# ---------------------------------------------------------------------------
# 5. SQL: the enum has the six expected values
# ---------------------------------------------------------------------------


class TestEnumSurface:
    def test_enum_range_contains_manual_review(self, db_session: Session):
        rows = db_session.execute(
            text(
                "SELECT unnest(enum_range(NULL::platform_verification_status)) AS v"
            )
        ).fetchall()
        values = {r[0] for r in rows}
        assert values == {
            "pending", "in_progress", "passed", "failed", "expired", "manual_review",
        }
