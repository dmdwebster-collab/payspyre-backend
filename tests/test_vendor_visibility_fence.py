"""Vendor data-exposure REGRESSION FENCE (WS-I) — clinic/v1 model snapshots.

Dave's tab-by-tab visibility rules (docs/turnkey_parity/10__Vendor_Access.md §2/§5)
say vendors must NEVER receive: risk scores, credit-bureau data, bank
statements / bank details (account, routing, transit, institution numbers),
hardship/rescheduling data, scheduled-transaction internals, borrower
contact-log/comms, or SIN. This module makes that rule STRUCTURAL:

1. It walks every module in the ``app.api.clinic.v1`` package (pkgutil, plain
   imports) and collects every Pydantic model present in a clinic module
   namespace whose home package is ``app.api.*`` — i.e. every model the clinic
   surface can declare as a request or response schema — recursing into nested
   models and generic containers.
2. It asserts no field NAME matches the forbidden vocabulary — the data cannot
   be present even as ``null`` because the field does not exist.
3. It snapshots the exact field set of every collected model. ANY new field on
   the vendor surface fails this test and forces a conscious review against
   Dave's rules before the snapshot is updated.

DISCOVERY IS HERMETIC BY DESIGN (do not "simplify" it back to route walking):
the first version of this fence walked ``clinic_router.routes`` and read each
route's ``response_model``. That is instantiation-state and version dependent —
FastAPI 0.139 changed ``include_router`` to LAZY inclusion, so a parent
router's ``.routes`` holds ``_IncludedRouter`` wrappers (no ``path``, no
``response_model``) and the walk silently discovered NOTHING in CI (which
resolves the latest FastAPI) while passing locally on an older pin. Module-walk
discovery depends only on Python imports, so it finds the same models under
every FastAPI/starlette version and every env-flag combination. Its one blind
spot: a schema referenced ONLY via attribute access in a decorator (e.g.
``response_model=some_module.Foo``) without importing ``Foo`` into the module
namespace — don't do that in clinic endpoints.

Out of static reach (covered elsewhere / by convention):
* ``dict``-typed dashboard blocks (``VendorOverview.window/applications/...``)
  — their key contracts are fixed by the block builders in
  ``dashboard_applications`` / ``dashboard_loanbook`` / ``dashboard_marketplace``
  (stable-contract docstrings + module tests) and contain aggregates only.
* ``GET /marketplace/leads`` returns the marketplace ``vendor_view`` projection
  (de-identified, PII-free by design — see app/services/marketplace tests).
* The dev-only ``endpoints/dev_tools.py`` module is excluded: it is never
  mounted in production (router guard) and returns seeding credentials, not
  borrower data.

Run (only this file — the full suite hits a shared remote DB):
    python -m pytest tests/test_vendor_visibility_fence.py -p no:warnings -q
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import get_args, get_origin

from pydantic import BaseModel

import app.api.clinic.v1 as clinic_pkg

# ---------------------------------------------------------------------------
# Forbidden vocabulary (Dave's never-list). Matching is TOKEN-based on the
# snake_case field name so e.g. "housing" does not false-positive on "sin".
# ---------------------------------------------------------------------------

FORBIDDEN_TOKENS = {
    # SIN / identifiers
    "sin", "ssn",
    # proprietary risk + bureau
    "risk", "score", "bureau", "equifax", "transunion",
    # bank details / statements / payment-rail internals
    "bank", "account", "routing", "transit", "institution", "iban",
    "statement", "statements", "flinks", "zumrails",
    # hardship / rescheduling / scheduled-transaction internals
    "hardship", "reschedule", "rescheduling", "suspension", "suspended",
    # borrower comms / contact log
    "comms", "communication", "communications", "log",
    # raw decision internals (vendors get the mapped status only)
    "decision", "reasons",
}

# Fields whose name trips a token but whose content is explicitly vendor-safe.
# Each entry needs a justification.
ALLOWLIST: set[tuple[str, str]] = {
    # The vendor's OWN compliance score on their OWN profile — data about the
    # vendor, not about any borrower (10__: risk scores are borrower-side).
    ("VendorProfile", "compliance_score"),
}

# Dev-only modules, never mounted in production (router guard); their schemas
# return seeding credentials, not borrower data.
EXCLUDED_MODULES = {"app.api.clinic.v1.endpoints.dev_tools"}


# ---------------------------------------------------------------------------
# The snapshot: EVERY clinic/v1 request/response model and its exact field
# set. Adding/renaming a field on the vendor surface MUST update this dict —
# that update is the review checkpoint against Dave's visibility rules.
# ---------------------------------------------------------------------------

EXPECTED_MODEL_FIELDS: dict[str, list[str]] = {
    # products / applications / financing links
    "ClinicProduct": [
        "code", "currency", "id", "max_amount_cents", "min_amount_cents",
        "name", "vertical",
    ],
    "ClinicApplication": [
        "amount_cents", "created_at", "currency", "id", "patient_contact",
        "patient_name", "product_name", "status",
    ],
    "ClinicDashboardSummary": [
        "approved", "declined", "manual_review", "started", "total",
    ],
    "CreateFinancingLinkBody": [
        "amount_cents", "credit_product_id", "patient_contact", "patient_name",
    ],
    "ClinicFinancingLink": [
        "amount_cents", "application_ref", "patient_name", "product_name", "url",
    ],
    # WS-I vendor origination
    "VendorApplicationIntakeBody": [
        "additional_notes", "alt_contact_name", "alt_contact_relationship",
        "amount_financed_cents", "credit_product_id", "down_payment_cents",
        "first_due_date", "insurance_coverage_cents", "loan_start_date",
        "patient_contact", "patient_name", "preferred_first_due_date",
        "preferred_payment_amount_cents", "preferred_payment_frequency",
        "provider_name", "province", "requested_annual_rate_bps",
        "term_months", "treatment_cost_cents",
    ],
    "VendorApplicationCreated": [
        "amount_financed_cents", "application_id", "patient_flow_url",
        "patient_name", "product_name", "status", "verification_channel",
        "verification_message",
    ],
    "PreviewRequestBody": [
        "amount_cents", "annual_rate_bps", "credit_product_id", "frequency",
        "term_months",
    ],
    "VendorPaymentPreview": [
        "amount_cents", "annual_rate_bps", "apr_bps", "commission_cents",
        "commission_note", "fee_lines", "fees_cents", "final_installment_cents",
        "frequency", "frequency_label", "installment_cents", "interest_cents",
        "num_payments", "principal_cents", "schedule", "term_months",
        "total_of_payments_cents",
    ],
    "PreviewFeeLine": ["amount", "calc", "charge_timing", "fee_type"],
    "PreviewScheduleRow": [
        "balance_cents", "interest_cents", "number", "payment_cents",
        "principal_cents",
    ],
    "VendorReprocessingResult": [
        "application_id", "reprocessing_requested", "status",
    ],
    # dashboards
    "VendorOverview": [
        "applications", "loan_book", "marketplace", "payments", "vendor", "window",
    ],
    "OverviewVendor": ["business_name", "id", "status"],
    "AppTimeseries": ["granularity", "points"],
    "AppTimeseriesPoint": [
        "approved", "bucket", "declined", "in_review",
        "requested_amount_cents", "started",
    ],
    "VendorLoanBook": ["next_cursor", "rows"],
    "VendorLoanRow": [
        "application_id", "days_past_due", "disbursed_at", "disbursement_status",
        "loan_id", "next_due_cents", "next_due_date", "patient_name",
        "principal_balance_cents", "principal_cents", "status",
    ],
    "VendorFunnel": [
        "appointment_booked", "approved", "charged", "disbursed",
        "interest_expressed", "leads_viewed", "pre_qualified", "started",
        "verifying",
    ],
    "VendorRevenue": [
        "by_trigger", "charge_count", "timeseries", "total_charges_cents",
    ],
    "RevenuePoint": ["bucket", "charge_count", "charges_cents"],
    # account / marketplace billing
    "VendorProfile": [
        "address", "business_name", "business_type", "compliance_score",
        "contact_name", "dba_name", "email", "id", "license_expiry",
        "license_number", "phone", "status",
    ],
    "VendorAddress": ["city", "line1", "line2", "postal_code", "province"],
    "ProfileChangeRequestBody": [
        "address_line1", "address_line2", "city", "contact_name", "email",
        "note", "phone", "postal_code", "province",
    ],
    "ProfileChangeRequest": [
        "created_at", "id", "note", "requested_changes", "status", "vendor_id",
    ],
    "VendorBillingEntry": [
        "charge_trigger", "lead_charge_cents", "lead_charged_at", "listing_id",
    ],
    # W2-DISB vendor self-serve disbursements (video 10). Reviewed against the
    # 10__Vendor_Access.md §2 never-list: these expose only the vendor's OWN
    # money position (MTD collected/due/available/held-back) and its own payout
    # history — no risk score, bureau, bank-statement, or cross-vendor data.
    "WalletResponse": [
        "as_of", "available_cents", "cleared_collected_cents",
        "disbursed_in_flight_cents", "disbursed_settled_cents",
        "due_to_vendor_cents", "held_back_cents", "holdback_business_days",
        "holdback_cutoff", "mtd_collected_cents", "share_bps",
        "total_collected_cents", "vendor_id",
    ],
    "DisbursementRow": [
        "amount_cents", "completed_at", "created_at", "external_ref",
        "fee_cents", "holdback_cutoff", "id", "kind", "period_month",
        "period_year", "requested_by", "return_code", "status", "vendor_id",
    ],
    "ExtraPayoutResponse": [
        "amount_cents", "disbursement_id", "fee_cents", "status",
    ],
}


# ---------------------------------------------------------------------------
# Hermetic collection (imports only — no router/app instantiation state)
# ---------------------------------------------------------------------------


def _collect_models(tp, seen: set) -> None:
    """Recursively gather BaseModel classes from an annotation/generic."""
    origin = get_origin(tp)
    if origin is not None:
        for arg in get_args(tp):
            _collect_models(arg, seen)
        return
    if isinstance(tp, type) and issubclass(tp, BaseModel) and tp is not BaseModel and tp not in seen:
        seen.add(tp)
        for field in tp.model_fields.values():
            _collect_models(field.annotation, seen)


def _clinic_modules():
    for info in pkgutil.walk_packages(clinic_pkg.__path__, prefix=clinic_pkg.__name__ + "."):
        if info.name in EXCLUDED_MODULES:
            continue
        yield importlib.import_module(info.name)


def clinic_models() -> set[type[BaseModel]]:
    """Every Pydantic model reachable from a clinic/v1 module namespace whose
    home package is ``app.api.*`` (plus their nested models).

    The ``app.api.*`` filter keeps merely-imported internal schemas (e.g.
    ``PricingConfig`` from ``app.schemas``) out of the top-level scan; if such
    a model were ever EMBEDDED in a clinic schema, the field recursion would
    still pull it into the fence — exactly when it becomes vendor-exposed.
    """
    seen: set[type[BaseModel]] = set()
    for mod in _clinic_modules():
        for obj in vars(mod).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseModel)
                and obj is not BaseModel
                and obj.__module__.startswith("app.api.")
            ):
                _collect_models(obj, seen)
    return seen


def _tokens(field_name: str) -> set[str]:
    return set(field_name.lower().split("_"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_discovery_is_not_empty():
    """Guards the fence's own discovery: if the module walk ever comes back
    (near-)empty, the fence is blind and must fail LOUDLY — this is exactly the
    failure mode the route-walking version hit under FastAPI 0.139's lazy
    include_router."""
    models = clinic_models()
    assert len(models) >= 20, (
        f"Fence discovery found only {len(models)} clinic models — discovery is "
        "broken (blind fence), not a clean surface."
    )


def test_no_forbidden_field_names_on_the_vendor_surface():
    """Dave's never-list is STRUCTURALLY absent: no clinic model may declare a
    field whose name touches risk/bureau/bank/hardship/comms/SIN vocabulary.
    Absent means absent — not just null."""
    offenders = []
    for model in clinic_models():
        for name in model.model_fields:
            if (model.__name__, name) in ALLOWLIST:
                continue
            hit = _tokens(name) & FORBIDDEN_TOKENS
            if hit:
                offenders.append(f"{model.__name__}.{name} (matched: {sorted(hit)})")
    assert not offenders, (
        "Vendor-forbidden field name(s) on the clinic surface — Dave's visibility "
        "rules (10__Vendor_Access.md) prohibit exposing this data to vendors:\n"
        + "\n".join(sorted(offenders))
    )


def test_clinic_models_match_snapshot():
    """The exact field set of EVERY clinic model is pinned. A new or renamed
    field fails here by design: update EXPECTED_MODEL_FIELDS only after
    checking the field against Dave's vendor visibility rules."""
    actual = {
        model.__name__: sorted(model.model_fields.keys())
        for model in clinic_models()
    }
    assert actual == EXPECTED_MODEL_FIELDS, (
        "Clinic model snapshot drift. If you intentionally changed the vendor "
        "surface, review the change against docs/turnkey_parity/"
        "10__Vendor_Access.md §2 (vendor never-list) and update the snapshot."
    )


def test_every_expected_model_is_still_present():
    """Guards the fence itself: if a module/model is removed or renamed, the
    snapshot must shrink consciously rather than silently stop covering it."""
    actual_names = {m.__name__ for m in clinic_models()}
    assert actual_names == set(EXPECTED_MODEL_FIELDS.keys())


def test_forbidden_vocabulary_covers_the_spec_never_list():
    """Self-check: the token list keeps covering every category Dave named."""
    for required in ("sin", "risk", "score", "bureau", "bank", "routing",
                     "institution", "statement", "hardship", "log"):
        assert required in FORBIDDEN_TOKENS
