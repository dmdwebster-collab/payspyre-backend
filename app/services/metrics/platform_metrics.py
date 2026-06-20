"""Platform Prometheus metrics — spec §11 ("KPIs to instrument"), audit H-8.

Module-level ``Counter`` / ``Histogram`` objects for the §11 KPI table, plus
thin, *never-raising* helper functions that the orchestrator (and other event
sources) call to record each event. Scraped via the ``/metrics`` endpoint in
``app/api/metrics.py``.

PII discipline (Hard Rule #7 — non-negotiable):

- Labels MUST be LOW-CARDINALITY and PII-FREE. Allowed: credit-product *code*,
  vendor (adapter name / vendor_id), status, decision, verification *type*,
  consent purpose, lead/marketplace state + category. NEVER: patient/application
  ids, names, emails, phone numbers, DOBs, document numbers, free-form vendor
  error strings.
- ``vendor_id`` in §11 is the clinic/practice ``vendors.id`` UUID. A raw UUID is
  unbounded-cardinality and would blow up Prometheus, so the helpers DO NOT put
  it on a label by default — ``record_application_started`` accepts it only to
  mirror the spec's column and deliberately drops it from the label set. If a
  bounded vendor *channel*/*name* is ever wanted, pass that, not the UUID.
- Monetary / cost fields from the spec table (``cost_cents``, ``amount_cents``,
  ``charge_trigger`` amounts) are recorded as Histogram *observations* (values),
  never as labels.

Every helper is wrapped in ``try/except`` → ``logger.warning`` and returns
``None``. Instrumentation must never break the credit flow.
"""
from __future__ import annotations

import logging

from prometheus_client import Counter, Histogram

logger = logging.getLogger(__name__)


