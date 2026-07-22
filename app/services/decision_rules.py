"""Decision-rules directory (Workstream F — Turnkey Settings parity, video 08 §2.1).

An admin-editable registry over the flow-engine's decision rules, mirroring the
notification-rules pattern from PR #174: the CODE registry below is the source
of truth for which rules EXIST (with their default thresholds/outcomes, seeded
from the Turnkey video-08 values + the engine's current defaults), and the
``platform_decision_rules`` table stores only EDITS. No DB row → the shipped
default applies, so current decision behaviour is preserved bit-for-bit until an
admin edits a rule.

Two enforcement tiers (honest by design):

* ``wired=True`` — the rule's thresholds are actually consumed by the live
  decision path. Today that is:
    - ``bureau_score`` → ``verification_matrix.bureau.manual_review_band``.
      The SEEDED DEFAULT below is the single source of truth for the band and
      ``flow_engine.DEFAULT_MANUAL_REVIEW_BAND`` is derived from it, so there
      is exactly one 660/600 in the codebase.

      ⚠️ DECISION-PATH, CHANGED 2026-07-22 (P0/T4): the seeded ``approve_at``
      was **680** (Turnkey-era engine default) against the **660** on Dave's
      Settings screen. It is now **660** — auto-approving applicants scoring
      660-679 who previously went to manual review. **Confirm 660 with Dave**
      before this reaches production; if he says 680, change
      :data:`BUREAU_SCORE_DEFAULT_APPROVE_AT` back — one constant, no other
      edits. Per-product ``verification_matrix.bureau.manual_review_band``
      snapshots still win over this default, so existing products that pin a
      band are untouched.
    - ``bankruptcy_check`` → ``verification_matrix.bureau.bankruptcy_discharge_min_years``.
  Wired rules have LOCKED outcomes (the engine's decline/review semantics are
  law); only their numeric params are editable, and ``enabled=False`` simply
  removes the overlay (product-snapshot / engine defaults apply again). The
  overlay is applied in ``FlowOrchestrator._decide`` via :func:`apply_overlay`.

* ``wired=False`` — directory entries for the full TL rule catalog (DTI/PTI,
  net income, min age, IBV analytics, sanctions, ...). Enable/threshold/outcome
  edits persist and are returned by the API, but the platform does not yet
  compute these signals, so they are flagged ``enforcement="directory_only"``
  in the API until their evaluators come online. This is deliberate: silently
  pretending to enforce ~46 rules would be worse than saying "stored, not yet
  enforced".

DECISION-PATH RISK: :func:`apply_overlay` sits on the automated credit-decision
path. It is a no-op unless an admin has edited a wired rule; tests pin that.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)

# Outcome vocabulary (TL video 08: manual review / alter / reject / no action).
# "alter" is out of scope (no offer-alteration engine); "no_action" = record only.
ALLOWED_OUTCOMES = ("approve", "manual_review", "decline", "no_action")

# Rule groups, in TL's on-screen order.
GROUPS = (
    "fraud_prevention",
    "application_check",
    "geolocation",
    "network_security",
    "web_behavior",
    "bank_provider",
    "credit_bureau",
    "co_applicant",
)


@dataclass(frozen=True)
class DecisionRuleSpec:
    """One rule's shipped definition (defaults; DB rows store only edits)."""

    key: str
    group: str
    name: str
    description: str
    params: dict[str, Any] = field(default_factory=dict)  # {param_key: default}
    outcome: str = "manual_review"
    enabled: bool = True
    wired: bool = False           # thresholds consumed by the live decision path
    outcome_locked: bool = False  # wired rules: outcome semantics owned by engine


def _r(key: str, group: str, name: str, description: str, *,
       params: Optional[dict[str, Any]] = None, outcome: str = "manual_review",
       enabled: bool = True, wired: bool = False,
       outcome_locked: bool = False) -> DecisionRuleSpec:
    return DecisionRuleSpec(
        key=key, group=group, name=name, description=description,
        params=params or {}, outcome=outcome, enabled=enabled,
        wired=wired, outcome_locked=outcome_locked,
    )


# ---------------------------------------------------------------------------
# Credit-bureau score band — the ONE place the numbers live
# ---------------------------------------------------------------------------
# ``flow_engine.DEFAULT_MANUAL_REVIEW_BAND`` is derived from these, the registry
# spec below seeds from them, and an admin can override per-deployment through
# ``platform_decision_rules`` (no deploy). Do not hardcode 600/660 anywhere else.
#
# ⚠️ 660 is Dave's on-screen "Second score threshold"; the engine shipped 680.
# See the module docstring — this needs Dave's written confirmation.
BUREAU_SCORE_DEFAULT_DECLINE_BELOW = 600
BUREAU_SCORE_DEFAULT_APPROVE_AT = 660


