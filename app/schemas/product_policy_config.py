"""Typed product-policy configuration (Wave 2 W2-APPCONFIG; TL video 07 §2.8-2.18
"Edit credit product" tabs beyond Schedule building / pricing).

``PricingConfig`` (``app/schemas/pricing_config.py``) already formalises the
Schedule-building tab (interest, fees, frequencies, amount/term bounds). This
schema formalises the *remaining* product tabs onto a sibling document stored in
``platform_credit_products.policy_config`` (JSONB, nullable):

    Grace period · Due dates · Due-date seasons · Payoff · Disbursement options
    · Approval · Repayment modes (+ loan-closure / installment-recalc)

DESIGN — defaults preserve current behaviour (the contract of this workstream):

  * ``policy_config = NULL`` on every existing product row. Readers fall back to
    :data:`DEFAULT_PRODUCT_POLICY_CONFIG`, whose values encode exactly what the
    engine does today. No product save is required to keep working.
  * **Payoff is config-DRIVEN but math-UNCHANGED.** The actuals engine already
    computes payoff = outstanding principal + interest-accrued-to-date +
    fees-due + add-on balance (``app/services/interest_engine.BalanceView``),
    which *is* Dave's rule: 100% principal, 100% outstanding NSF/fees, 0% future
    interest, no future admin/origination generated. This schema DESCRIBES that
    behaviour as per-fee ``future_pct`` knobs and validation refuses a config
    that would contradict the engine (:func:`assert_payoff_consistent_with_engine`),
    so a product can never advertise a payoff rule the engine won't honour. The
    quote path attaches the descriptor; it does not recompute anything.
  * Integer-cents / int / bool / enum primitives only; pydantic-only (no app
    imports) so quote engine + product CRUD + tests import it cycle-free.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

logger = logging.getLogger(__name__)

PRODUCT_POLICY_SCHEMA_VERSION = 1


class ProductPolicyConfigError(ValueError):
    """A policy_config payload failed schema or engine-consistency validation."""


# ---------------------------------------------------------------------------
# Tab 2 — Grace period (TL 07 §2.10)
# ---------------------------------------------------------------------------

class GracePeriodConfig(BaseModel):
    """Promotional "no payments / no interest for N installments" period.

    Off by default — no current account uses it (Dave)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    term_installments: int = Field(default=1, ge=1, le=24)


# ---------------------------------------------------------------------------
# Tab 4 — Due dates (TL 07 §2.12)
# ---------------------------------------------------------------------------

class DueDatesConfig(BaseModel):
    """Start-date shift + first-due-date alignment window + holiday rolling."""

    model_config = ConfigDict(extra="forbid")

    use_change_start_date: bool = True
    default_start_shift_days: int = Field(default=1, ge=0, le=365)
    use_change_first_due_date: bool = True
    first_due_min_days: int = Field(default=1, ge=0, le=365)
    first_due_max_days: int = Field(default=45, ge=1, le=365)
    enable_date_rolling: bool = False  # needs Business Calendar; off (TL parity)

    @model_validator(mode="after")
    def _bounds(self) -> "DueDatesConfig":
        if self.first_due_min_days > self.first_due_max_days:
            raise ValueError("first_due_min_days must be <= first_due_max_days")
        return self


# ---------------------------------------------------------------------------
# Tab 5 — Due-date seasons (TL 07 §2.13) — Dave: leave out for now.
# ---------------------------------------------------------------------------

class DueDateSeason(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=100)
    on_season_start: str = Field(..., description="MM-DD")
    on_season_end: str = Field(..., description="MM-DD")


class DueDateSeasonsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    seasons: list[DueDateSeason] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enabled_needs_seasons(self) -> "DueDateSeasonsConfig":
        if self.enabled and not self.seasons:
            raise ValueError("due_date_seasons.enabled requires at least one season")
        return self


# ---------------------------------------------------------------------------
# Tab 6 — Payoff (TL 07 §2.14). Per-fee "by date" scope + "future %".
# ---------------------------------------------------------------------------

class PayoffByDate(str, Enum):
    ALL = "all"  # TL demoed "All" for every row


class PayoffFeeRule(BaseModel):
    """One row of the TL payoff grid: fee name · by-date scope · future %.

    ``future_pct`` = the percentage of *future* (not-yet-accrued/not-yet-charged)
    amounts of this category that a payoff collects. Dave's mandated grid:
    interest 0, principal 100, nsf 100, administration 0, origination 0.
    """

    model_config = ConfigDict(extra="forbid")

    category: str = Field(..., min_length=1, max_length=64)
    by_date: PayoffByDate = PayoffByDate.ALL
    future_pct: int = Field(..., ge=0, le=100)


