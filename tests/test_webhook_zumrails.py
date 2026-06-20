"""Unit tests for POST /api/webhooks/v1/zumrails/transaction (P8.x).

Fully mocked — NO live database. ``get_db`` is overridden with a ``MagicMock``
Session, the Zumrails adapter (``_build_adapter``) + ``loan_lifecycle`` are
patched, and replay behavior is driven by a stub ``SignatureVerifier``.

Covered:
* happy path: ``Completed`` txn → ``on_disbursement_complete`` called, 200;
* signature failure → 401;
* unknown disbursement_ref → 202 orphaned (no lifecycle call);
* idempotent replay → 202 replay (no lifecycle call);
* ``Failed`` → ``on_disbursement_failed``; non-terminal → 202 ignored.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app.api.webhooks.v1.endpoints.payments as payments
from app.db.base import get_db
from app.main import app
from app.api.webhooks.v1.deps import get_signature_verifier
from app.services.webhooks.signature_verifier import NonceReplayed

_PATH = "/api/webhooks/v1/zumrails/transaction"
_TXN_ID = "zum-txn-789"


def _body(status: str = "Completed", txn_id: str = _TXN_ID) -> bytes:
    return json.dumps(
        {"result": {"Id": txn_id, "TransactionStatus": status}}
    ).encode("utf-8")


def _make_db(loan):
    db = MagicMock(name="db_session")
    db.query.return_value.filter.return_value.first.return_value = loan
    return db


def _make_verifier(*, replay: bool = False):
    verifier = MagicMock(name="verifier")
    if replay:
        verifier.check_nonce.side_effect = NonceReplayed("already processed")
    else:
        verifier.check_nonce.return_value = None
    return verifier


@contextmanager
def _wired(*, loan, verifier, verify_ok: bool = True):
    """Override deps + patch the adapter + loan_lifecycle for one request."""
    db = _make_db(loan)
    adapter = MagicMock(name="zumrails_adapter")
    adapter.verify_webhook.return_value = verify_ok

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_signature_verifier] = lambda: verifier
    with patch.object(payments, "_build_adapter", return_value=adapter), patch.object(
        payments, "loan_lifecycle"
    ) as lifecycle, patch(
        "app.services.observability.posthog_bridge.capture_event", MagicMock()
    ):
        try:
            yield db, adapter, lifecycle
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(get_signature_verifier, None)


@pytest.fixture
def tc():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_completed_calls_on_disbursement_complete(tc):
    loan = MagicMock(name="loan")
    verifier = _make_verifier()
    with _wired(loan=loan, verifier=verifier) as (db, adapter, lifecycle):
        r = tc.post(_PATH, content=_body("Completed"))

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"
    lifecycle.on_disbursement_complete.assert_called_once_with(db, loan, ref=_TXN_ID)
    lifecycle.on_disbursement_failed.assert_not_called()
    verifier.check_nonce.assert_called_once_with(f"zumrails:{_TXN_ID}:completed")


def test_failed_calls_on_disbursement_failed(tc):
    loan = MagicMock(name="loan")
    verifier = _make_verifier()
    with _wired(loan=loan, verifier=verifier) as (db, adapter, lifecycle):
        r = tc.post(_PATH, content=_body("Failed"))

    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    lifecycle.on_disbursement_failed.assert_called_once_with(db, loan, ref=_TXN_ID)
    lifecycle.on_disbursement_complete.assert_not_called()


def test_non_terminal_status_is_ignored(tc):
    loan = MagicMock(name="loan")
    verifier = _make_verifier()
    with _wired(loan=loan, verifier=verifier) as (db, adapter, lifecycle):
        r = tc.post(_PATH, content=_body("InProgress"))

    assert r.status_code == 202
    assert r.json()["status"] == "ignored"
    lifecycle.on_disbursement_complete.assert_not_called()
    lifecycle.on_disbursement_failed.assert_not_called()


# ---------------------------------------------------------------------------
# Signature failure → 401
# ---------------------------------------------------------------------------


def test_bad_signature_returns_401(tc):
    verifier = _make_verifier()
    with _wired(loan=MagicMock(), verifier=verifier, verify_ok=False) as (
        db, adapter, lifecycle,
    ):
        r = tc.post(_PATH, content=_body("Completed"))

    assert r.status_code == 401
    lifecycle.on_disbursement_complete.assert_not_called()
    verifier.check_nonce.assert_not_called()


def test_provider_disabled_returns_401(tc):
    verifier = _make_verifier()
    db = _make_db(MagicMock())
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_signature_verifier] = lambda: verifier
    with patch.object(payments, "_build_adapter", return_value=None), patch(
        "app.services.observability.posthog_bridge.capture_event", MagicMock()
    ):
        try:
            r = tc.post(_PATH, content=_body("Completed"))
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(get_signature_verifier, None)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Unknown ref → 202 orphaned
# ---------------------------------------------------------------------------


def test_unknown_disbursement_ref_returns_202_orphaned(tc):
    verifier = _make_verifier()
    with _wired(loan=None, verifier=verifier) as (db, adapter, lifecycle):
        r = tc.post(_PATH, content=_body("Completed"))

    assert r.status_code == 202
    assert r.json()["status"] == "orphaned"
    lifecycle.on_disbursement_complete.assert_not_called()
    db.commit.assert_called()


# ---------------------------------------------------------------------------
# Idempotent replay → 202 replay
# ---------------------------------------------------------------------------


def test_replay_returns_202_replay_no_lifecycle(tc):
    loan = MagicMock(name="loan")
    verifier = _make_verifier(replay=True)
    with _wired(loan=loan, verifier=verifier) as (db, adapter, lifecycle):
        r = tc.post(_PATH, content=_body("Completed"))

    assert r.status_code == 202
    assert r.json()["status"] == "replay"
    lifecycle.on_disbursement_complete.assert_not_called()
    lifecycle.on_disbursement_failed.assert_not_called()


def test_missing_transaction_id_returns_400(tc):
    verifier = _make_verifier()
    with _wired(loan=MagicMock(), verifier=verifier) as (db, adapter, lifecycle):
        r = tc.post(_PATH, content=json.dumps({"result": {"TransactionStatus": "Completed"}}).encode())

    assert r.status_code == 400
    lifecycle.on_disbursement_complete.assert_not_called()
