"""Render coverage for the full notification template catalog (Dave v1.0).

Every registered notification type must render subject + email body (and SMS
where shipped) against its sample context under StrictUndefined. The sample
contexts live in tests/fixtures/notification_manifests/*.json — the build
manifests from the Dave-template integration — so this suite is the executable
contract between context builders and templates: add a variable to a template
without adding it to the sample context (and the producer) and this fails.

DB-free: pure Jinja rendering.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from jinja2 import StrictUndefined, Template

from app.services import notification_render as nr
from app.services.notification_internal import INTERNAL_NOTICES, build_internal_context

FIXTURES = Path(__file__).parent / "fixtures" / "notification_manifests"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _customer_cases():
    for fname in ("customer_lifecycle.json", "customer_ops.json"):
        for t in _load(fname)["types"]:
            yield pytest.param(t, id=t["key"])


def _dunning_cases():
    for row in _load("dunning_internal.json")["dunning"]:
        for i, ctx in enumerate(row["sample_contexts"]):
            yield pytest.param(row, ctx, id=f"{row['key']}-{i}")


def _internal_cases():
    for n in _load("dunning_internal.json")["internal"]:
        yield pytest.param(n, id=n["key"])


@pytest.mark.parametrize("t", _customer_cases())
def test_customer_template_renders(t):
    # The registry key for two templates differs from the manifest key (they
    # were folded into stable identifiers at integration time).
    key = {
        "offer_confirmation": "application_approved",
        "application_rejected_v2": "application_rejected",
    }.get(t["key"], t["key"])
    spec = nr.get_spec(key)
    subject, html = nr.render_email(key, t["sample_context"])
    assert subject and html
    # Base-layout invariants: brand shell + no leaked Jinja + no Turnkey tokens.
    assert "PaySpyre" in html
    assert "{{" not in html and "*|" not in html
    if t.get("sms_template"):
        assert nr.render_sms(key, t["sample_context"])
    else:
        assert spec.sms_template is None


@pytest.mark.parametrize("row,ctx", _dunning_cases())
def test_dunning_template_renders_each_offset(row, ctx):
    subject, raw_html = nr.render_email(row["key"], ctx)
    html = " ".join(raw_html.split())  # normalize wrapping for phrase asserts
    assert "{{" not in html and "*|" not in html
    days_overdue = ctx.get("days_overdue")
    if row["key"] == "payment_overdue" and days_overdue is not None:
        if int(days_overdue) >= 90:
            assert "DEFAULT" in subject.upper()
            assert "Collections Department" in html
            assert "Without prejudice" in html
            # 90-day tier uses the default-notice wording, not the 30/60 line
            assert "adverse actions" in html.lower()
        else:
            assert "Action Required" in subject
            assert "Collections Department" not in html
            if int(days_overdue) >= 30:
                assert "legal action" in html.lower()
    assert nr.render_sms(row["key"], ctx)


@pytest.mark.parametrize("n", _internal_cases())
def test_internal_notice_renders(n):
    key = n["key"]
    assert key in INTERNAL_NOTICES
    ctx = build_internal_context(key, n["sample_context"])
    subject, html = nr.render_email(key, ctx)
    assert subject and "{{" not in html
    # every detail field the notice declares must appear as a label row
    assert html.count("<strong>") >= len(INTERNAL_NOTICES[key].detail_fields)


def test_every_registry_type_is_exercised():
    """The registry and the fixture manifests can't drift apart silently."""
    covered = set()
    for fname in ("customer_lifecycle.json", "customer_ops.json"):
        for t in _load(fname)["types"]:
            covered.add({
                "offer_confirmation": "application_approved",
                "application_rejected_v2": "application_rejected",
            }.get(t["key"], t["key"]))
    for row in _load("dunning_internal.json")["dunning"]:
        covered.add(row["key"])
    covered |= set(INTERNAL_NOTICES)
    # Types with bespoke templates predating the Dave integration, covered by
    # their own suites (adverse action + under-review + WS-E cancellation,
    # which renders in test_decision_reasons_directory).
    covered |= {
        "application_declined", "application_under_review", "application_cancelled",
        # WS-G auto-collection PAD pre-notification — rendered by its own suite.
        "pad_pre_notification",
        # WS-J hardship amendment notice — rendered in tests/test_hardship.py.
        "hardship_agreement_sent",
    }
    missing = set(nr.NOTIFICATION_TYPES) - covered
    assert not missing, f"registry types without render coverage: {sorted(missing)}"


def test_global_context_defaults_present():
    ctx = nr._global_context()
    for key in ("company_name", "support_email", "dashboard_url", "website_url",
                "terms_url", "privacy_url"):
        assert ctx[key]


def test_subject_never_renders_blank_for_minimal_context():
    """Subjects for wired lifecycle types render from the lifecycle context
    builder's guaranteed keys (regression guard for StrictUndefined)."""
    guaranteed = {
        "full_name": "A B", "borrower_name": "A B", "loan_id": "abcd1234",
        "vendor_name": "Clinic", "outstanding_balance": "$1.00",
        "written_off_amount": "$1.00", "days_overdue": 0,
        "current_date": "July 20, 2026", "first_payment_amount": "$1.00",
        "first_payment_date": "July 20, 2026", "next_payment_amount": "$1.00",
        "due_date": "July 20, 2026", "payment_amount": "$1.00",
        "payment_date": "July 20, 2026",
    }
    for ntype in ("loan_activated", "loan_written_off", "offer_accepted_signing",
                  "agreement_signed", "payment_received", "loan_repaid"):
        subject, html = nr.render_email(ntype, guaranteed)
        assert subject.strip()
