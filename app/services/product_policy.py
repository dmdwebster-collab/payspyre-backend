"""Product-policy accessor (Wave 2 W2-APPCONFIG).

Thin, fallible-safe reader over ``platform_credit_products.policy_config``.
A NULL / empty / unparseable blob → :data:`DEFAULT_PRODUCT_POLICY_CONFIG` (the
engine's current behaviour), so servicing/quote code can always ask for a
product's policy without special-casing legacy rows. Validation on WRITE lives
in the product CRUD path (``credit_products`` service); this module never
raises for a read.
"""
from __future__ import annotations

from typing import Any, Optional

from app.core.logging import get_logger
from app.schemas.product_policy_config import (
    DEFAULT_PRODUCT_POLICY_CONFIG,
    ProductPolicyConfig,
    ProductPolicyConfigError,
    parse_product_policy_config,
    payoff_policy_descriptor,
)

logger = get_logger(__name__)


def policy_for_product(product: Any) -> ProductPolicyConfig:
    """The effective policy config for a product row (or the shipped default).

    ``product`` may be ``None`` (loan with no product resolved) → default."""
    if product is None:
        return DEFAULT_PRODUCT_POLICY_CONFIG
    raw = getattr(product, "policy_config", None)
    if not raw:
        return DEFAULT_PRODUCT_POLICY_CONFIG
    try:
        return parse_product_policy_config(raw, context="product-row")
    except ProductPolicyConfigError:
        logger.error(
            "stored policy_config is invalid for product %s; using defaults",
            getattr(product, "code", "?"),
        )
        return DEFAULT_PRODUCT_POLICY_CONFIG


def payoff_descriptor_for_product(product: Any) -> Optional[dict]:
    """The payoff-policy descriptor to attach to a quote, or None when unset.

    Returns None when the product has no configured policy (keeps the quote
    payload byte-identical to the pre-workstream response for legacy rows)."""
    if product is None or not getattr(product, "policy_config", None):
        return None
    return payoff_policy_descriptor(policy_for_product(product))