def default_manual_review_band() -> dict[str, int]:
    """The shipped ``{min, max}`` manual-review band (max is inclusive).

    ``min`` = decline floor, ``max`` = ``approve_at - 1``. Consumed by
    ``flow_engine`` as its engine default so the engine and the decision-rules
    directory can never drift apart.
    """
    return {
        "min": BUREAU_SCORE_DEFAULT_DECLINE_BELOW,
        "max": BUREAU_SCORE_DEFAULT_APPROVE_AT - 1,
    }


# ---------------------------------------------------------------------------
# The registry — seeded from Turnkey video 08 §4.1 (Dave's configured values).
# ---------------------------------------------------------------------------

_SPECS: tuple[DecisionRuleSpec, ...] = (
    # -- Fraud prevention --------------------------------------------------
    _r("sanctions_suspicious_name", "fraud_prevention", "Sanctions: suspicious name",
       "Borrower's full name found in the sanctions list."),
    _r("sanctions_name_dob_year", "fraud_prevention", "Sanctions: name and DOB (YYYY)",
       "Full name + year of birth found in the sanctions list."),
    _r("sanctions_name_dob_month_year", "fraud_prevention", "Sanctions: name and DOB (MM/YYYY)",
       "Full name + partial DOB (month + year) found in the sanctions list."),
    _r("sanctions_full_match", "fraud_prevention", "Sanctions: sanctioned borrower",
       "Full name + full date of birth found on the sanctions list."),
    _r("blacklisted", "fraud_prevention", "Blacklisted",
       "Full name, SIN, phones, email or driver's licence found in internal blacklists."),
    _r("main_phone_reused", "fraud_prevention", "Main phone",
       "Main phone already used by another borrower."),
    _r("suspicious_age", "fraud_prevention", "Suspicious age",
       "Borrower older than the configured maximum age.",
       params={"max_age": 100}),
    _r("suspicious_phone_number", "fraud_prevention", "Suspicious phone number",
       "Phone appears in the suspicious-phones dictionary."),
    _r("drivers_license_unique", "fraud_prevention", "Driver's licence",
       "Driver's licence must be unique in the database."),
    _r("sin_unique", "fraud_prevention", "Social Insurance Number",
       "SIN must be unique in the database."),
    # -- Application check -------------------------------------------------
    _r("citizenship", "application_check", "Citizenship",
       "Borrower is a Canadian resident."),
    _r("dti", "application_check", "DTI (Debt to Income Ratio)",
       "Debt-to-income ratio must not exceed the configured maximum percent.",
       params={"max_percent": 50}),
    _r("employment_since", "application_check", "Employment since",
       "Employed with the current company for at least the configured months.",
       params={"min_months": 6}),
    _r("employment_official", "application_check", "Employment",
       "Borrower has official employment."),
    _r("income_derivatives", "application_check", "Income derivatives",
       "Insurance/retirement income, or self-employed for too short a period.",
       params={"min_months": 6}),
    _r("net_income", "application_check", "Net income",
       "Net monthly income must not be less than the configured minimum.",
       params={"min_amount": 1500}),
    _r("pti", "application_check", "PTI (Payment to Net Income Ratio)",
       "Monthly payment to net income ratio must not exceed the maximum percent.",
       params={"max_percent": 20}),
    _r("age_end_of_loan", "application_check", "Age by the end of loan period",
       "Age at the end of the loan term must not exceed the configured maximum.",
       params={"max_age_male": 65, "max_age_female": 65}),
    _r("residence_tenure", "application_check", "Residence at registration address",
       "Minimum months at the registration address.",
       params={"min_months": 6}),
    _r("active_loans", "application_check", "Number of active loans",
       "Maximum concurrent active loans per borrower.",
       params={"max_active_loans": 2}),
    _r("delinquency_check", "application_check", "Delinquency check",
       "Minor / major days-past-due tiers on existing accounts.",
       params={"minor_dpd": 14, "major_dpd": 30}),
    _r("minimal_age", "application_check", "Minimal age",
       "Borrower must be at least the configured age (BC/AB age of majority: 19).",
       params={"min_age": 19}),
    # -- Geolocation -------------------------------------------------------
    _r("ip_lookup_distance", "geolocation", "IP address lookup distance",
       "Distance between borrower geolocation and IP-lookup coordinates.",
       params={"range_km": 150}),
    _r("geolocation_distance", "geolocation", "Geolocation",
       "Distance between application-form address and current geolocation.",
       params={"range_km": 150}),
    # -- Network security --------------------------------------------------
    _r("network_threat_level", "network_security", "Network threat level",
       "Medium / high network-threat classification of the applying connection."),
    _r("proxy_usage", "network_security", "Proxy usage",
       "Borrower is using a proxy server to apply."),
    _r("tor_usage", "network_security", "Tor system usage",
       "Borrower is using Tor to apply."),
    # -- Web activity / behaviour -----------------------------------------
    _r("clipboard_paste", "web_behavior", "Application details pasted from clipboard",
       "Form fields appear pasted rather than typed."),
    _r("attachment_replacements", "web_behavior", "Replacement of attachments",
       "Attached documents removed/replaced too many times.",
       params={"max_replacements": 6}),
    _r("fast_form_fill", "web_behavior", "Abnormally fast form filling",
       "Application completed faster than the configured minutes.",
       params={"min_minutes": 3}),
    _r("irresponsible_behavior", "web_behavior", "Suspected irresponsible behaviour",
       "Immediate selection of maximum loan term and amount."),
    _r("loan_term_changes", "web_behavior", "Too doubtful about the loan term",
       "Loan term changed too many times during the application.",
       params={"max_changes": 6}),
    # -- Bank provider (IBV analytics) ------------------------------------
    _r("no_credit_transactions", "bank_provider", "No credit transactions",
       "No credit transactions in the account for too many days.",
       params={"max_days": 24}),
    _r("no_debit_transactions", "bank_provider", "No debit transactions",
       "No debit transactions in the account for too many days.",
       params={"max_days": 24}),
    _r("nontypical_payment_behavior", "bank_provider", "Nontypical payment behaviour",
       "Expenses exceeded 2.5x portfolio average too many times in 3 months.",
       params={"max_cases": 3}),
    _r("negative_balance", "bank_provider", "Negative balance",
       "Too long a period with a negative account balance.",
       params={"max_days": 30}),
    _r("account_age", "bank_provider", "Account age in days",
       "Bank account must be at least the configured days old.",
       params={"min_days": 60}),
    _r("active_days_90d", "bank_provider", "Active days in account (last 90 days)",
       "Minimum days with activity in the last 90 days.",
       params={"min_days": 20}),
    _r("child_support_income_90d", "bank_provider", "Child support income (90 days)",
       "Sum of government child-support income over 90 days.",
       enabled=False),
    _r("avg_monthly_employer_income", "bank_provider", "Average monthly employer income",
       "Minimum average monthly employer income.",
       params={"min_amount": 1500}),
    _r("avg_monthly_expenditure", "bank_provider", "Average monthly expenditure",
       "Average monthly expenditure check.", enabled=False),
    _r("avg_monthly_free_cash_flow", "bank_provider", "Average monthly free cash flow",
       "Minimum average monthly free cash flow.",
       params={"min_amount": 100}),
    _r("avg_monthly_government_income", "bank_provider", "Average monthly government income",
       "Average monthly government income check.", enabled=False),
    _r("avg_monthly_non_employer_income", "bank_provider", "Average monthly non-employer income",
       "Average monthly non-employer income check.", enabled=False),
    _r("telecom_payments", "bank_provider", "Average monthly telecom payments",
       "Maximum average monthly telecom spend.",
       params={"max_amount": 250}),
    _r("utility_payments", "bank_provider", "Average monthly utility payments",
       "Maximum average monthly utility spend.",
       params={"max_amount": 250}),
    _r("nsf_fees_90d", "bank_provider", "Count of NSF fees (last 90 days)",
       "Maximum NSF fees in the last 90 days.",
       params={"max_count": 0}),
    _r("stop_payment_fees_90d", "bank_provider", "Count of stop-payment fees (last 90 days)",
       "Maximum stop-payment fees in the last 90 days.",
       params={"max_count": 0}),
    _r("micro_loan_payment", "bank_provider", "Micro-loan payment",
       "Maximum detected micro-loan payment amount.",
       params={"max_amount": 0}),
    _r("employed_income_present", "bank_provider", "Check borrower's employed income",
       "Employed borrower with zero employer income in the last month."),
    _r("income_vs_avg_inflow", "bank_provider", "Income vs average total inflow",
       "Stated income < 80% of last-3-month average total inflow."),
    _r("inflow_vs_min_income", "bank_provider", "Average total inflow vs minimal income",
       "Last-3-month average total inflow < 80% of minimal income."),
    _r("outflow_vs_min_living_cost", "bank_provider", "Average total outflow vs minimal living cost",
       "Last-3-month average total outflow < 80% of minimal living cost."),
    # -- Credit bureau (WIRED rules live here) -----------------------------
    _r("bankruptcy_check", "credit_bureau", "Bankruptcy check",
       "Active bankruptcy declines; a discharge younger than min_years_discharged "
       "declines (manual review under vendor override). WIRED to "
       "verification_matrix.bureau.bankruptcy_discharge_min_years.",
       params={"max_bankruptcies": 0, "min_years_discharged": 2},
       outcome="decline", wired=True, outcome_locked=True),
    _r("defaults_check", "credit_bureau", "Defaults check",
       "Maximum reported defaults.",
       params={"max_defaults": 1}),
    _r("bureau_inquiries", "credit_bureau", "Credit bureau inquiries",
       "Minor / major inquiry-count tiers (6 months).",
       params={"minor_count": 10, "major_count": 15}),
    _r("bureau_score", "credit_bureau", "Credit bureau score",
       "Score band: below decline_below → decline; decline_below..approve_at-1 → "
       "manual review; approve_at and above → approve. WIRED to "
       "verification_matrix.bureau.manual_review_band. Seeded from Dave's "
       "Settings screen (second score threshold 660); was 680 until 2026-07-22 "
       "— DECISION-PATH, pending Dave's written confirmation.",
       params={"decline_below": BUREAU_SCORE_DEFAULT_DECLINE_BELOW,
               "approve_at": BUREAU_SCORE_DEFAULT_APPROVE_AT},
       outcome="manual_review", wired=True, outcome_locked=True),
    # -- Co-applicant ------------------------------------------------------
    _r("coapp_blacklisted", "co_applicant", "Co-applicants: blacklisted",
       "Co-applicant's phones, email, full name or ID found in internal blacklists."),
    _r("coapp_suspicious_age", "co_applicant", "Co-applicants: suspicious age",
       "Co-applicant older than the configured maximum age.",
       params={"max_age": 100}),
    _r("coapp_suspicious_phone", "co_applicant", "Co-applicants: suspicious phone number",
       "Co-applicant phone appears in the suspicious-phones dictionary."),
    _r("coapp_minimal_age", "co_applicant", "Co-applicants: minimal age",
       "Co-applicant must be at least the configured age.",
       params={"min_age": 19}),
    _r("coapp_bankruptcy_check", "co_applicant", "Co-applicants: bankruptcy check",
       "Maximum reported bankruptcies for the co-applicant.",
       params={"max_bankruptcies": 0}),
)

