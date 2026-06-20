"""Marketplace lead pricing (spec §6.2).

Computes the base price (in integer cents) for a marketplace lead from the
rules in ``config/marketplace/lead_pricing.yaml``. All money values are
integer cents; floats are only used transiently for the multiplier arithmetic
and the result is rounded to the nearest cent.
"""
from __future__ import annotations

from pathlib import Path

import yaml

_CONFIG_PATH = (
    Path(__file__).resolve().parents[3]
    / "config"
    / "marketplace"
    / "lead_pricing.yaml"
)
with _CONFIG_PATH.open() as _f:
    _PRICING_CONFIG: dict = yaml.safe_load(_f)

_BASE_PRICES: dict[str, int] = _PRICING_CONFIG["base_prices"]
_MULTIPLIERS: dict[str, dict[str, float]] = _PRICING_CONFIG.get("multipliers", {})
_CATEGORY_MULTIPLIERS: dict[str, float] = _MULTIPLIERS.get("treatment_category", {})
_URGENCY_MULTIPLIERS: dict[str, float] = _MULTIPLIERS.get("urgency", {})
_VERIFICATION_MULTIPLIERS: dict[str, float] = _MULTIPLIERS.get("verification_depth", {})


def price_lead(
    *,
    lead_state: str,
    treatment_categories: list[str],
    urgency: str,
    verification_depth: str,
) -> int:
    """Return the lead's base price in INTEGER CENTS per config/marketplace/lead_pricing.yaml.

    price = base_prices[lead_state]
            * max(treatment_category multiplier across the listing's categories)
            * urgency multiplier
            * verification_depth multiplier

    When a listing carries multiple treatment categories we use the MAX category
    multiplier — a lead is priced by its most valuable treatment (e.g. a listing
    tagged both ``general_dentistry`` and ``full_arch`` is priced as a full-arch
    lead). Any category/urgency/verification_depth not present in the multipliers
    map defaults to a 1.0 multiplier. If ``treatment_categories`` is empty the
    category multiplier is 1.0. If ``lead_state`` is not in base_prices a
    ValueError is raised. The float result is rounded to the nearest integer
    cent (round(), banker's rounding).
    """
    if lead_state not in _BASE_PRICES:
        raise ValueError(f"Unknown lead_state: {lead_state!r}")

    base = _BASE_PRICES[lead_state]

    if treatment_categories:
        category_multiplier = max(
            _CATEGORY_MULTIPLIERS.get(category, 1.0)
            for category in treatment_categories
        )
    else:
        category_multiplier = 1.0

    urgency_multiplier = _URGENCY_MULTIPLIERS.get(urgency, 1.0)
    verification_multiplier = _VERIFICATION_MULTIPLIERS.get(verification_depth, 1.0)

    price = base * category_multiplier * urgency_multiplier * verification_multiplier
    return round(price)


def get_charge_trigger() -> str:
    """Return the configured ``charge_trigger`` string from lead_pricing.yaml."""
    return _PRICING_CONFIG["charge_trigger"]
