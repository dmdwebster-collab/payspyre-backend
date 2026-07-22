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

THE MASKED-VALUE CONTRACT (replaces the old blanket token ban)
--------------------------------------------------------------
This fence used to ban the tokens ``bank``/``account``/``routing``/
``institution`` outright. That is *wrong for the product*: Dave's video-10 rule
R2 explicitly asks for masked bank details on the vendor surface — *"the
account number ends in 000… the institution number could be fully blocked
out."* A blanket ban would have failed CI on the feature he asked for, and the
tempting fix — allowlisting the token — reopens the entire category.

So the bank vocabulary is now **conditionally** permitted, and only in a form
that is provably masked:

* ``HARD_FORBIDDEN_TOKENS`` — risk/score, bureau, bank statements, hardship,
  scheduled-transaction internals, borrower comms, SIN, decision internals, and
  payment-rail identity. Never permitted, in any form, masked or not.
* ``MASKABLE_TOKENS`` (``bank account routing transit institution iban``) — a
  field may carry these ONLY if it is
    - named ``*_masked`` and typed :data:`app.api.clinic.v1.masking.MaskedValue`
      (validator: must contain redaction characters, may reveal ≤4 digits), or
    - named ``*_last4`` and typed :data:`app.api.clinic.v1.masking.Last4`
      (validator: 1–4 digits and nothing else), or
    - a pure trade-NAME field (``*_name``/``*_label``) whose only maskable hit
      is ``bank``/``institution`` — *"your account is with Flinks Capital"* is a
      label, not an account identifier.
  Anything else — ``account_number``, ``routing_number``, a ``str``-typed
  ``account_number_masked`` — fails. The NAME test proves the shape; the
  validators in ``app/api/clinic/v1/masking.py`` (unit-tested below) make an
  unmasked VALUE impossible to serialize through the declared type.

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
from typing import Annotated, Optional, Union, get_args, get_origin

import pytest
from pydantic import BaseModel, ValidationError

import app.api.clinic.v1 as clinic_pkg
from app.api.clinic.v1.masking import (
    Last4,
    MaskedField,
    MaskedValue,
    mask_tail,
    validate_last4,
    validate_masked,
)

# ---------------------------------------------------------------------------
# Forbidden vocabulary (Dave's never-list). Matching is TOKEN-based on the
# snake_case field name so e.g. "housing" does not false-positive on "sin".
# ---------------------------------------------------------------------------

# NEVER permitted, in any form — masking does not redeem these.
HARD_FORBIDDEN_TOKENS = {
    # SIN / identifiers
    "sin", "ssn",
    # proprietary risk + bureau (R4, R5)
    "risk", "score", "bureau", "equifax", "transunion",
    # bank statements — the verification payload itself (R7)
    "statement", "statements", "flinks",
    # hardship / rescheduling (R8) + scheduled-transaction internals (R9).
    # NB "scheduled" ≠ "schedule": the contractual amortization schedule
    # (VendorPaymentPreview.schedule) is vendor-safe and stays allowed.
    "hardship", "reschedule", "rescheduling", "suspension", "suspended",
    "scheduled",
    # payment-processor / rail identity (R11 — vendors see the accounting of a
    # payment and its method, never the processor or rail behind it)
    "zumrails", "processor", "rail", "gateway", "merchant",
    # borrower comms / contact log (R12)
    "comms", "communication", "communications", "log",
    # raw decision internals (vendors get the mapped status only)
    "decision", "reasons",
}

# Permitted ONLY through the masked-value contract below (R2).
MASKABLE_TOKENS = {"bank", "account", "routing", "transit", "institution", "iban"}

# Trade-name/label forms are permitted for these tokens only: an institution's
# NAME ("Flinks Capital") is not an account identifier. Never for account /
# routing / transit / iban, where a "name" could carry the number itself.
NAMEABLE_TOKENS = {"bank", "institution"}
NAME_SUFFIXES = ("_name", "_label")

MASKED_SUFFIX = "_masked"
LAST4_SUFFIXES = ("_last4", "_last_4")

# Back-compat / self-check: the full never-list vocabulary.
FORBIDDEN_TOKENS = HARD_FORBIDDEN_TOKENS | MASKABLE_TOKENS