DECISION_RULE_REGISTRY: dict[str, DecisionRuleSpec] = {s.key: s for s in _SPECS}

# Wired-rule keys, referenced by the overlay + the API's outcome lock.
WIRED_RULE_KEYS = tuple(s.key for s in _SPECS if s.wired)


# ---------------------------------------------------------------------------
# Effective-rule resolution (DB edits over registry defaults)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectiveRule:
    """A rule's effective (registry ⊕ DB-edit) state."""

    key: str
    group: str
    name: str
    description: str
    params: dict[str, Any]
    outcome: str
    enabled: bool
    wired: bool
    outcome_locked: bool
    customized: bool  # a DB edit row exists


def _merge(spec: DecisionRuleSpec, row: Any) -> EffectiveRule:
    """Overlay a DB edit row (or None) onto the registry spec.

    Params are merged key-by-key: an edit may set a subset; unknown param keys
    in the row are ignored (the registry's param schema is authoritative)."""
    params = dict(spec.params)
    outcome = spec.outcome
    enabled = spec.enabled
    if row is not None:
        row_params = getattr(row, "params", None) or {}
        for k in params:
            if k in row_params and row_params[k] is not None:
                params[k] = row_params[k]
        row_outcome = getattr(row, "outcome", None)
        if row_outcome and not spec.outcome_locked and row_outcome in ALLOWED_OUTCOMES:
            outcome = row_outcome
        row_enabled = getattr(row, "enabled", None)
        if row_enabled is not None:
            enabled = bool(row_enabled)
    return EffectiveRule(
        key=spec.key, group=spec.group, name=spec.name, description=spec.description,
        params=params, outcome=outcome, enabled=enabled, wired=spec.wired,
        outcome_locked=spec.outcome_locked, customized=row is not None,
    )


