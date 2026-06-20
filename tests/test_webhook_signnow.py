"""Unit tests for POST /api/webhooks/v1/signnow/agreement (P8.x).

Fully mocked — NO live database. The ``get_db`` dependency is overridden with a
``MagicMock`` Session, the SignNow adapter (``_build_adapter``) and
``loan_lifecycle`` are patched, and the nonce / replay behavior is driven by a
stub ``SignatureVerifier`` injected via ``get_signature_verifier``.

Covered:
* happy path: ``signed`` event → ``on_agreement_signed`` called, 200 accepted;
* signature failure → 401;
* unknown agreement_ref → 202 orphaned (no lifecycle call);
* idempotent replay → 202 replay (no lifecycle call).
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app.api.webhooks.v1.endpoints.esign as esign
from app.db.base import get_db
from app.main import app
from app.api.webhooks.v1.deps import get_signature_verifier
from app.services.esign.signnow_adapter import (
    SignNowWebhookError,
    SignNowWebhookEvent,
)
from app.services.webhooks.signature_verifier import NonceReplayed

_PATH = "/api/webhooks/v1/signnow/agreement"
_DOC_ID = "signnow-doc-123"


def _make_db(loan):
    """A MagicMock Session whose ``query(...).filter(...).first()`` yields ``loan``."""
    db = MagicMock(name="db_session")
    db.query.return_value.filter.return_value.first.return_value = loan
    return db


def _make_verifier(*, replay: bool = False):
    """Stub SignatureVerifier — ``check_nonce`` raises NonceReplayed iff ``replay``."""
    verifier = MagicMock(name="verifier")
    if replay:
        verifier.check_nonce.side_effect = NonceReplayed("already processed")
    else:
        verifier.check_nonce.return_value = None
    return verifier


@contextmanager
def _wired(*, loan, verifier, webhook_event=None, verify_raises=None):
    """Override deps + patch the adapter + loan_lifecycle for one request.

    ``webhook_event`` is what ``adapter.verify_webhook`` returns; ``verify_raises``
    (a SignNowWebhookError) is raised instead when set.
    """
    db = _make_db(loan)

    adapter = MagicMock(name="signnow_adapter")
    if verify_raises is not None:
        adapter.verify_webhook.side_effect = verify_raises
    else:
        adapter.verify_webhook.return_value = webhook_event

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_signature_verifier] = lambda: verifier
    with patch.object(esign, "_build_adapter", return_value=adapter), patch.object(
        esign, "loan_lifecycle"
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


def _signed_event(status: str = "signed") -> SignNowWebhookEvent:
    return SignNowWebhookEvent(
        event_type="document.complete",
        document_id=_DOC_ID,
        status=status,
        raw={"document_id": _DOC_ID, "status": status},
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_signed_event_calls_on_agreement_signed(tc):
    loan = MagicMock(name="loan")
    verifier = _make_verifier()
    with _wired(loan=loan, verifier=verifier, webhook_event=_signed_event()) as (
        db, adapter, lifecycle,
    ):
        r = tc.post(_PATH, content=b'{"document_id": "signnow-doc-123"}')

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"
    lifecycle.on_agreement_signed.assert_called_once_with(db, loan)
    lifecycle.on_agreement_declined.assert_not_called()
    # Nonce includes the document id + status.
    verifier.check_nonce.assert_called_once_with(f"signnow:{_DOC_ID}:signed")


def test_declined_event_calls_on_agreement_declined(tc):
    loan = MagicMock(name="loan")
    verifier = _make_verifier()
    with _wired(
        loan=loan, verifier=verifier, webhook_event=_signed_event("declined")
    ) as (db, adapter, lifecycle):
        r = tc.post(_PATH, content=b'{"document_id": "signnow-doc-123"}')

    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    lifecycle.on_agreement_declined.assert_called_once_with(db, loan)
    lifecycle.on_agreement_signed.assert_not_called()


def test_pending_event_is_ignored_no_lifecycle(tc):
    loan = MagicMock(name="loan")
    verifier = _make_verifier()
    with _wired(
        loan=loan, verifier=verifier, webhook_event=_signed_event("pending")
    ) as (db, adapter, lifecycle):
        r = tc.post(_PATH, content=b'{"document_id": "signnow-doc-123"}')

    assert r.status_code == 202
    assert r.json()["status"] == "ignored"
    lifecycle.on_agreement_signed.assert_not_called()
    lifecycle.on_agreement_declined.assert_not_called()


# ---------------------------------------------------------------------------
# Signature failure → 401
# ---------------------------------------------------------------------------


def test_bad_signature_returns_401(tc):
    verifier = _make_verifier()
    with _wired(
        loan=MagicMock(),
        verifier=verifier,
        verify_raises=SignNowWebhookError("Signature mismatch"),
    ) as (db, adapter, lifecycle):
        r = tc.post(_PATH, content=b'{"document_id": "x"}')

    assert r.status_code == 401
    lifecycle.on_agreement_signed.assert_not_called()
    # Nonce never checked — body was untrusted.
    verifier.check_nonce.assert_not_called()


def test_provider_disabled_returns_401(tc):
    """No adapter (provider disabled/unconfigured) ⇒ unverifiable ⇒ 401."""
    verifier = _make_verifier()
    db = _make_db(MagicMock())
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_signature_verifier] = lambda: verifier
    with patch.object(esign, "_build_adapter", return_value=None), patch(
        "app.services.observability.posthog_bridge.capture_event", MagicMock()
    ):
        try:
            r = tc.post(_PATH, content=b'{"document_id": "x"}')
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(get_signature_verifier, None)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Unknown ref → 202 orphaned
# ---------------------------------------------------------------------------


def test_unknown_agreement_ref_returns_202_orphaned(tc):
    verifier = _make_verifier()
    with _wired(loan=None, verifier=verifier, webhook_event=_signed_event()) as (
        db, adapter, lifecycle,
    ):
        r = tc.post(_PATH, content=b'{"document_id": "signnow-doc-123"}')

    assert r.status_code == 202
    assert r.json()["status"] == "orphaned"
    lifecycle.on_agreement_signed.assert_not_called()
    # Nonce claim still persisted so a retry of the same unknown no-ops.
    db.commit.assert_called()


# ---------------------------------------------------------------------------
# Idempotent replay → 202 replay
# ---------------------------------------------------------------------------


def test_replay_returns_202_replay_no_lifecycle(tc):
    loan = MagicMock(name="loan")
    verifier = _make_verifier(replay=True)
    with _wired(loan=loan, verifier=verifier, webhook_event=_signed_event()) as (
        db, adapter, lifecycle,
    ):
        r = tc.post(_PATH, content=b'{"document_id": "signnow-doc-123"}')

    assert r.status_code == 202
    assert r.json()["status"] == "replay"
    lifecycle.on_agreement_signed.assert_not_called()
    lifecycle.on_agreement_declined.assert_not_called()
