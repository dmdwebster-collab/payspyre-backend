"""Activation rework Wave 4a — borrower-facing application agreement endpoints.

The borrower half of Dave's pre-loan lifecycle: an approved borrower reviews +
e-signs their APPLICATION-level agreement (before any loan is booked) from their
own dashboard. Two endpoints, both app-scoped:

* ``GET  /applications/{id}/agreement``               — agreement state to review.
* ``POST /applications/{id}/agreement/simulate-sign`` — Simulator e-sign
  (``sent`` -> ``signed``); LIVE 409s; must be ``sent`` first.

DELIBERATELY DB-FREE + NETWORK-FREE (the suite shares a remote DB and must not be
run wholesale). We mount JUST the agreement router on a bare FastAPI app, override
``get_db``/``get_current_applicant``, and monkeypatch ``integration_mode`` so no
real credential lookup / DB read happens. The endpoint DELEGATES to the Wave 1
``application_agreement`` service — we assert the delegation + the borrower-only
precondition (must be ``sent``) that the borrower surface layers on top.

Run JUST this file:
    source .venv/bin/activate && \
        python -m pytest tests/test_activation_rework_wave4.py -p no:warnings -q
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.applicant.v1.deps import (
    ApplicantClaims,
    get_current_applicant,
    get_db,
)
from app.api.applicant.v1.endpoints import agreement as agreement_ep
from app.models.platform.credit_application import PlatformCreditApplication
from app.services import application_agreement, integration_mode


# ---------------------------------------------------------------------------
# In-memory fakes (no database)
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._row


class _FakeSession:
    def __init__(self, application):
        self._application = application
        self.added = []
        self.commits = 0

    def query(self, model):
        if model is PlatformCreditApplication:
            return _FakeQuery(self._application)
        return _FakeQuery(None)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commits += 1

    def refresh(self, obj, **kw):
        pass


class _Application:
    def __init__(self, *, agreement_status="sent", agreement_ref=None):
        self.id = uuid.uuid4()
        self.patient_id = uuid.uuid4()
        self.patient = None
        self.email = "borrower@example.com"
        self.first_name = "Borrower"
        self.last_name = "One"
        self.agreement_status = agreement_status
        if agreement_ref is None and agreement_status in ("sent", "signed"):
            agreement_ref = (
                f"{application_agreement.SIMULATED_AGREEMENT_REF_PREFIX}{self.id}"
            )
        self.agreement_ref = agreement_ref
        self.agreement_signed_at = (
            datetime.now(timezone.utc) if agreement_status == "signed" else None
        )


def _client(application, *, token_app_ids=None, live=False):
    """Bare app mounting only the agreement router, with db/auth overridden and
    integration_mode forced to simulator (or live)."""
    if token_app_ids is None:
        token_app_ids = [application.id]

    app = FastAPI()
    app.include_router(agreement_ep.router, prefix="/api/applicant/v1")

    session = _FakeSession(application)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_applicant] = lambda: ApplicantClaims(
        patient_id=application.patient_id, app_ids=list(token_app_ids)
    )
    return TestClient(app), session


@pytest.fixture(autouse=True)
def _force_simulator(monkeypatch):
    """Default every test to SIMULATOR mode (no DB credential lookup). Individual
    tests flip to live by re-patching."""
    monkeypatch.setattr(integration_mode, "resolve_mode", lambda db, p: "simulator")
    monkeypatch.setattr(integration_mode, "is_live", lambda db, p: False)
    monkeypatch.setattr(integration_mode, "is_simulator", lambda db, p: True)


def _go_live(monkeypatch):
    monkeypatch.setattr(integration_mode, "resolve_mode", lambda db, p: "live")
    monkeypatch.setattr(integration_mode, "is_live", lambda db, p: True)
    monkeypatch.setattr(integration_mode, "is_simulator", lambda db, p: False)


# ===========================================================================
# GET /agreement — state to review
# ===========================================================================


def test_get_agreement_returns_state():
    app_row = _Application(agreement_status="sent")
    client, _ = _client(app_row)

    resp = client.get(f"/api/applicant/v1/applications/{app_row.id}/agreement")

    assert resp.status_code == 200
    body = resp.json()
    assert body["application_id"] == str(app_row.id)
    assert body["agreement_status"] == "sent"
    assert body["agreement_ref"] == app_row.agreement_ref
    assert body["mode"] == "simulator"
    # The document to review is the reference the send produced.
    assert body["document_html_or_ref"] == app_row.agreement_ref
    assert body["agreement_signed_at"] is None


def test_get_agreement_rejects_foreign_application_token():
    app_row = _Application(agreement_status="sent")
    # Token authorized for a DIFFERENT application only.
    client, _ = _client(app_row, token_app_ids=[uuid.uuid4()])

    resp = client.get(f"/api/applicant/v1/applications/{app_row.id}/agreement")

    assert resp.status_code == 403


# ===========================================================================
# POST /agreement/simulate-sign — sent -> signed (delegates to Wave 1 service)
# ===========================================================================


def test_simulate_sign_advances_sent_to_signed():
    app_row = _Application(agreement_status="sent")
    client, session = _client(app_row)

    resp = client.post(
        f"/api/applicant/v1/applications/{app_row.id}/agreement/simulate-sign"
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["agreement_status"] == "signed"
    assert body["agreement_signed_at"] is not None
    assert body["simulated"] is True
    assert body["mode"] == "simulator"
    # Delegated to the service: the application really transitioned + an audit
    # event was appended + committed.
    assert app_row.agreement_status == "signed"
    assert session.commits >= 1
    signed_events = [
        e for e in session.added
        if getattr(e, "event_type", None)
        == application_agreement.APPLICATION_AGREEMENT_SIGNED_EVENT
    ]
    assert signed_events and signed_events[0].payload["after"]["simulated"] is True


def test_simulate_sign_rejects_foreign_application_token():
    app_row = _Application(agreement_status="sent")
    client, session = _client(app_row, token_app_ids=[uuid.uuid4()])

    resp = client.post(
        f"/api/applicant/v1/applications/{app_row.id}/agreement/simulate-sign"
    )

    assert resp.status_code == 403
    # Nothing was touched.
    assert app_row.agreement_status == "sent"
    assert session.commits == 0


def test_simulate_sign_409s_in_live_mode(monkeypatch):
    _go_live(monkeypatch)
    app_row = _Application(agreement_status="sent")
    client, _ = _client(app_row, live=True)

    resp = client.post(
        f"/api/applicant/v1/applications/{app_row.id}/agreement/simulate-sign"
    )

    assert resp.status_code == 409
    assert app_row.agreement_status == "sent"  # unchanged


def test_simulate_sign_before_sent_is_409():
    # The borrower-only precondition: cannot sign an agreement never sent.
    app_row = _Application(agreement_status="not_sent", agreement_ref=None)
    client, session = _client(app_row)

    resp = client.post(
        f"/api/applicant/v1/applications/{app_row.id}/agreement/simulate-sign"
    )

    assert resp.status_code == 409
    assert "not been sent" in resp.json()["detail"].lower()
    assert app_row.agreement_status == "not_sent"
    assert session.commits == 0


def test_simulate_sign_idempotent_when_already_signed():
    app_row = _Application(agreement_status="signed")
    client, _ = _client(app_row)

    resp = client.post(
        f"/api/applicant/v1/applications/{app_row.id}/agreement/simulate-sign"
    )

    assert resp.status_code == 200
    assert resp.json()["agreement_status"] == "signed"


def test_simulate_sign_missing_application_404():
    app_row = _Application(agreement_status="sent")
    client, session = _client(app_row)
    session._application = None  # application lookup now misses

    resp = client.post(
        f"/api/applicant/v1/applications/{app_row.id}/agreement/simulate-sign"
    )

    assert resp.status_code == 404
