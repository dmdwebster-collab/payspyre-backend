"""Unit tests for the Canadian notice-of-decision (rejected) service.

Fully mocked: a MagicMock dispatcher + a MagicMock DB session — NO live DB, NO
network. We control the idempotency query (``_already_sent``) and the patient
lookup by stubbing ``db.execute`` / ``db.query`` return values.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.services import decision_notice


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_application():
    return SimpleNamespace(id=uuid4(), patient_id=uuid4(), status="rejected")


def _make_patient(email="applicant@example.com", first="Jane", last="Doe"):
    return SimpleNamespace(email=email, legal_first_name=first, legal_last_name=last)


def _make_db(*, already_sent: bool, patient):
    """A MagicMock Session.

    - ``db.execute(...).first()`` drives the idempotency check: returns a truthy
      row when already_sent, else None.
    - ``db.query(...).filter(...).first()`` returns the patient.
    """
    db = MagicMock()
    db.execute.return_value.first.return_value = (1,) if already_sent else None
    db.query.return_value.filter.return_value.first.return_value = patient
    return db


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------


def test_content_includes_reasons_and_canadian_disclosures():
    content = decision_notice.build_notice_content(
        applicant_name="Jane Doe",
        application_id="app-123",
        reasons=["bureau_below_minimum", "verification_failed:identity"],
    )
    html = decision_notice.render_notice_html(content)

    # Principal reasons translated to applicant-facing text.
    assert "minimum requirement" in html
    assert "identity verification" in html  # verification_failed:identity line

    # Canadian non-discrimination notice (human rights regime, not US ECOA).
    assert "Canadian Human Rights Act" in html
    assert "human rights" in html.lower()

    # Canadian consumer-report rights (PIPEDA + provincial), with dispute right.
    assert "PIPEDA" in html or "Personal Information Protection" in html
    assert "dispute" in html

    # Guard: NO US statutes / regulators leak into a Canadian-only notice.
    for us_term in (
        "Equal Credit Opportunity Act",
        "Fair Credit Reporting Act",
        "Consumer Financial Protection Bureau",
        "Washington",
    ):
        assert us_term not in html, f"US reference leaked: {us_term}"


def test_unknown_reason_code_falls_back_safely():
    lines = decision_notice.humanize_reasons(["totally_unknown_code"])
    assert lines == ["Your application did not meet our current lending criteria."]


def test_empty_reasons_still_yields_a_principal_reason():
    lines = decision_notice.humanize_reasons([])
    assert len(lines) == 1
    assert lines[0]


def test_generic_cra_disclosure_when_no_real_bureau(monkeypatch):
    monkeypatch.setattr(decision_notice.settings, "USE_REAL_ADAPTERS", False, raising=False)
    content = decision_notice.build_notice_content(
        applicant_name="Jane Doe", application_id="x", reasons=["bureau_below_minimum"]
    )
    assert content["bureau"]["bureau_used"] is False
    html = decision_notice.render_notice_html(content)
    assert "consumer reporting agency" in html.lower()


def test_named_bureau_disclosure_when_real_bureau(monkeypatch):
    monkeypatch.setattr(decision_notice.settings, "USE_REAL_ADAPTERS", True, raising=False)
    monkeypatch.setattr(decision_notice.settings, "EQUIFAX_API_KEY", "live-key", raising=False)
    content = decision_notice.build_notice_content(
        applicant_name="Jane Doe", application_id="x", reasons=["bureau_below_minimum"]
    )
    assert content["bureau"]["bureau_used"] is True
    assert content["bureau"]["name"] == "Equifax Canada Co."
    html = decision_notice.render_notice_html(content)
    assert "Equifax Canada Co." in html
    assert "1-800-465-7166" in html


# ---------------------------------------------------------------------------
# Send path
# ---------------------------------------------------------------------------


def test_sends_to_patient_email_and_records_event():
    application = _make_application()
    patient = _make_patient(email="patient@example.com")
    db = _make_db(already_sent=False, patient=patient)
    dispatcher = MagicMock()

    sent = decision_notice.send_decision_notice(
        db, application, ["bureau_below_minimum"], dispatcher
    )

    assert sent is True
    # Sent to the patient's email.
    dispatcher.send_email.assert_called_once()
    kwargs = dispatcher.send_email.call_args.kwargs
    assert kwargs["to"] == "patient@example.com"
    assert "Canadian Human Rights Act" in kwargs["html_content"]
    assert "Equal Credit Opportunity Act" not in kwargs["html_content"]
    assert "minimum requirement" in kwargs["html_content"]

    # An audit event was added + flushed.
    assert db.add.called
    added = db.add.call_args.args[0]
    assert added.event_type == decision_notice.DECISION_NOTICE_EVENT_TYPE
    assert added.application_id == application.id
    assert added.patient_id == application.patient_id
    # No PII in the event payload — only ids + reason codes + bureau name.
    payload_str = str(added.payload)
    assert "patient@example.com" not in payload_str
    assert added.payload["after"]["reason_codes"] == ["bureau_below_minimum"]
    assert db.flush.called


def test_idempotent_does_not_send_twice():
    application = _make_application()
    patient = _make_patient()
    db = _make_db(already_sent=True, patient=patient)
    dispatcher = MagicMock()

    sent = decision_notice.send_decision_notice(
        db, application, ["bureau_below_minimum"], dispatcher
    )

    assert sent is False
    dispatcher.send_email.assert_not_called()
    db.add.assert_not_called()


def test_no_email_skips_send():
    application = _make_application()
    patient = _make_patient(email=None)
    db = _make_db(already_sent=False, patient=patient)
    dispatcher = MagicMock()

    sent = decision_notice.send_decision_notice(
        db, application, ["bureau_below_minimum"], dispatcher
    )

    assert sent is False
    dispatcher.send_email.assert_not_called()
    db.add.assert_not_called()


def test_swallows_dispatcher_error_and_does_not_record():
    application = _make_application()
    patient = _make_patient()
    db = _make_db(already_sent=False, patient=patient)
    dispatcher = MagicMock()
    dispatcher.send_email.side_effect = RuntimeError("vendor down")

    # Must NOT raise into the caller.
    sent = decision_notice.send_decision_notice(
        db, application, ["bureau_below_minimum"], dispatcher
    )

    assert sent is False
    # Send was attempted, but the error was swallowed and no event recorded.
    dispatcher.send_email.assert_called_once()
    db.add.assert_not_called()


def test_missing_patient_is_swallowed():
    application = _make_application()
    db = _make_db(already_sent=False, patient=None)
    dispatcher = MagicMock()

    sent = decision_notice.send_decision_notice(
        db, application, ["bureau_below_minimum"], dispatcher
    )

    assert sent is False
    dispatcher.send_email.assert_not_called()


def test_undelivered_send_records_failure_event_not_sent():
    # The provider accepted nothing (falsy return) — the notice must NOT be recorded
    # as sent; a failure event is recorded instead so it's auditable.
    application = _make_application()
    patient = _make_patient(email="patient@example.com")
    db = _make_db(already_sent=False, patient=patient)
    dispatcher = MagicMock()
    dispatcher.send_email.return_value = False

    sent = decision_notice.send_decision_notice(
        db, application, ["bureau_below_minimum"], dispatcher
    )

    assert sent is False
    added_types = [getattr(c.args[0], "event_type", None) for c in db.add.call_args_list]
    assert decision_notice.DECISION_NOTICE_FAILED_EVENT_TYPE in added_types
    assert decision_notice.DECISION_NOTICE_EVENT_TYPE not in added_types


def test_default_dispatcher_routes_to_sendgrid_when_configured(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "EMAIL_PROVIDER", "sendgrid")
    monkeypatch.setattr(settings, "SENDGRID_API_KEY", "SG.testkey")
    monkeypatch.setattr(settings, "SENDGRID_FROM_EMAIL", "noreply@payspyre.com")

    captured = {}

    def _fake_send(**kw):
        captured.update(kw)
        return True

    monkeypatch.setattr(decision_notice, "_send_via_sendgrid", _fake_send)

    ok = decision_notice._EmailServiceDispatcher().send_email(
        to="x@example.com", subject="s", html_content="<p>h</p>", text_content="t"
    )

    assert ok is True
    assert captured["to"] == "x@example.com"
    assert captured["api_key"] == "SG.testkey"
    assert captured["from_email"] == "noreply@payspyre.com"
