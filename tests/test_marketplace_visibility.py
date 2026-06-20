"""Unit tests for marketplace lead visibility (no DB)."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.services.marketplace.visibility import is_visible_to_vendors, vendor_view

_CREATED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_EXPIRES = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _listing(**overrides):
    base = dict(
        id="11111111-2222-3333-4444-555555555555",
        treatment_categories=["implants"],
        treatment_urgency="immediate",
        location_postal_code="M5V 2T6",
        max_travel_km=25,
        lead_state="unqualified",
        verification_depth="id_verified",
        estimated_budget_cents=750000,
        created_at=_CREATED,
        expires_at=_EXPIRES,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_no_pii_keys_present():
    view = vendor_view(_listing())
    forbidden = {
        "name", "patient_name", "first_name", "last_name",
        "email", "phone", "address", "location_postal_code", "postal_code",
        "patient_id",
    }
    assert forbidden.isdisjoint(view.keys())


def test_fsa_derivation():
    view = vendor_view(_listing(location_postal_code="M5V 2T6"))
    assert view["fsa"] == "M5V"
    # lowercase + no space variants normalise the same way
    assert vendor_view(_listing(location_postal_code="m5v2t6"))["fsa"] == "M5V"


def test_core_projection_fields():
    view = vendor_view(_listing())
    assert view["listing_id"] == "11111111-2222-3333-4444-555555555555"
    assert view["treatment_categories"] == ["implants"]
    assert view["treatment_urgency"] == "immediate"
    assert view["max_travel_km"] == 25
    assert view["lead_state"] == "unqualified"
    assert view["created_at"] == _CREATED.isoformat()
    assert view["expires_at"] == _EXPIRES.isoformat()


def test_budget_omitted_for_unqualified():
    # show_estimated_budget: false -> key omitted entirely
    view = vendor_view(_listing(lead_state="unqualified", estimated_budget_cents=750000))
    assert "estimated_budget" not in view


def test_budget_range_for_pre_qualified():
    # show_estimated_budget: range -> $5k-wide bucket
    view = vendor_view(_listing(lead_state="pre_qualified", estimated_budget_cents=750000))
    assert view["estimated_budget"] == "$5,000–$10,000"


def test_budget_range_omitted_when_none():
    view = vendor_view(_listing(lead_state="pre_qualified", estimated_budget_cents=None))
    assert "estimated_budget" not in view


def test_budget_exact_for_pre_approved():
    # show_estimated_budget: exact -> raw cents int
    view = vendor_view(_listing(lead_state="pre_approved", estimated_budget_cents=750000))
    assert view["estimated_budget"] == 750000
    assert isinstance(view["estimated_budget"], int)


def test_budget_exact_omitted_when_none():
    view = vendor_view(_listing(lead_state="pre_approved", estimated_budget_cents=None))
    assert "estimated_budget" not in view


def test_verification_depth_present_when_shown():
    # unqualified has show_verification_depth: true in the current config
    view = vendor_view(_listing(lead_state="unqualified"))
    assert view["verification_depth"] == "id_verified"


def test_verification_depth_omitted_branch():
    # An unknown lead_state has no config rules -> show_verification_depth absent/False
    # -> the omission branch is exercised.
    view = vendor_view(_listing(lead_state="mystery_state"))
    assert "verification_depth" not in view


def test_priority_placement_reflects_config():
    assert vendor_view(_listing(lead_state="unqualified"))["priority_placement"] is False
    assert vendor_view(_listing(lead_state="pre_approved"))["priority_placement"] is True


def test_is_visible_to_vendors():
    assert is_visible_to_vendors("unqualified") is True
    assert is_visible_to_vendors("declined") is False
    assert is_visible_to_vendors("unknown_state") is False
