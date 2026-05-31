"""Tests for app/services/observability/posthog_bridge.py — P8.0."""
from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(event_type: str, application_id=None, payload=None):
    """Return a minimal PlatformEvent-like mock."""
    ev = MagicMock()
    ev.event_type = event_type
    ev.application_id = application_id or "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    ev.patient_id = "ffffffff-0000-1111-2222-333333333333"
    ev.id = 42
    ev.payload = payload or {}
    return ev


# ---------------------------------------------------------------------------
# Test 1 — Allowlist event is forwarded to posthog.capture()
# ---------------------------------------------------------------------------

def test_allowlist_event_captured(monkeypatch):
    """capture_event calls posthog.capture for a whitelisted event type."""
    mock_posthog = MagicMock()
    monkeypatch.setattr("app.core.config.settings.OBSERVABILITY_ENABLED", True)
    monkeypatch.setattr("app.core.config.settings.POSTHOG_API_KEY", "phc_test")
    monkeypatch.setattr(
        "app.core.config.settings.OBSERVABILITY_POSTHOG_ALLOWLIST",
        "verification_completed,decision_made",
    )

    with patch.dict(sys.modules, {"posthog": mock_posthog}):
        from app.services.observability import posthog_bridge
        importlib.reload(posthog_bridge)
        ev = _make_event("verification_completed")
        posthog_bridge.capture_event(ev)

    mock_posthog.capture.assert_called_once()
    call_kwargs = mock_posthog.capture.call_args
    assert call_kwargs.kwargs["event"] == "verification_completed"


# ---------------------------------------------------------------------------
# Test 2 — Non-allowlist event is silently skipped
# ---------------------------------------------------------------------------

def test_non_allowlist_event_skipped(monkeypatch):
    """capture_event does not call posthog.capture for events not on the allowlist."""
    mock_posthog = MagicMock()
    monkeypatch.setattr("app.core.config.settings.OBSERVABILITY_ENABLED", True)
    monkeypatch.setattr("app.core.config.settings.POSTHOG_API_KEY", "phc_test")
    monkeypatch.setattr(
        "app.core.config.settings.OBSERVABILITY_POSTHOG_ALLOWLIST",
        "verification_completed",
    )

    with patch.dict(sys.modules, {"posthog": mock_posthog}):
        from app.services.observability import posthog_bridge
        importlib.reload(posthog_bridge)
        ev = _make_event("application_created")   # not on allowlist
        posthog_bridge.capture_event(ev)

    mock_posthog.capture.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3 — PII-safe payload shape
# ---------------------------------------------------------------------------

def test_pii_safe_payload(monkeypatch):
    """
    Properties forwarded to PostHog must not contain raw application_id,
    patient_id, email, phone, or rich_payload contents.
    """
    mock_posthog = MagicMock()
    monkeypatch.setattr("app.core.config.settings.OBSERVABILITY_ENABLED", True)
    monkeypatch.setattr("app.core.config.settings.POSTHOG_API_KEY", "phc_test")
    monkeypatch.setattr(
        "app.core.config.settings.OBSERVABILITY_POSTHOG_ALLOWLIST",
        "decision_made",
    )

    raw_app_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    raw_patient_id = "ffffffff-0000-1111-2222-333333333333"
    payload = {
        "decision": "approved",
        "rich_payload": {"email": "test@example.com", "phone": "+15550001234"},
    }

    with patch.dict(sys.modules, {"posthog": mock_posthog}):
        from app.services.observability import posthog_bridge
        importlib.reload(posthog_bridge)
        ev = _make_event("decision_made", application_id=raw_app_id, payload=payload)
        ev.patient_id = raw_patient_id
        posthog_bridge.capture_event(ev)

    mock_posthog.capture.assert_called_once()
    props = mock_posthog.capture.call_args.kwargs["properties"]
    distinct_id = mock_posthog.capture.call_args.kwargs["distinct_id"]

    # Raw UUIDs must not appear anywhere
    assert raw_app_id not in str(props)
    assert raw_patient_id not in str(props)
    assert raw_app_id not in distinct_id

    # PII from rich_payload must not appear
    assert "test@example.com" not in str(props)
    assert "+15550001234" not in str(props)

    # distinct_id must be a 16-char hex hash
    assert len(distinct_id) == 16
    assert all(c in "0123456789abcdef" for c in distinct_id)

    # Legitimate metadata is present
    assert props["decision"] == "approved"


