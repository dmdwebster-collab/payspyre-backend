"""Unit tests for §11 Prometheus metrics (audit H-8).

No DB, no app startup — exercises the helper functions + the /metrics text
output via ``generate_latest()`` directly. Run in isolation:

    source .venv/bin/activate && python -m pytest tests/test_platform_metrics.py -p no:warnings -q
"""
from __future__ import annotations

from prometheus_client import generate_latest

from app.services.metrics import platform_metrics as m


def _value(counter, **labels) -> float:
    """Read a single labelled child's current value from a Counter/Histogram."""
    return counter.labels(**labels)._value.get()


# ---------------------------------------------------------------------------
# Helpers increment the right metric + labels
# ---------------------------------------------------------------------------


def test_record_application_started_increments():
    before = _value(m.application_started_total, credit_product_code="dental_v1", vertical="dental")
    m.record_application_started("dental_v1", vendor_id="ignored-uuid", vertical="dental")
    after = _value(m.application_started_total, credit_product_code="dental_v1", vertical="dental")
    assert after == before + 1


def test_record_application_started_drops_vendor_uuid_from_labels():
    # vendor_id must NOT appear as a label dimension (Hard Rule #7 — unbounded UUID).
    assert "vendor_id" not in m.application_started_total._labelnames
    assert set(m.application_started_total._labelnames) == {"credit_product_code", "vertical"}


def test_record_step_completed_increments():
    before = _value(
        m.application_step_completed_total,
        credit_product_code="dental_v1", step_type="bank_link", result="passed",
    )
    m.record_step_completed("dental_v1", "bank_link", "passed")
    after = _value(
        m.application_step_completed_total,
        credit_product_code="dental_v1", step_type="bank_link", result="passed",
    )
    assert after == before + 1


def test_record_decision_increments():
    before = _value(m.application_completed_total, credit_product_code="dental_v1", decision="approved")
    m.record_decision("dental_v1", "approved")
    after = _value(m.application_completed_total, credit_product_code="dental_v1", decision="approved")
    assert after == before + 1


def test_record_verification_completed_increments_and_observes_cost():
    before = _value(
        m.verification_completed_total, type="kyc_id", vendor="didit", status="passed"
    )
    cost_before = m.verification_cost_cents.labels(type="kyc_id", vendor="didit")._sum.get()
    m.record_verification_completed(type="kyc_id", vendor="didit", status="passed", cost_cents=150)
    after = _value(
        m.verification_completed_total, type="kyc_id", vendor="didit", status="passed"
    )
    cost_after = m.verification_cost_cents.labels(type="kyc_id", vendor="didit")._sum.get()
    assert after == before + 1
    assert cost_after == cost_before + 150


def test_record_consent_granted_and_revoked_split_by_flag():
    g_before = _value(m.consent_granted_total, purpose="bank_verification")
    r_before = _value(m.consent_revoked_total, purpose="bank_verification")
    m.record_consent("bank_verification", granted=True)
    m.record_consent("bank_verification", granted=False)
    assert _value(m.consent_granted_total, purpose="bank_verification") == g_before + 1
    assert _value(m.consent_revoked_total, purpose="bank_verification") == r_before + 1


def test_record_lead_state_changed_increments():
    before = _value(m.lead_state_changed_total, from_state="new", to_state="qualified")
    m.record_lead_state_changed("new", "qualified")
    after = _value(m.lead_state_changed_total, from_state="new", to_state="qualified")
    assert after == before + 1