def _default_payoff_rules() -> list[PayoffFeeRule]:
    return [
        PayoffFeeRule(category="interest", future_pct=0),
        PayoffFeeRule(category="principal", future_pct=100),
        PayoffFeeRule(category="nsf", future_pct=100),
        PayoffFeeRule(category="administration", future_pct=0),
        PayoffFeeRule(category="origination", future_pct=0),
    ]


class PayoffConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    use_payoff_grace_period: bool = False
    rules: list[PayoffFeeRule] = Field(default_factory=_default_payoff_rules)

    @field_validator("rules")
    @classmethod
    def _unique_categories(cls, v: list[PayoffFeeRule]) -> list[PayoffFeeRule]:
        cats = [r.category for r in v]
        if len(cats) != len(set(cats)):
            raise ValueError("payoff rules must have unique categories")
        return v

    def future_pct_for(self, category: str) -> Optional[int]:
        for r in self.rules:
            if r.category == category:
                return r.future_pct
        return None


# ---------------------------------------------------------------------------
# Tab 8 — Disbursement options (TL 07 §2.16)
# ---------------------------------------------------------------------------

class DisbursementType(str, Enum):
    VIRTUAL = "virtual_disbursement"           # vendor provides capital (100% today)
    TO_CUSTOMER = "disbursement_to_customer"   # direct lending to borrower
    TO_VENDOR = "disbursement_to_vendor"       # lender finances, pays clinic


class DisbursementConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    multiple_disbursements: bool = False  # credit line — needs new legal docs
    disbursement_type: DisbursementType = DisbursementType.VIRTUAL


# ---------------------------------------------------------------------------
# Tab 9 — Approval (TL 07 §2.17)
# ---------------------------------------------------------------------------

class ApprovalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    use_two_level_approval: bool = False


# ---------------------------------------------------------------------------
# Tab 10 — Repayment modes (TL 07 §2.18)
# ---------------------------------------------------------------------------

class RepaymentModeAvailability(str, Enum):
    BACK_OFFICE = "back_office"
    CUSTOMER = "customer"


class RepaymentModeConfig(BaseModel):
    """A repayment mode = key + who can use it. The step/bucket engine lives in
    ``app/services/interest_engine`` allocators; the ``key`` maps to one of its
    modes (``regular``/``add_on``/``special``/``payoff``) or to a
    non-user-facing/parity mode (``automatic``/``initial``)."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., min_length=1, max_length=40)
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    availability: list[RepaymentModeAvailability] = Field(default_factory=list)
    is_default: bool = False


class LoanClosure(str, Enum):
    #: "Immediately once there is no debt on a loan" — zero TOTAL debt (incl.
    #: fees), the current engine behaviour (record_payment zero-debt rule).
    ZERO_TOTAL_DEBT = "immediately_zero_total_debt"


class FutureInstallmentRecalc(str, Enum):
    #: "Keep initial installment payment and reduce number of future
    #: installments" — installments stay level; the count shrinks. Current
    #: engine behaviour.
    KEEP_INSTALLMENT_REDUCE_COUNT = "keep_installment_reduce_count"


def _default_repayment_modes() -> list[RepaymentModeConfig]:
    A = RepaymentModeAvailability
    return [
        RepaymentModeConfig(
            key="automatic", name="Automatic payment",
            description="Used only for auto-charges; always runs on schedule.",
            availability=[],
        ),
        RepaymentModeConfig(
            key="regular", name="Regular payment",
            description="Regular payments on/around due dates.",
            availability=[A.BACK_OFFICE, A.CUSTOMER], is_default=True,
        ),
        RepaymentModeConfig(
            key="add_on", name="Add-on payment",
            description="Pay NSF / late fees out of the schedule (add-on bucket).",
            availability=[A.BACK_OFFICE, A.CUSTOMER],
        ),
        RepaymentModeConfig(
            key="special", name="Special payment",
            description="Principal-only payment. Back-office users only.",
            availability=[A.BACK_OFFICE],
        ),
        RepaymentModeConfig(
            key="payoff", name="Payoff",
            description="Complete pay off of the loan as of the current date.",
            availability=[A.BACK_OFFICE, A.CUSTOMER],
        ),
    ]


class RepaymentModesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modes: list[RepaymentModeConfig] = Field(default_factory=_default_repayment_modes)
    loan_closure: LoanClosure = LoanClosure.ZERO_TOTAL_DEBT
    future_installments_recalc: FutureInstallmentRecalc = (
        FutureInstallmentRecalc.KEEP_INSTALLMENT_REDUCE_COUNT
    )

    @model_validator(mode="after")
    def _one_default(self) -> "RepaymentModesConfig":
        keys = [m.key for m in self.modes]
        if len(keys) != len(set(keys)):
            raise ValueError("repayment modes must have unique keys")
        defaults = [m for m in self.modes if m.is_default]
        if len(defaults) > 1:
            raise ValueError("at most one repayment mode may be is_default")
        return self


# ---------------------------------------------------------------------------
# Root document
# ---------------------------------------------------------------------------

class ProductPolicyConfig(BaseModel):
    """The typed shape of ``platform_credit_products.policy_config`` (nullable).

    ``application_form_variant`` selects which
    ``ApplicationProcessConfig.form_variants`` entry the applicant sees for this
    product (``default`` = the platform default form)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=PRODUCT_POLICY_SCHEMA_VERSION)
    grace_period: GracePeriodConfig = Field(default_factory=GracePeriodConfig)
    due_dates: DueDatesConfig = Field(default_factory=DueDatesConfig)
    due_date_seasons: DueDateSeasonsConfig = Field(default_factory=DueDateSeasonsConfig)
    payoff: PayoffConfig = Field(default_factory=PayoffConfig)
    disbursement: DisbursementConfig = Field(default_factory=DisbursementConfig)
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    repayment_modes: RepaymentModesConfig = Field(default_factory=RepaymentModesConfig)
    application_form_variant: str = Field(default="default", min_length=1, max_length=100)

    @field_validator("schema_version")
    @classmethod
    def _version(cls, v: int) -> int:
        if v != PRODUCT_POLICY_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported policy_config schema_version {v} "
                f"(expected {PRODUCT_POLICY_SCHEMA_VERSION})"
            )
        return v


