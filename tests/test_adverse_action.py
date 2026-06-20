"""Unit tests for the adverse-action (ECOA/FCRA) notice service.

Fully mocked: a MagicMock dispatcher + a MagicMock DB session — NO live DB, NO
network. We control the idempotency query (``_already_sent``) and the patient
lookup by stubbing ``db.execute`` / ``db.query`` return values.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services import adverse_action


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_application():
    return SimpleNamespace(id=uuid4(), patient_id=uuid4(), status="declined")


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


def test_content_includes_reasons_and_ecoa_boilerplate():
    content = adverse_action.build_notice_content(
        applicant_name="Jane Doe",
        application_id="app-123",
        reasons=["bureau_below_minimum", "verification_failed:identity"],
    )
    html = adverse_action.render_notice_html(content)

    # Principal reasons translated to applicant-facing text.
    assert "minimum requirement" in html
    assert "identity verification" in html  # verification_failed:identity line

    # ECOA non-discrimination boilerplate.
    assert "Equal Credit Opportunity Act" in html
    assert "race, color, religion, national origin, sex, marital status" in html
    assert "Consumer Financial Protection Bureau" in html

    # FCRA consumer rights — free report within 60 days + dispute.
    assert "60 days" in html
    assert "dispute" in html
    assert "Fair Credit Reporting Act" in html


def test_unknown_reason_code_falls_back_safely():
    lines = adverse_action.humanize_reasons(["totally_unknown_code"])
    assert lines == ["Your application did not meet our current lending criteria."]


def test_empty_reasons_still_yields_a_principal_reason():
    lines = adverse_action.humanize_reasons([])
    assert len(lines) == 1
    assert lines[0]


def test_generic_cra_disclosure_when_no_real_bureau(monkeypatch):
    monkeypatch.setattr(adverse_action.settings, "USE_REAL_ADAPTERS", False, raising=False)
    content = adverse_action.build_notice_content(
        applicant_name="Jane Doe", application_id="x", reasons=["bureau_below_minimum"]
    )
    assert content["bureau"]["bureau_used"] is False
    html = adverse_action.render_notice_html(content)
    assert "consumer reporting agency" in html.lower()


def test_named_bureau_disclosure_when_real_bureau(monkeypatch):
    monkeypatch.setattr(adverse_action.settings, "USE_REAL_ADAPTERS", True, raising=False)
    monkeypatch.setattr(adverse_action.settings, "EQUIFAX_API_KEY", "live-key", raising=False)
    content = adverse_action.build_notice_content(
        applicant_name="Jane Doe", application_id="x", reasons=["bureau_below_minimum"]
    )
    assert content["bureau"]["bureau_used"] is True
    assert content["bureau"]["name"] == "Equifax Canada Co."
    html = adverse_action.render_notice_html(content)
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

    sent = adverse_action.send_adverse_action_notice(
        db, application, ["bureau_below_minimum"], dispatcher
    )

    assert sent is True
    # Sent to the patient's email.
    dispatcher.send_email.assert_called_once()
    kwargs = dispatcher.send_email.call_args.kwargs
    assert kwargs["to"] == "patient@example.com"
    assert "Equal Credit Opportunity Act" in kwargs["html_content"]
    assert "minimum requirement" in kwargs["html_content"]

    # An audit event was added + flushed.
    assert db.add.called
    added = db.add.call_args.args[0]
    assert added.event_type == adverse_action.ADVERSE_ACTION_EVENT_TYPE
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

    sent = adverse_action.send_adverse_action_notice(
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

    sent = adverse_action.send_adverse_action_notice(
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
    sent = adverse_action.send_adverse_action_notice(
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

    sent = adverse_action.send_adverse_action_notice(
        db, application, ["bureau_below_minimum"], dispatcher
    )

    assert sent is False
    dispatcher.send_email.assert_not_called()