def test_record_marketplace_helpers_increment():
    l_before = _value(
        m.marketplace_listing_created_total, treatment_category="ortho", lead_state="open"
    )
    m.record_marketplace_listing_created("ortho", "open")
    assert _value(
        m.marketplace_listing_created_total, treatment_category="ortho", lead_state="open"
    ) == l_before + 1

    i_before = _value(m.marketplace_vendor_interest_total, treatment_category="ortho")
    m.record_marketplace_vendor_interest(vendor_id="ignored", treatment_category="ortho")
    assert _value(m.marketplace_vendor_interest_total, treatment_category="ortho") == i_before + 1

    c_before = _value(m.marketplace_lead_charged_total, charge_trigger="accepted")
    amt_before = m.marketplace_lead_charge_amount_cents.labels(charge_trigger="accepted")._sum.get()
    m.record_marketplace_lead_charged("accepted", amount_cents=2500, vendor_id="ignored")
    assert _value(m.marketplace_lead_charged_total, charge_trigger="accepted") == c_before + 1
    amt_after = m.marketplace_lead_charge_amount_cents.labels(charge_trigger="accepted")._sum.get()
    assert amt_after == amt_before + 2500


# Marketplace vendor_id never becomes a label.
def test_marketplace_vendor_id_not_a_label():
    assert "vendor_id" not in m.marketplace_vendor_interest_total._labelnames
    assert "vendor_id" not in m.marketplace_lead_charged_total._labelnames


# ---------------------------------------------------------------------------
# /metrics output contains the metric names
# ---------------------------------------------------------------------------


def test_generate_latest_contains_all_metric_names():
    # Touch each metric so it materialises at least one sample line.
    m.record_application_started("p", vertical="v")
    m.record_step_completed("p", "s", "r")
    m.record_decision("p", "approved")
    m.record_verification_completed(type="t", vendor="v", status="passed", cost_cents=10)
    m.record_consent("purpose", granted=True)
    m.record_consent("purpose", granted=False)
    m.record_lead_state_changed("a", "b")
    m.record_marketplace_listing_created("cat", "open")
    m.record_marketplace_vendor_interest(treatment_category="cat")
    m.record_marketplace_lead_charged("accepted", amount_cents=100)

    text = generate_latest().decode()
    for name in (
        "application_started_total",
        "application_step_completed_total",
        "application_completed_total",
        "verification_completed_total",
        "verification_cost_cents",
        "consent_granted_total",
        "consent_revoked_total",
        "lead_state_changed_total",
        "marketplace_listing_created_total",
        "marketplace_vendor_interest_total",
        "marketplace_lead_charged_total",
        "marketplace_lead_charge_amount_cents",
    ):
        assert name in text, f"{name} missing from /metrics output"


def test_metrics_endpoint_router_serves_text():
    # The router module wires generate_latest() with the prometheus content type.
    from app.api.metrics import metrics

    resp = metrics()
    assert resp.status_code == 200
    assert "text/plain" in resp.media_type
    assert b"application_started_total" in resp.body


# ---------------------------------------------------------------------------
# Helpers swallow bad input — never raise
# ---------------------------------------------------------------------------


def test_helpers_swallow_none_and_weird_input():
    # None labels coerce to "unknown"; nothing raises.
    m.record_application_started(None, vendor_id=None, vertical=None)
    m.record_step_completed(None, None, None)
    m.record_decision(None, None)
    m.record_consent(None, granted=None)
    m.record_lead_state_changed(None, None)
    m.record_marketplace_listing_created(None, None)
    m.record_marketplace_vendor_interest()
    m.record_marketplace_lead_charged(None)
    assert _value(m.application_started_total, credit_product_code="unknown", vertical="unknown") >= 1


def test_non_numeric_cost_does_not_raise_and_counter_still_increments():
    before = _value(
        m.verification_completed_total, type="kyc_id", vendor="didit", status="failed"
    )
    # Bad cost_cents must not break the counter increment.
    m.record_verification_completed(
        type="kyc_id", vendor="didit", status="failed", cost_cents="not-a-number"
    )
    after = _value(
        m.verification_completed_total, type="kyc_id", vendor="didit", status="failed"
    )
    assert after == before + 1


def test_long_label_value_is_truncated():
    long_code = "x" * 500
    m.record_decision(long_code, "approved")
    text = generate_latest().decode()
    # The 64-char-truncated form is present; the full 500-char form is not.
    assert ("x" * 64) in text
    assert long_code not in text