def get_effective_rule(db: Any, key: str) -> EffectiveRule:
    """Effective state for one rule key. Raises KeyError for unknown keys."""
    spec = DECISION_RULE_REGISTRY[key]
    row = _get_row(db, key)
    return _merge(spec, row)


def get_effective_rules(db: Any) -> list[EffectiveRule]:
    """Effective state for the whole directory, in registry (TL screen) order."""
    return [_merge(spec, _get_row(db, spec.key)) for spec in _SPECS]


def _get_row(db: Any, key: str) -> Any:
    from app.models.platform.decision_rule import PlatformDecisionRule

    if db is None:
        return None
    return db.get(PlatformDecisionRule, key)


def validate_edit(key: str, *, params: Optional[dict[str, Any]] = None,
                  outcome: Optional[str] = None) -> Optional[str]:
    """Validate a proposed edit; returns an error string or None when valid.

    * unknown rule key → error
    * unknown param key / non-numeric value for a numeric default → error
    * outcome not in ALLOWED_OUTCOMES → error
    * outcome change on an outcome-locked (wired) rule → error
    """
    spec = DECISION_RULE_REGISTRY.get(key)
    if spec is None:
        return f"unknown decision rule {key!r}"
    if params is not None:
        for pk, pv in params.items():
            if pk not in spec.params:
                return f"unknown parameter {pk!r} for rule {key!r}"
            default = spec.params[pk]
            if isinstance(default, bool):
                if not isinstance(pv, bool):
                    return f"parameter {pk!r} must be a boolean"
            elif isinstance(default, (int, float)):
                if isinstance(pv, bool) or not isinstance(pv, (int, float)):
                    return f"parameter {pk!r} must be a number"
                if pv < 0:
                    return f"parameter {pk!r} must be non-negative"
    if outcome is not None:
        if outcome not in ALLOWED_OUTCOMES:
            return f"outcome must be one of {ALLOWED_OUTCOMES}"
        if spec.outcome_locked and outcome != spec.outcome:
            return (
                f"rule {key!r} is wired into the decision engine; its outcome is "
                "locked (edit its thresholds instead)"
            )
    # Wired-rule structural sanity: the band must stay ordered.
    if key == "bureau_score" and params:
        merged = {**spec.params, **params}
        if int(merged["approve_at"]) <= int(merged["decline_below"]):
            return "approve_at must be greater than decline_below"
    return None


