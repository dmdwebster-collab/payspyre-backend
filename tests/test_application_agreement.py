"""Unit tests for the APPLICATION-level agreement + Simulate-Signing service
(activation rework Wave 1 — the pre-loan port of the loan-level e-sign path).

DELIBERATELY DB-FREE and NETWORK-FREE (the suite shares a remote DB and must not
be run wholesale). We use the same lightweight in-memory fakes as
``test_loan_lifecycle.py``: a fake Session that records add/commit/refresh, a
fake application object, and a mocked SignNow adapter injected straight into the
service (``signnow=``), so no real adapter / credential lookup / HTTP call ever
happens. Live-vs-simulator mode is driven by monkeypatching
``integration_mode`` resolution.

Run JUST this file:

    source .venv/bin/activate && \
        python -m pytest tests/test_application_agreement.py -p no:warnings -q
"""
import pytest

from app.services import application_agreement
from app.services.esign.signnow_adapter import SignNowSendResult


# ---------------------------------------------------------------------------
# In-memory fakes (mirrors tests/test_loan_lifecycle.py)
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, query_results=None):
        self.added = []
        self.commits = 0
        self._query_results = query_results or {}

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commits += 1

    def refresh(self, obj, **kwargs):
        pass

    def query(self, *args, **kwargs):
        return _FakeQuery(self._query_results)


class _FakeQuery:
    def __init__(self, results):
        self._results = results

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._results.get("first")

    def all(self):
        return self._results.get("all", [])


class _Application:
    def __init__(self, *, agreement_status="not_sent"):
        self.id = "app-1"
        self.patient_id = "patient-1"
        self.patient = None
        self.email = "borrower@example.com"
        self.first_name = "Borrower"
        self.last_name = "One"
        self.agreement_status = agreement_status
        self.agreement_ref = None
        self.agreement_signed_at = None


class _MockSignNow:
    def __init__(self, document_id="snd_123"):
        self.document_id = document_id
        self.calls = []

    def send_for_signature(self, **kwargs):
        self.calls.append(kwargs)
        return SignNowSendResult(document_id=self.document_id, signing_url="https://x")


def _force_live(monkeypatch, live=True):
    """Force integration_mode to report live/simulator regardless of db state."""
    monkeypatch.setattr(
        application_agreement.integration_mode,
        "is_live",
        lambda db, provider: live,
    )
    monkeypatch.setattr(
        application_agreement.integration_mode,
        "is_simulator",
        lambda db, provider: not live,
    )


# ===========================================================================
# send_agreement_for_application — mode-aware
# ===========================================================================


def test_send_for_application_simulates_in_simulator_mode():
    # Default (no signnow row -> simulator): send is NOT a no-op — it really
    # transitions the agreement to "sent" with a labelled simulated ref.
    app = _Application()
    db = _FakeSession(query_results={"first": None})

    out = application_agreement.send_agreement_for_application(db, app)

    assert out.agreement_status == "sent"
    assert out.agreement_ref == (
        f"{application_agreement.SIMULATED_AGREEMENT_REF_PREFIX}{app.id}"
    )
    assert db.commits == 1
    sent = [
        e for e in db.added
        if getattr(e, "event_type", None)
        == application_agreement.APPLICATION_AGREEMENT_SENT_EVENT
    ]
    assert sent and sent[0].payload["after"]["simulated"] is True


def test_send_for_application_uses_real_adapter_when_injected():
    # An injected adapter always takes the real send path (test / live seam).
    app = _Application()
    db = _FakeSession(query_results={"first": None})
    adapter = _MockSignNow(document_id="snd_real")

    out = application_agreement.send_agreement_for_application(
        db, app, signnow=adapter
    )

    assert out.agreement_status == "sent"
    assert out.agreement_ref == "snd_real"
    assert len(adapter.calls) == 1
    assert db.commits == 1


def test_send_for_application_live_unconfigured_is_graceful_noop(monkeypatch):
    # Live mode but no adapter can be built (creds absent) -> graceful no-op,
    # left not_sent for a retry once creds land.
    _force_live(monkeypatch, live=True)
    app = _Application()
    db = _FakeSession(query_results={"first": None})

    out = application_agreement.send_agreement_for_application(db, app)

    assert out.agreement_status == "not_sent"
    assert out.agreement_ref is None
    assert db.commits == 0


def test_send_for_application_idempotent_when_already_sent():
    app = _Application(agreement_status="sent")
    app.agreement_ref = "snd_existing"
    db = _FakeSession()
    adapter = _MockSignNow(document_id="snd_new")

    out = application_agreement.send_agreement_for_application(
        db, app, signnow=adapter
    )

    assert adapter.calls == []
    assert out.agreement_ref == "snd_existing"
    assert db.commits == 0


# ===========================================================================
# simulate_signing_for_application — not_sent -> sent -> signed
# ===========================================================================


def test_simulate_signing_drives_sent_to_signed_and_stamps_signed_at():
    app = _Application(agreement_status="sent")
    app.agreement_ref = (
        f"{application_agreement.SIMULATED_AGREEMENT_REF_PREFIX}{'app-1'}"
    )
    db = _FakeSession(query_results={"first": None})  # simulator

    out = application_agreement.simulate_signing_for_application(db, app)

    assert out.agreement_status == "signed"
    assert out.agreement_signed_at is not None
    signed = [
        e for e in db.added
        if getattr(e, "event_type", None)
        == application_agreement.APPLICATION_AGREEMENT_SIGNED_EVENT
    ]
    assert signed and signed[0].payload["after"]["simulated"] is True
    assert db.commits == 1


def test_simulate_signing_full_flow_not_sent_to_sent_to_signed():
    # Drive the full lifecycle through the public service: not_sent -> sent
    # (send) -> signed (simulate_signing).
    app = _Application()  # not_sent
    db = _FakeSession(query_results={"first": None})  # simulator throughout

    application_agreement.send_agreement_for_application(db, app)
    assert app.agreement_status == "sent"
    assert app.agreement_signed_at is None

    application_agreement.simulate_signing_for_application(db, app)
    assert app.agreement_status == "signed"
    assert app.agreement_signed_at is not None


def test_simulate_signing_accepts_out_of_order_not_sent():
    # If the agreement was never "sent", simulate-signing is still accepted
    # (mirrors the real webhook's tolerance).
    app = _Application(agreement_status="not_sent")
    db = _FakeSession(query_results={"first": None})

    out = application_agreement.simulate_signing_for_application(db, app)

    assert out.agreement_status == "signed"
    assert out.agreement_signed_at is not None


def test_simulate_signing_idempotent_when_already_signed():
    app = _Application(agreement_status="signed")
    db = _FakeSession(query_results={"first": None})

    out = application_agreement.simulate_signing_for_application(db, app)

    assert out.agreement_status == "signed"
    # No new event / commit on the idempotent no-op.
    assert db.commits == 0


def test_simulate_signing_rejects_declined():
    app = _Application(agreement_status="declined")
    db = _FakeSession(query_results={"first": None})

    with pytest.raises(application_agreement.ESignModeError):
        application_agreement.simulate_signing_for_application(db, app)


def test_simulate_signing_409s_in_live_mode(monkeypatch):
    # Live mode disables Simulate Signing (the real SignNow flow applies).
    _force_live(monkeypatch, live=True)
    app = _Application(agreement_status="sent")
    db = _FakeSession(query_results={"first": None})

    with pytest.raises(application_agreement.ESignModeError):
        application_agreement.simulate_signing_for_application(db, app)