# ---------------------------------------------------------------------------
# Test 4 — OBSERVABILITY_ENABLED=False is a complete no-op
# ---------------------------------------------------------------------------

def test_disabled_is_noop(monkeypatch):
    """No posthog import or network call when OBSERVABILITY_ENABLED=False."""
    mock_posthog = MagicMock()
    monkeypatch.setattr("app.core.config.settings.OBSERVABILITY_ENABLED", False)
    monkeypatch.setattr("app.core.config.settings.POSTHOG_API_KEY", "phc_test")

    with patch.dict(sys.modules, {"posthog": mock_posthog}):
        from app.services.observability import posthog_bridge
        importlib.reload(posthog_bridge)
        ev = _make_event("verification_completed")
        posthog_bridge.capture_event(ev)   # must not raise

    mock_posthog.capture.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5 — posthog import failure does not break event emission
# ---------------------------------------------------------------------------

def test_posthog_import_failure_is_silent(monkeypatch):
    """
    If posthog is not installed, capture_event swallows the ImportError
    and does not propagate it to the caller.
    """
    monkeypatch.setattr("app.core.config.settings.OBSERVABILITY_ENABLED", True)
    monkeypatch.setattr("app.core.config.settings.POSTHOG_API_KEY", "phc_test")
    monkeypatch.setattr(
        "app.core.config.settings.OBSERVABILITY_POSTHOG_ALLOWLIST",
        "verification_completed",
    )

    # Remove posthog from sys.modules to simulate it not being installed
    with patch.dict(sys.modules, {"posthog": None}):
        from app.services.observability import posthog_bridge
        importlib.reload(posthog_bridge)
        ev = _make_event("verification_completed")
        # Must not raise — PostHog absence must never break callers
        posthog_bridge.capture_event(ev)


# ---------------------------------------------------------------------------
# Test 6 — Allowlist is read from config, not hardcoded
# ---------------------------------------------------------------------------

def test_allowlist_from_config_not_hardcoded(monkeypatch):
    """
    Changing OBSERVABILITY_POSTHOG_ALLOWLIST at runtime changes which events
    are captured — proves the allowlist is not baked in at import time.
    """
    mock_posthog = MagicMock()
    monkeypatch.setattr("app.core.config.settings.OBSERVABILITY_ENABLED", True)
    monkeypatch.setattr("app.core.config.settings.POSTHOG_API_KEY", "phc_test")

    with patch.dict(sys.modules, {"posthog": mock_posthog}):
        from app.services.observability import posthog_bridge
        importlib.reload(posthog_bridge)

        # First: allowlist contains only "decision_made"
        monkeypatch.setattr(
            "app.core.config.settings.OBSERVABILITY_POSTHOG_ALLOWLIST",
            "decision_made",
        )
        ev_not_allowed = _make_event("magic_link_issued")
        posthog_bridge.capture_event(ev_not_allowed)
        mock_posthog.capture.assert_not_called()

        # Now: expand allowlist to include "magic_link_issued"
        monkeypatch.setattr(
            "app.core.config.settings.OBSERVABILITY_POSTHOG_ALLOWLIST",
            "decision_made,magic_link_issued",
        )
        ev_allowed = _make_event("magic_link_issued")
        posthog_bridge.capture_event(ev_allowed)
        mock_posthog.capture.assert_called_once()