# ---------------------------------------------------------------------------
# DECISION-PATH overlay — the ONLY place the directory touches live decisions
# ---------------------------------------------------------------------------


def apply_overlay(db: Any, decision_product: Any) -> Any:
    """Overlay admin-edited WIRED rules onto the decision product view.

    Called by ``FlowOrchestrator._decide`` after the snapshot-based product view
    is built. Behaviour contract (pinned by tests):

    * No DB edit rows for wired rules → returns ``decision_product`` UNCHANGED
      (the exact same object) — zero decision-path drift by default.
    * ``bureau_score`` edited + enabled → ``bureau.manual_review_band`` becomes
      ``{min: decline_below, max: approve_at - 1}``.
    * ``bankruptcy_check`` edited + enabled → ``bureau.bankruptcy_discharge_min_years``
      becomes ``min_years_discharged``.
    * An edited wired rule with ``enabled=False`` removes its overlay (the
      snapshot / engine defaults apply again) — same as no row.

    The snapshot itself is never mutated; a shallow-copied matrix is returned on
    a fresh namespace.
    """
    if db is None:
        return decision_product

    overrides: dict[str, Any] = {}
    try:
        score_rule = get_effective_rule(db, "bureau_score")
        if score_rule.customized and score_rule.enabled:
            overrides["manual_review_band"] = {
                "min": int(score_rule.params["decline_below"]),
                "max": int(score_rule.params["approve_at"]) - 1,
            }
        bk_rule = get_effective_rule(db, "bankruptcy_check")
        if bk_rule.customized and bk_rule.enabled:
            overrides["bankruptcy_discharge_min_years"] = int(
                bk_rule.params["min_years_discharged"]
            )
    except Exception:  # noqa: BLE001 — directory failure must NEVER break decisions
        logger.warning("decision_rules_overlay_failed_falling_back", exc_info=True)
        return decision_product

    if not overrides:
        return decision_product

    matrix = getattr(decision_product, "verification_matrix", None)
    matrix = dict(matrix) if isinstance(matrix, dict) else {}
    bureau = matrix.get("bureau")
    bureau = dict(bureau) if isinstance(bureau, dict) else {}
    bureau.update(overrides)
    matrix["bureau"] = bureau
    logger.info(
        "decision_rules_overlay_applied",
        overrides=sorted(overrides.keys()),
        product_id=str(getattr(decision_product, "id", None)),
    )
    return SimpleNamespace(
        id=getattr(decision_product, "id", None),
        version=getattr(decision_product, "version", None),
        verification_matrix=matrix,
    )