#: The shipped default — encodes exactly the engine's current behaviour.
DEFAULT_PRODUCT_POLICY_CONFIG = ProductPolicyConfig()


# ---------------------------------------------------------------------------
# Engine-consistency guard for payoff (behaviour-preservation invariant)
# ---------------------------------------------------------------------------

#: The actuals engine's fixed payoff behaviour, as future-% per category. The
#: engine computes payoff = principal + interest-to-``as_of`` + fees-due +
#: add-on, i.e. it collects 100% of outstanding principal / NSF / accrued fees
#: and 0% of any FUTURE (not-yet-accrued) interest or not-yet-generated fee.
ENGINE_PAYOFF_FUTURE_PCT = {
    "interest": 0,
    "principal": 100,
    "nsf": 100,
    "administration": 0,
    "origination": 0,
}


def assert_payoff_consistent_with_engine(cfg: ProductPolicyConfig) -> None:
    """Refuse a payoff config that contradicts the engine's fixed behaviour.

    The engine's payoff math is not configurable (Dave's rule is law); this
    keeps a product from *advertising* a payoff rule the engine won't honour.
    A category the config omits is fine (engine default applies); a category it
    sets must match :data:`ENGINE_PAYOFF_FUTURE_PCT`. Raises
    :class:`ProductPolicyConfigError` on a mismatch.
    """
    for rule in cfg.payoff.rules:
        expected = ENGINE_PAYOFF_FUTURE_PCT.get(rule.category)
        if expected is None:
            continue
        if rule.future_pct != expected:
            raise ProductPolicyConfigError(
                f"payoff rule for {rule.category!r} sets future_pct="
                f"{rule.future_pct} but the actuals engine collects "
                f"{expected}% (payoff math is fixed: 100% principal + "
                "outstanding fees, 0% future interest)"
            )


def payoff_policy_descriptor(cfg: ProductPolicyConfig) -> dict:
    """A JSON-able description of what a payoff collects, for the quote path.

    Purely descriptive — it does NOT feed the payoff amount (that stays the
    server-computed ``BalanceView.payoff_cents``)."""
    return {
        "use_payoff_grace_period": cfg.payoff.use_payoff_grace_period,
        "future_pct": {r.category: r.future_pct for r in cfg.payoff.rules},
        "closes_at_zero_total_debt": (
            cfg.repayment_modes.loan_closure is LoanClosure.ZERO_TOTAL_DEBT
        ),
    }


def parse_product_policy_config(
    raw: Optional[dict], *, context: str = ""
) -> ProductPolicyConfig:
    """Parse a stored/submitted policy_config into the typed schema.

    ``None`` / ``{}`` → shipped defaults (current behaviour). Any other payload
    is strictly validated AND checked for payoff engine-consistency. Raises
    :class:`ProductPolicyConfigError` on invalid payloads.
    """
    if raw is None or raw == {}:
        return ProductPolicyConfig()
    if not isinstance(raw, dict):
        raise ProductPolicyConfigError(
            f"policy_config must be an object, got {type(raw).__name__}"
        )
    try:
        cfg = ProductPolicyConfig.model_validate(raw)
    except ValidationError as exc:
        raise ProductPolicyConfigError(
            f"policy_config failed schema validation: {exc}"
        ) from exc
    assert_payoff_consistent_with_engine(cfg)
    return cfg