# Fields whose name trips a token but whose content is explicitly vendor-safe.
# Each entry needs a justification. This is a per-FIELD escape hatch, never a
# per-token one — allowlisting a token would reopen a whole category.
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
# The masked-value contract
# ---------------------------------------------------------------------------


def _mask_kind(annotation) -> Optional[str]:
    """The :class:`MaskedField` kind declared by an annotation, if any.

    Accepts either a raw annotation or a pydantic ``FieldInfo`` (pydantic v2
    hoists ``Annotated`` metadata off ``.annotation`` and onto ``.metadata``).
    Unwraps ``Annotated[...]`` and ``Optional[...]`` / unions so
    ``Optional[MaskedValue]`` is recognised just like ``MaskedValue``.
    """
    for meta in getattr(annotation, "metadata", None) or ():
        if isinstance(meta, MaskedField):
            return meta.kind
    if hasattr(annotation, "annotation"):  # FieldInfo
        return _mask_kind(annotation.annotation)
    if get_origin(annotation) is Annotated:
        args = get_args(annotation)
        for meta in args[1:]:
            if isinstance(meta, MaskedField):
                return meta.kind
        return _mask_kind(args[0])
    if get_origin(annotation) is Union:
        for arg in get_args(annotation):
            kind = _mask_kind(arg)
            if kind:
                return kind
    return None


