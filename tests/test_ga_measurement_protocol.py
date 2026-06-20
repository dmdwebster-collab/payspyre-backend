"""Tests for app/services/observability/ga_measurement_protocol.py.

No network (respx mocks httpx), no database. Mirrors the PII discipline checks
in test_posthog_bridge.py for the GA4 Measurement Protocol path.

Note: GA_MEASUREMENT_ID / GA_API_SECRET are not yet declared on Settings (see
"WIRING NEEDED" in the PR). The module reads GA config through the patchable
``_ga_config()`` indirection, so these tests patch that rather than the frozen
pydantic Settings model.
"""
from __future__ import annotations

import json

import httpx
import respx

from app.services.observability import ga_measurement_protocol as ga

_COLLECT_URL = "https://www.google-analytics.com/mp/collect"


def _configure(monkeypatch, *, enabled=True, mid="G-TEST123", secret="s3cr3t"):
    monkeypatch.setattr(ga, "_ga_config", lambda: (enabled, mid, secret))


# ---------------------------------------------------------------------------
# Test 1 — configured event is dispatched to the Measurement Protocol endpoint
# ---------------------------------------------------------------------------

@respx.mock
def test_event_dispatched_when_configured(monkeypatch):
    _configure(monkeypatch)
    route = respx.post(_COLLECT_URL).mock(return_value=httpx.Response(204))

    sent = ga.send_ga_event(
        event_name="decision_made",
        raw_client_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        params={"status": "approved"},
    )

    assert sent is True
    assert route.called
    request = route.calls.last.request
    # measurement_id + api_secret travel as query params, not in the body.
    assert request.url.params["measurement_id"] == "G-TEST123"
    assert request.url.params["api_secret"] == "s3cr3t"


# ---------------------------------------------------------------------------
# Test 2 — PII-clean payload: hashed client_id, scalar-only allowlisted params
# ---------------------------------------------------------------------------

@respx.mock
def test_payload_is_pii_clean(monkeypatch):
    _configure(monkeypatch)
    route = respx.post(_COLLECT_URL).mock(return_value=httpx.Response(204))

    raw_app_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    ga.send_ga_event(
        event_name="application_started",
        raw_client_id=raw_app_id,
        params={
            "product_id": "prod_basic",
            "amount_band": "10k-25k",
            "approved": True,
            # These must be stripped — nested structures could smuggle PII.
            "rich_payload": {"email": "test@example.com", "phone": "+15550001234"},
            "patient_profile": ["secret"],
            "missing": None,
        },
    )

    body = json.loads(route.calls.last.request.content)

    # client_id is a 16-char hex hash — never the raw UUID.
    client_id = body["client_id"]
    assert client_id != raw_app_id
    assert raw_app_id not in json.dumps(body)
    assert len(client_id) == 16
    assert all(c in "0123456789abcdef" for c in client_id)

    params = body["events"][0]["params"]
    # Scalars survive.
    assert params["product_id"] == "prod_basic"
    assert params["amount_band"] == "10k-25k"
    assert params["approved"] is True
    # Nested / None values are dropped — no PII leakage path.
    assert "rich_payload" not in params
    assert "patient_profile" not in params
    assert "missing" not in params
    assert "test@example.com" not in json.dumps(body)
    assert "+15550001234" not in json.dumps(body)
    assert body["events"][0]["name"] == "application_started"


# ---------------------------------------------------------------------------
# Test 3 — no-op (no network) when measurement id / api secret unset
# ---------------------------------------------------------------------------

@respx.mock
def test_noop_when_unconfigured(monkeypatch):
    _configure(monkeypatch, mid="", secret="")
    route = respx.post(_COLLECT_URL).mock(return_value=httpx.Response(204))

    sent = ga.send_ga_event(
        event_name="decision_made",
        raw_client_id="app-1",
        params={"status": "approved"},
    )

    assert sent is False
    assert not route.called


# ---------------------------------------------------------------------------
# Test 4 — no-op when OBSERVABILITY_ENABLED is False
# ---------------------------------------------------------------------------

@respx.mock
def test_noop_when_observability_disabled(monkeypatch):
    _configure(monkeypatch, enabled=False)
    route = respx.post(_COLLECT_URL).mock(return_value=httpx.Response(204))

    sent = ga.send_ga_event(
        event_name="decision_made", raw_client_id="app-1", params={}
    )

    assert sent is False
    assert not route.called


# ---------------------------------------------------------------------------
# Test 5 — transport failure is swallowed (never breaks the caller)
# ---------------------------------------------------------------------------

@respx.mock
def test_transport_error_is_silent(monkeypatch):
    _configure(monkeypatch)
    respx.post(_COLLECT_URL).mock(side_effect=httpx.ConnectError("boom"))

    # Must not raise; returns False on swallowed failure.
    sent = ga.send_ga_event(
        event_name="decision_made", raw_client_id="app-1", params={}
    )
    assert sent is False


# ---------------------------------------------------------------------------
# Test 6 — _safe_params unit: scalar allowlist by shape
# ---------------------------------------------------------------------------

def test_safe_params_drops_non_scalars():
    out = ga._safe_params(
        {
            "s": "x",
            "i": 3,
            "f": 1.5,
            "b": False,
            "none": None,
            "dict": {"a": 1},
            "list": [1, 2],
        }
    )
    assert out == {"s": "x", "i": 3, "f": 1.5, "b": False}
