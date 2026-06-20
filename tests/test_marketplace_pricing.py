"""Unit tests for marketplace lead pricing (no DB)."""
from __future__ import annotations

import pytest

from app.services.marketplace.pricing import get_charge_trigger, price_lead


def test_base_price_per_lead_state():
    # No category, urgency unknown->1.0, verification id_verified->1.0
    assert price_lead(
        lead_state="unqualified",
        treatment_categories=[],
        urgency="this_month",
        verification_depth="id_verified",
    ) == 1500
    assert price_lead(
        lead_state="pre_qualified",
        treatment_categories=[],
        urgency="this_month",
        verification_depth="id_verified",
    ) == 4000
    assert price_lead(
        lead_state="pre_approved",
        treatment_categories=[],
        urgency="this_month",
        verification_depth="id_verified",
    ) == 7500
    assert price_lead(
        lead_state="approved",
        treatment_categories=[],
        urgency="this_month",
        verification_depth="id_verified",
    ) == 15000


def test_category_multiplier_dimension():
    # 1500 * 1.5 = 2250
    assert price_lead(
        lead_state="unqualified",
        treatment_categories=["implants"],
        urgency="this_month",
        verification_depth="id_verified",
    ) == 2250


def test_urgency_multiplier_dimension():
    # 1500 * 1.4 = 2100
    assert price_lead(
        lead_state="unqualified",
        treatment_categories=[],
        urgency="immediate",
        verification_depth="id_verified",
    ) == 2100
    # 1500 * 0.8 = 1200
    assert price_lead(
        lead_state="unqualified",
        treatment_categories=[],
        urgency="flexible",
        verification_depth="id_verified",
    ) == 1200


def test_verification_depth_multiplier_dimension():
    # 1500 * 0.5 = 750
    assert price_lead(
        lead_state="unqualified",
        treatment_categories=[],
        urgency="this_month",
        verification_depth="none",
    ) == 750
    # 1500 * 1.5 = 2250
    assert price_lead(
        lead_state="unqualified",
        treatment_categories=[],
        urgency="this_month",
        verification_depth="id_bank_cb_verified",
    ) == 2250


def test_multi_category_uses_max_multiplier():
    # full_arch (2.5) beats orthodontics (1.3) and general_dentistry (1.0)
    # 1500 * 2.5 = 3750
    assert price_lead(
        lead_state="unqualified",
        treatment_categories=["general_dentistry", "full_arch", "orthodontics"],
        urgency="this_month",
        verification_depth="id_verified",
    ) == 3750


def test_unknown_keys_default_to_one():
    # Unknown category, urgency, and verification_depth all default to 1.0 -> base price.
    assert price_lead(
        lead_state="unqualified",
        treatment_categories=["nonexistent_category"],
        urgency="someday",
        verification_depth="quantum_verified",
    ) == 1500


def test_combined_multipliers_exact_cents():
    # pre_approved + implants + immediate + id_bank_verified
    # round(7500 * 1.5 * 1.4 * 1.3) = 20475
    assert price_lead(
        lead_state="pre_approved",
        treatment_categories=["implants"],
        urgency="immediate",
        verification_depth="id_bank_verified",
    ) == 20475


def test_unknown_lead_state_raises():
    with pytest.raises(ValueError):
        price_lead(
            lead_state="bogus_state",
            treatment_categories=[],
            urgency="this_month",
            verification_depth="id_verified",
        )


def test_charge_trigger_returns_config_value():
    assert get_charge_trigger() == "patient_selected"