def field_violation(model_name: str, field_name: str, annotation) -> Optional[str]:
    """The fence rule for ONE field. Returns a message, or None if permitted.

    Shared by the real-surface scan and the contract unit tests below, so the
    tests exercise exactly the rule that guards production.
    """
    if (model_name, field_name) in ALLOWLIST:
        return None

    name = field_name.lower()
    tokens = _tokens(name)

    hard = tokens & HARD_FORBIDDEN_TOKENS
    if hard:
        return (
            f"{model_name}.{field_name} — forbidden vocabulary {sorted(hard)}; "
            "this category is never exposed to vendors, masked or not."
        )

    maskable = tokens & MASKABLE_TOKENS
    if not maskable:
        return None

    # Trade name / label (e.g. bank_name) — permitted for bank/institution only.
    if name.endswith(NAME_SUFFIXES) and maskable <= NAMEABLE_TOKENS:
        return None

    if name.endswith(MASKED_SUFFIX):
        if _mask_kind(annotation) == "masked":
            return None
        return (
            f"{model_name}.{field_name} — named as masked but not typed "
            "`MaskedValue`; the mask must be enforced by the type, not by the "
            "field name (app/api/clinic/v1/masking.py)."
        )

    if name.endswith(LAST4_SUFFIXES):
        if _mask_kind(annotation) == "last4":
            return None
        return (
            f"{model_name}.{field_name} — named as a last-4 field but not typed "
            "`Last4` (app/api/clinic/v1/masking.py)."
        )

    return (
        f"{model_name}.{field_name} — bank vocabulary {sorted(maskable)} on the "
        "vendor surface must be masked: name it `*_masked` (typed MaskedValue) "
        "or `*_last4` (typed Last4). Full account/routing/institution numbers "
        "are never exposed to vendors (video 10 R2)."
    )


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
    field whose name touches risk/bureau/hardship/comms/SIN/rail vocabulary, and
    bank vocabulary only through the masked-value contract. Absent means absent
    — not just null."""
    offenders = []
    for model in clinic_models():
        for name, field in model.model_fields.items():
            problem = field_violation(model.__name__, name, field)
            if problem:
                offenders.append(problem)
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
    # The maskable set is a strict, named subset — everything else is absolute.
    assert MASKABLE_TOKENS.isdisjoint(HARD_FORBIDDEN_TOKENS)
    assert FORBIDDEN_TOKENS == HARD_FORBIDDEN_TOKENS | MASKABLE_TOKENS


def test_the_categories_dave_never_wants_stay_forbidden():
    """R4/R5/R7/R8/R9/R11/R12 + SIN: still refused, and masking cannot buy them
    in — a `*_masked` name does NOT redeem a hard-forbidden token."""
    for field_name in (
        "risk_score", "credit_score", "bureau_report_id", "equifax_file",
        "transunion_file", "bank_statement_url", "flinks_login_id",
        "hardship_plan", "rescheduling_status", "suspended_until",
        "scheduled_transactions", "zumrails_customer_id", "processor_ref",
        "rail_type", "gateway_id", "merchant_id", "contact_log",
        "communication_history", "decision_reasons", "sin", "ssn_last4",
    ):
        assert field_violation("SomeClinicModel", field_name, str), field_name
        # even dressed up as masked
        assert field_violation("SomeClinicModel", field_name + "_masked", MaskedValue)


# --- R2: masked bank details are PERMITTED ---------------------------------


class _BankDetailExample(BaseModel):
    """Dave's R2 view: "your account is with Flinks Capital, the account number
    ends in 000… the institution number could be fully blocked out."""

    bank_name: str
    account_number_masked: MaskedValue
    account_number_last4: Last4
    routing_number_masked: MaskedValue
    institution_number_masked: MaskedValue


def test_masked_bank_detail_fields_pass_the_fence():
    for name, field in _BankDetailExample.model_fields.items():
        assert field_violation("_BankDetailExample", name, field) is None, name


def test_full_account_number_fails_the_fence():
    class _Leaky(BaseModel):
        account_number: str
        routing_number: str
        institution_number: str
        iban: str
        transit_number: str

    offenders = [
        name
        for name, field in _Leaky.model_fields.items()
        if field_violation("_Leaky", name, field)
    ]
    assert offenders == list(_Leaky.model_fields)


def test_masked_name_without_the_masked_type_fails():
    """The mask must be enforced by the TYPE — a `str` named `*_masked` is a
    promise, not a guarantee, and is exactly how the category would leak back."""

    class _NameOnly(BaseModel):
        account_number_masked: str
        account_number_last4: str

    for name, field in _NameOnly.model_fields.items():
        assert field_violation("_NameOnly", name, field) is not None


def test_optional_masked_field_is_recognised():
    class _Optional(BaseModel):
        account_number_masked: Optional[MaskedValue] = None

    field = _Optional.model_fields["account_number_masked"]
    assert field_violation("_Optional", "account_number_masked", field) is None


def test_name_form_is_only_for_bank_and_institution():
    assert field_violation("M", "bank_name", str) is None
    assert field_violation("M", "institution_name", str) is None
    # An "account name" could carry the number itself — not a free pass.
    assert field_violation("M", "account_name", str) is not None
    assert field_violation("M", "routing_label", str) is not None


# --- value-level: an unmasked value cannot serialize through the type ------


def test_masked_value_validator_accepts_dave_examples():
    assert validate_masked("•••• 000") == "•••• 000"
    assert validate_masked("•••••5") == "•••••5"
    assert validate_masked("•••") == "•••"  # institution fully blocked
    assert validate_masked("****1234") == "****1234"


def test_masked_value_validator_rejects_unmasked_and_overlong():
    for bad in ("123456789", "", "   ", "•12345", "4029930001234"):
        with pytest.raises(ValueError):
            validate_masked(bad)


def test_last4_validator():
    assert validate_last4("0000") == "0000"
    assert validate_last4(" 123 ") == "123"
    for bad in ("12345", "abcd", "12a4", ""):
        with pytest.raises(ValueError):
            validate_last4(bad)


def test_model_refuses_to_serialize_an_unmasked_account_number():
    """The end-to-end guarantee: even if a builder passes the raw number, the
    declared type rejects it — the vendor response never carries it."""
    with pytest.raises(ValidationError):
        _BankDetailExample(
            bank_name="Flinks Capital",
            account_number_masked="402993000",  # raw — must be refused
            account_number_last4="000",
            routing_number_masked="•••••5",
            institution_number_masked="•••",
        )
    ok = _BankDetailExample(
        bank_name="Flinks Capital",
        account_number_masked=mask_tail("402993000", reveal=3),
        account_number_last4="3000",
        routing_number_masked="•••••5",
        institution_number_masked="•••",
    )
    assert ok.account_number_masked.endswith("000")
    assert "402993" not in ok.account_number_masked


def test_mask_tail_helper():
    assert mask_tail("402993000", reveal=3).endswith("000")
    assert mask_tail("402993000", reveal=0).strip("•") == ""
    # reveal is clamped to the 4-digit maximum
    assert len([c for c in mask_tail("402993000", reveal=9) if c.isdigit()]) == 4
    assert mask_tail(None) is None
    assert mask_tail("no-digits") is None
