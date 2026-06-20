"""Marketplace lead listing visibility rules (spec §6.4).

Produces de-identified, vendor-safe projections of marketplace listings. PII
(patient name/email/phone, exact postal/address) must never appear in the
vendor view; only the postal FSA (first 3 chars) is exposed. Visibility and
budget-disclosure behaviour are driven by
``config/marketplace/visibility.yaml``.
"""
from __future__ import annotations

from pathlib import Path

import yaml

_CONFIG_PATH = (
    Path(__file__).resolve().parents[3]
    / "config"
    / "marketplace"
    / "visibility.yaml"
)
with _CONFIG_PATH.open() as _f:
    _VISIBILITY_CONFIG: dict = yaml.safe_load(_f)

_DEFAULT_VISIBILITY: dict[str, dict] = _VISIBILITY_CONFIG.get("default_visibility", {})

# Budget buckets are $5,000 wide.
_BUDGET_BUCKET_CENTS = 5_000_00


def is_visible_to_vendors(lead_state: str) -> bool:
    """Return default_visibility[lead_state].visible_to_clinics, default False if absent."""
    rules = _DEFAULT_VISIBILITY.get(lead_state)
    if rules is None:
        return False
    return bool(rules.get("visible_to_clinics", False))


def _fsa(postal_code: str | None) -> str:
    """Return the forward sortation area (first 3 chars), uppercased, spaces stripped."""
    if not postal_code:
        return ""
    return postal_code.replace(" ", "").upper()[:3]


def _format_cents(cents: int) -> str:
    """Format integer cents as a whole-dollar string, e.g. 500000 -> '$5,000'."""
    return f"${cents // 100:,}"


def _budget_range(budget_cents: int) -> str:
    """Bucket a budget into a $5,000-wide human range string, e.g. '$5,000–$10,000'."""
    lower = (budget_cents // _BUDGET_BUCKET_CENTS) * _BUDGET_BUCKET_CENTS
    upper = lower + _BUDGET_BUCKET_CENTS
    return f"{_format_cents(lower)}–{_format_cents(upper)}"


def _iso(value) -> str | None:
    """Render a datetime (or anything with isoformat) as an ISO string, passing None/str through."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


def vendor_view(listing) -> dict:
    """De-identified, vendor-safe projection of a PlatformMarketplaceListing (spec §6.4).

    PII (patient name/email/phone, exact postal/address) MUST NEVER appear.
    Returns a dict with keys:
      listing_id (str), treatment_categories (list[str]), treatment_urgency (str),
      fsa (str = first 3 chars of location_postal_code, uppercased, spaces stripped),
      max_travel_km (int), lead_state (str), created_at (iso str), expires_at (iso str),
      priority_placement (bool from config),
      verification_depth (str)  -- ONLY if show_verification_depth is true, else omit the key,
      estimated_budget (str|int|None) -- per show_estimated_budget:
          false  -> omit the key entirely
          'range' -> a bucketed human range string derived from estimated_budget_cents,
                     e.g. '$5,000–$10,000' ($5k-wide buckets; if budget is None -> omit key)
          'exact' -> the exact estimated_budget_cents int (omit key if None)
    """
    lead_state = listing.lead_state
    rules = _DEFAULT_VISIBILITY.get(lead_state, {})

    view: dict = {
        "listing_id": str(listing.id),
        "treatment_categories": list(listing.treatment_categories or []),
        "treatment_urgency": listing.treatment_urgency,
        "fsa": _fsa(listing.location_postal_code),
        "max_travel_km": listing.max_travel_km,
        "lead_state": lead_state,
        "created_at": _iso(listing.created_at),
        "expires_at": _iso(listing.expires_at),
        "priority_placement": bool(rules.get("priority_placement", False)),
    }

    if rules.get("show_verification_depth", False):
        view["verification_depth"] = listing.verification_depth

    show_budget = rules.get("show_estimated_budget", False)
    budget_cents = listing.estimated_budget_cents
    if show_budget == "range" and budget_cents is not None:
        view["estimated_budget"] = _budget_range(budget_cents)
    elif show_budget == "exact" and budget_cents is not None:
        view["estimated_budget"] = budget_cents

    return view