def _s(value: object, default: str = "unknown") -> str:
    """Coerce a label value to a safe, bounded string.

    ``None`` / empty → ``default``. Anything else → ``str(value)`` truncated to
    64 chars (defence-in-depth against an unexpectedly long/unbounded value
    sneaking onto a label and exploding Prometheus cardinality). Booleans are
    normalised to ``"true"``/``"false"``.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip()
    if not text:
        return default
    return text[:64]


# ---------------------------------------------------------------------------
# §11 metric definitions — names use the spec's dotted KPI identifiers mapped
# to Prometheus-legal underscored names (``.`` is not a legal metric char).
# ---------------------------------------------------------------------------

application_started_total = Counter(
    "application_started_total",
    "Applications created (spec §11 application.started).",
    ["credit_product_code", "vertical"],
)

application_step_completed_total = Counter(
    "application_step_completed_total",
    "Application steps completed (spec §11 application.step_completed).",
    ["credit_product_code", "step_type", "result"],
)

application_completed_total = Counter(
    "application_completed_total",
    "Applications decisioned (spec §11 application.completed).",
    ["credit_product_code", "decision"],
)

verification_completed_total = Counter(
    "verification_completed_total",
    "Verifications completed (spec §11 verification.completed).",
    ["type", "vendor", "status"],
)

# cost_cents is a value, not a label (PII/cardinality) — Histogram observation.
verification_cost_cents = Histogram(
    "verification_cost_cents",
    "Per-verification vendor cost in cents (spec §11 verification.completed.cost_cents).",
    ["type", "vendor"],
    buckets=(0, 25, 50, 100, 200, 400, 800, 1600, 3200, 6400),
)

consent_granted_total = Counter(
    "consent_granted_total",
    "Consents granted (spec §11 consent.granted).",
    ["purpose"],
)

consent_revoked_total = Counter(
    "consent_revoked_total",
    "Consents revoked (spec §11 consent.revoked).",
    ["purpose"],
)

lead_state_changed_total = Counter(
    "lead_state_changed_total",
    "Lead state transitions (spec §11 lead_state_changed).",
    ["from_state", "to_state"],
)

marketplace_listing_created_total = Counter(
    "marketplace_listing_created_total",
    "Marketplace listings created (spec §11 marketplace.listing_created).",
    ["treatment_category", "lead_state"],
)

marketplace_vendor_interest_total = Counter(
    "marketplace_vendor_interest_total",
    "Marketplace vendor interest events (spec §11 marketplace.vendor_interest).",
    ["treatment_category"],
)

marketplace_lead_charged_total = Counter(
    "marketplace_lead_charged_total",
    "Marketplace lead charges (spec §11 marketplace.lead_charged).",
    ["charge_trigger"],
)

# amount_cents is a value, not a label — Histogram observation.
marketplace_lead_charge_amount_cents = Histogram(
    "marketplace_lead_charge_amount_cents",
    "Marketplace lead charge amount in cents (spec §11 marketplace.lead_charged.amount_cents).",
    ["charge_trigger"],
    buckets=(0, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000),
)


# ---------------------------------------------------------------------------
# Safe helpers — never raise. Call these from the orchestrator / event sources.
# ---------------------------------------------------------------------------


def record_application_started(
    product_code: object,
    vendor_id: object = None,  # accepted to mirror §11; intentionally NOT labelled (UUID = unbounded)
    vertical: object = None,
) -> None:
    """Record application.started.

    ``vendor_id`` (clinic ``vendors.id`` UUID) is accepted but deliberately
    dropped from the label set — a raw UUID is unbounded-cardinality PII-adjacent
    data and must not become a Prometheus label (Hard Rule #7).
    """
    try:
        application_started_total.labels(
            credit_product_code=_s(product_code),
            vertical=_s(vertical),
        ).inc()
    except Exception:  # noqa: BLE001
        logger.warning("metrics.record_application_started failed", exc_info=True)


def record_step_completed(
    product_code: object,
    step_type: object,
    result: object,
) -> None:
    """Record application.step_completed."""
    try:
        application_step_completed_total.labels(
            credit_product_code=_s(product_code),
            step_type=_s(step_type),
            result=_s(result),
        ).inc()
    except Exception:  # noqa: BLE001
        logger.warning("metrics.record_step_completed failed", exc_info=True)


def record_decision(product_code: object, decision: object) -> None:
    """Record application.completed with its decision."""
    try:
        application_completed_total.labels(
            credit_product_code=_s(product_code),
            decision=_s(decision),
        ).inc()
    except Exception:  # noqa: BLE001
        logger.warning("metrics.record_decision failed", exc_info=True)


def record_verification_completed(
    type: object,  # noqa: A002 — matches the §11 tag name
    vendor: object,
    status: object,
    cost_cents: object = None,
) -> None:
    """Record verification.completed (+ optional cost_cents as a histogram value)."""
    try:
        vtype = _s(type)
        vend = _s(vendor)
        verification_completed_total.labels(
            type=vtype,
            vendor=vend,
            status=_s(status),
        ).inc()
        if cost_cents is not None:
            try:
                verification_cost_cents.labels(type=vtype, vendor=vend).observe(
                    float(cost_cents)
                )
            except (TypeError, ValueError):
                pass  # non-numeric cost → skip the observation, keep the counter
    except Exception:  # noqa: BLE001
        logger.warning("metrics.record_verification_completed failed", exc_info=True)


def record_consent(purpose: object, granted: object) -> None:
    """Record consent.granted or consent.revoked, keyed on ``granted``."""
    try:
        purp = _s(purpose)
        if granted:
            consent_granted_total.labels(purpose=purp).inc()
        else:
            consent_revoked_total.labels(purpose=purp).inc()
    except Exception:  # noqa: BLE001
        logger.warning("metrics.record_consent failed", exc_info=True)


def record_lead_state_changed(from_state: object, to_state: object) -> None:
    """Record lead_state_changed."""
    try:
        lead_state_changed_total.labels(
            from_state=_s(from_state),
            to_state=_s(to_state),
        ).inc()
    except Exception:  # noqa: BLE001
        logger.warning("metrics.record_lead_state_changed failed", exc_info=True)


def record_marketplace_listing_created(
    treatment_category: object, lead_state: object
) -> None:
    """Record marketplace.listing_created."""
    try:
        marketplace_listing_created_total.labels(
            treatment_category=_s(treatment_category),
            lead_state=_s(lead_state),
        ).inc()
    except Exception:  # noqa: BLE001
        logger.warning("metrics.record_marketplace_listing_created failed", exc_info=True)


def record_marketplace_vendor_interest(
    vendor_id: object = None,  # §11 column; UUID intentionally NOT labelled
    treatment_category: object = None,
) -> None:
    """Record marketplace.vendor_interest.

    ``vendor_id`` accepted to mirror §11 but dropped from labels (unbounded UUID).
    """
    try:
        marketplace_vendor_interest_total.labels(
            treatment_category=_s(treatment_category),
        ).inc()
    except Exception:  # noqa: BLE001
        logger.warning("metrics.record_marketplace_vendor_interest failed", exc_info=True)


def record_marketplace_lead_charged(
    charge_trigger: object,
    amount_cents: object = None,
    vendor_id: object = None,  # §11 column; UUID intentionally NOT labelled
) -> None:
    """Record marketplace.lead_charged (+ optional amount_cents as histogram value)."""
    try:
        trig = _s(charge_trigger)
        marketplace_lead_charged_total.labels(charge_trigger=trig).inc()
        if amount_cents is not None:
            try:
                marketplace_lead_charge_amount_cents.labels(charge_trigger=trig).observe(
                    float(amount_cents)
                )
            except (TypeError, ValueError):
                pass
    except Exception:  # noqa: BLE001
        logger.warning("metrics.record_marketplace_lead_charged failed", exc_info=True)
