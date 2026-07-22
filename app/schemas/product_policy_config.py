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
# Tab 1 — Schedule building, the half PricingConfig does NOT cover
# (rewatch_2026-07-21/07-08_settings.md §A7.1 "Tab 1 — Schedule building")
#
# PricingConfig owns interest / fees / frequencies / amount + term bounds. What
# lived only on Dave's screen and in no schema of ours: loan type, late grace
# days, calculation basis, loan phase, customizable equal payments. All five
# default to the behaviour the engine has today.
# ---------------------------------------------------------------------------


class LoanType(str, Enum):
    #: Daily simple interest — what every real product in Dave's tenant uses and
    #: what our interest engine actually computes (actual/365 on the remaining
    #: principal). This is our default, NOT the TL dialog default.
    DAILY_SIMPLE_INTEREST = "daily_simple_interest"
    #: Annuity (installment based) — the TL dialog default. Declared for parity;
    #: our engine does not implement it, so a product may not select it yet
    #: (:func:`assert_schedule_consistent_with_engine`).
    ANNUITY = "annuity"


class CalculationBasis(str, Enum):
    #: "Remaining principal" — the only basis TL demoed and the only one our
    #: engine implements (interest accrues on outstanding principal).
    REMAINING_PRINCIPAL = "remaining_principal"


class LoanPhase(str, Enum):
    GRACE_PERIOD = "grace_period"
    MAIN_PERIOD = "main_period"


class ScheduleBuildingConfig(BaseModel):
    """Schedule-building knobs outside PricingConfig's scope.

    ``late_grace_days`` is Dave's "Late grace days" (unit Day, value 0) — the
    number of days past the due date before an installment counts as late. The
    shipped default is **0**, which is exactly what the delinquency/DPD path
    does today (day 1 past due is day 1 past due), so declaring the field does
    not move a single loan's status.
    """

    model_config = ConfigDict(extra="forbid")

    loan_type: LoanType = LoanType.DAILY_SIMPLE_INTEREST
    late_grace_days: int = Field(default=0, ge=0, le=365)
    calculation_basis: CalculationBasis = CalculationBasis.REMAINING_PRINCIPAL
    loan_phases: list[LoanPhase] = Field(default_factory=lambda: [LoanPhase.MAIN_PERIOD])
    enable_customizable_equal_payments: bool = False

    @field_validator("loan_phases")
    @classmethod
    def _phases(cls, v: list[LoanPhase]) -> list[LoanPhase]:
        if not v:
            raise ValueError("loan_phases must name at least one phase")
        seen: list[LoanPhase] = []
        for phase in v:
            if phase not in seen:
                seen.append(phase)
        return seen


# ---------------------------------------------------------------------------
# Allocation ordering — `Manage accrual priority` / `Manage repayment priority`
# (rewatch §A7.1b / §A7.1c). MONEY PATH: this decides where a dollar lands
# inside an installment. Defaults reproduce the engine's current, hard-coded
# behaviour exactly; the schema makes the ordering EXPLICIT rather than
# implicit, and refuses an order the engine cannot honour.
# ---------------------------------------------------------------------------

#: The categories an ordering may name. Matches the payoff grid's vocabulary.
ALLOCATION_CATEGORIES = ("interest", "principal", "origination", "administration", "nsf")

#: TL's default ACCRUAL order (§A7.1b): fees accrue before interest/principal,
#: NSF last.
#:
#: READ-ONLY / INFORMATIONAL. Accruals are generated by the schedule builder
#: (``loan_quote`` / ``loan_servicing`` amortization) and by the per-diem
#: interest walk in ``interest_engine.compute_balances`` — neither has an
#: orderable step: within one accrual date every category is charged to its own
#: bucket independently, so "which accrues first" has no observable effect on
#: any balance. There is therefore nothing that could consume a different value,
#: and rather than accept-and-ignore one we REFUSE it
#: (:func:`assert_allocation_consistent_with_engine`) and advertise the field as
#: read-only so the UI renders it as a fixed reference list.
DEFAULT_ACCRUAL_PRIORITY = ("origination", "administration", "interest", "principal", "nsf")

#: TL's default REPAYMENT order (§A7.1c) — note it DIFFERS from the accrual
#: order. It also happens to be exactly what
#: ``interest_engine.allocate_regular_payment`` already does: interest first,
#: then principal, then scheduled fees (origination/administration share the
#: single `fees_due` bucket), and NSF last because NSF lives in the non-interest
#: -bearing add-on bucket a regular payment never touches.
DEFAULT_REPAYMENT_PRIORITY = ("interest", "principal", "origination", "administration", "nsf")


def _default_accrual_priority() -> list[str]:
    return list(DEFAULT_ACCRUAL_PRIORITY)


def _default_repayment_priority() -> list[str]:
    return list(DEFAULT_REPAYMENT_PRIORITY)


def _validate_priority(order: list[str], *, label: str) -> list[str]:
    if len(order) != len(set(order)):
        raise ValueError(f"{label} must not repeat a category")
    unknown = [c for c in order if c not in ALLOCATION_CATEGORIES]
    if unknown:
        raise ValueError(
            f"{label} names unknown categor{'y' if len(unknown) == 1 else 'ies'} "
            f"{unknown}; allowed: {list(ALLOCATION_CATEGORIES)}"
        )
    missing = [c for c in ALLOCATION_CATEGORIES if c not in order]
    if missing:
        raise ValueError(f"{label} must cover every category; missing {missing}")
    return order


class AllocationPriorityConfig(BaseModel):
    """The two ordered lists behind Dave's `Manage accrual priority` and
    `Manage repayment priority` dialogs.

    Both must be a permutation of :data:`ALLOCATION_CATEGORIES` — a partial
    order would leave the destination of some dollars undefined, which is
    precisely the ambiguity this schema exists to remove.

    ``repayment`` is CONSUMED: ``interest_engine.allocate_regular_payment``
    walks this exact list (``loan_servicing.record_payment`` resolves it from
    the loan's product). ``accrual`` is READ-ONLY/informational — see
    :data:`DEFAULT_ACCRUAL_PRIORITY`. Use
    :func:`allocation_priority_field_metadata` to render the difference.
    """

    model_config = ConfigDict(extra="forbid")

    accrual: list[str] = Field(
        default_factory=_default_accrual_priority,
        json_schema_extra={
            "readOnly": True,
            "x-informational": True,
            "x-consumed-by": None,
        },
    )
    repayment: list[str] = Field(
        default_factory=_default_repayment_priority,
        json_schema_extra={
            "readOnly": False,
            "x-informational": False,
            "x-consumed-by": "app.services.interest_engine.allocate_regular_payment",
        },
    )

    @field_validator("accrual")
    @classmethod
    def _accrual(cls, v: list[str]) -> list[str]:
        return _validate_priority(v, label="accrual priority")

    @field_validator("repayment")
    @classmethod
    def _repayment(cls, v: list[str]) -> list[str]:
        return _validate_priority(v, label="repayment priority")


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
    #: Product-level "Visible to customer" (rewatch §A7.1 top strip). True =
    #: offered in the borrower-facing catalogue. Defaults True, which is how
    #: every product behaves today (there was no flag to hide one).
    visible_to_customer: bool = True
    schedule_building: ScheduleBuildingConfig = Field(default_factory=ScheduleBuildingConfig)
    allocation_priority: AllocationPriorityConfig = Field(
        default_factory=AllocationPriorityConfig
    )
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


# ---------------------------------------------------------------------------
# Engine-consistency guards for the schedule/allocation halves (MONEY PATH)
# ---------------------------------------------------------------------------

#: The repayment ordering ``interest_engine.allocate_regular_payment`` actually
#: implements. Interest, then principal, then the shared scheduled-fee bucket
#: (origination + administration), then the add-on bucket (NSF) — which a
#: regular payment does not touch at all, so NSF is necessarily last.
ENGINE_REPAYMENT_PRIORITY = tuple(DEFAULT_REPAYMENT_PRIORITY)

#: Fee categories that share the engine's single ``fees_due`` bucket. Their
#: order relative to EACH OTHER is not observable in the engine, so a config may
#: swap them; their position relative to interest/principal/nsf is fixed.
_SHARED_FEE_BUCKET = frozenset({"origination", "administration"})


def assert_allocation_consistent_with_engine(cfg: ProductPolicyConfig) -> None:
    """Refuse an allocation ordering the engine will not honour.

    ``repayment`` IS consumed — ``interest_engine.allocate_regular_payment``
    walks the configured list. What is constrained here is therefore the set of
    orderings that are *legitimate*, not a mask over an ignored value:

      * interest before principal — daily simple interest must be covered
        before principal is reduced; the reverse would let accrued interest sit
        while the balance it accrues on shrinks.
      * scheduled fees after principal — a fee may never jump the queue ahead
        of the borrower's own principal reduction.
      * NSF last — NSF accrues to the non-interest-bearing add-on bucket, which
        a regular payment structurally cannot reach (it is targeted only by the
        Add-on repayment mode). Any other position would be inert, so it is
        refused rather than silently ignored.

    Net configurable freedom under these rules: the origination ↔
    administration swap (they share the engine's single ``fees_due`` bucket, so
    the swap is a no-op the engine honours trivially). This is stated
    explicitly, and machine-readably, by
    :func:`allocation_priority_field_metadata` so the UI can lock the rest of
    the list rather than offering a drag-and-drop that can only be rejected.

    ``accrual`` is READ-ONLY: nothing can consume a different value (see
    :data:`DEFAULT_ACCRUAL_PRIORITY`), so a deviation is refused outright.
    """
    accrual = list(cfg.allocation_priority.accrual)
    if tuple(accrual) != tuple(DEFAULT_ACCRUAL_PRIORITY):
        raise ProductPolicyConfigError(
            "allocation_priority.accrual is read-only/informational: accruals "
            "are generated per-category by the schedule builder and the "
            "per-diem interest walk, which have no orderable step, so no "
            "engine can consume a different order "
            f"(fixed value: {list(DEFAULT_ACCRUAL_PRIORITY)})"
        )

    order = list(cfg.allocation_priority.repayment)
    position = {cat: i for i, cat in enumerate(order)}

    if position["interest"] > position["principal"]:
        raise ProductPolicyConfigError(
            "repayment priority puts principal before interest, but the "
            "allocation engine always covers accrued interest first "
            f"(engine order: {list(ENGINE_REPAYMENT_PRIORITY)})"
        )
    fee_positions = [position[c] for c in _SHARED_FEE_BUCKET]
    if min(fee_positions) < position["principal"]:
        raise ProductPolicyConfigError(
            "repayment priority puts a scheduled fee before principal, but the "
            "allocation engine covers interest and principal before the fee "
            f"bucket (engine order: {list(ENGINE_REPAYMENT_PRIORITY)})"
        )
    if position["nsf"] != len(order) - 1:
        raise ProductPolicyConfigError(
            "repayment priority must place 'nsf' last — NSF accrues to the "
            "non-interest-bearing add-on bucket, which a regular payment never "
            "touches (it is targeted only by the add-on repayment mode)"
        )


def allocation_priority_field_metadata() -> dict:
    """Machine-readable editability of the two allocation-priority lists.

    Exists so the admin UI can never render an ordering control that only
    *looks* like it does something. Every entry states whether the list is
    editable, exactly which reorderings are permitted, and what consumes it.
    """
    return {
        "accrual": {
            "editable": False,
            "informational": True,
            "value": list(DEFAULT_ACCRUAL_PRIORITY),
            "consumed_by": None,
            "reason": (
                "Accruals are generated per-category by the schedule builder and "
                "the per-diem interest walk; there is no ordered step for this "
                "list to control, so it is a fixed reference display."
            ),
        },
        "repayment": {
            "editable": True,
            "informational": False,
            "value": list(DEFAULT_REPAYMENT_PRIORITY),
            "consumed_by": "app.services.interest_engine.allocate_regular_payment",
            # The ONLY reordering the engine + guards permit. Everything not
            # listed here is pinned; the UI should render pinned rows as fixed.
            "swappable_groups": [sorted(_SHARED_FEE_BUCKET)],
            "pinned": [c for c in DEFAULT_REPAYMENT_PRIORITY if c not in _SHARED_FEE_BUCKET],
            "reason": (
                "The regular-payment allocator walks this list. Interest must "
                "precede principal, scheduled fees must follow principal, and "
                "NSF must be last (the add-on bucket is unreachable from a "
                "regular payment). origination and administration share one "
                "engine fee bucket, so swapping them is permitted and is a no-op."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Section-level consumer map for the WHOLE policy document.
#
# Audit finding (config-consumers PR): allocation_priority was not the only
# decorative block — most of ProductPolicyConfig is stored, validated and
# rendered but read by nothing. Rather than leave that invisible, every section
# declares its status here so the admin UI can badge it:
#
#   "consumed"       — something reads it; editing changes behaviour.
#   "informational"  — nothing can read it BY DESIGN; render read-only.
#   "pending_consumer" — the vocabulary is captured for Turnkey parity but the
#                      feature behind it is not built. Editing it today changes
#                      nothing. NOT an acceptable resting state; each one is a
#                      tracked build item.
# ---------------------------------------------------------------------------

POLICY_SECTION_STATUS: dict[str, dict] = {
    "visible_to_customer": {
        "status": "pending_consumer",
        "note": "No borrower-facing catalogue filter reads this yet.",
    },
    "schedule_building": {
        "status": "pending_consumer",
        "note": (
            "loan_type / calculation_basis are validated against the engine "
            "(only the implemented values are accepted), but late_grace_days, "
            "loan_phases and enable_customizable_equal_payments have no reader: "
            "the delinquency/DPD path still hard-codes a zero grace period."
        ),
    },
    "allocation_priority": {
        "status": "consumed",
        "note": (
            "repayment is walked by interest_engine.allocate_regular_payment; "
            "accrual is informational (see allocation_priority_field_metadata)."
        ),
    },
    "grace_period": {"status": "pending_consumer", "note": "No schedule-builder reader."},
    "due_dates": {"status": "pending_consumer", "note": "No schedule-builder reader."},
    "due_date_seasons": {
        "status": "pending_consumer",
        "note": "Explicitly deferred by Dave; disabled by default.",
    },
    "payoff": {
        "status": "consumed",
        "note": (
            "payoff_policy_descriptor feeds the payoff quote "
            "(loan_payments/admin_application_process), and the grid is "
            "validated against the engine's fixed payoff math."
        ),
    },
    "disbursement": {"status": "pending_consumer", "note": "No disbursement-path reader."},
    "approval": {"status": "pending_consumer", "note": "No two-level approval step exists."},
    "repayment_modes": {
        "status": "pending_consumer",
        "note": (
            "The four allocator modes exist in the engine, but availability / "
            "is_default / loan_closure / future_installments_recalc are not read "
            "— mode permissioning is enforced at the endpoint layer instead."
        ),
    },
    "application_form_variant": {
        "status": "pending_consumer",
        "note": "ApplicationProcessConfig.form_variants is not yet selected per product.",
    },
}


def policy_config_field_metadata() -> dict:
    """Per-section consumer status for the whole policy document.

    Served by ``GET /credit-products/policy-config/field-metadata`` so the admin
    UI can be honest about which product tabs actually do something today.
    """
    return {
        "sections": POLICY_SECTION_STATUS,
        "allocation_priority_fields": allocation_priority_field_metadata(),
    }


def assert_schedule_consistent_with_engine(cfg: ProductPolicyConfig) -> None:
    """Refuse a schedule-building config the engine cannot compute.

    Our interest engine is daily simple interest on the remaining principal.
    ``LoanType.ANNUITY`` exists for TL parity in the vocabulary but selecting it
    would make the product quote and book on math we do not have.
    """
    sb = cfg.schedule_building
    if sb.loan_type is not LoanType.DAILY_SIMPLE_INTEREST:
        raise ProductPolicyConfigError(
            f"loan_type '{sb.loan_type.value}' is declared for Turnkey parity but "
            "the interest engine only implements daily simple interest on the "
            "remaining principal; use 'daily_simple_interest'"
        )
    if sb.calculation_basis is not CalculationBasis.REMAINING_PRINCIPAL:
        raise ProductPolicyConfigError(
            f"calculation_basis '{sb.calculation_basis.value}' is not implemented; "
            "use 'remaining_principal'"
        )


class LoanTypeChangeError(ProductPolicyConfigError):
    """A product's loan type was changed without the destructive-change confirm."""


def assert_loan_type_change_allowed(
    existing: Optional[dict],
    incoming: ProductPolicyConfig,
    *,
    confirm_loan_type_change: bool = False,
) -> None:
    """Reproduce TL's destructive-change guard on `Loan type`.

    On Dave's screen, changing Loan type raises *"All filled data will be
    removed. Are you sure that you want to change Loan Type?"* — loan type is
    the root discriminator and switching it resets the whole product config. We
    do not silently wipe a saved product; instead the update is REFUSED unless
    the caller passes ``confirm_loan_type_change=true``, which is the API
    equivalent of clicking Confirm. Products with no stored policy_config (the
    overwhelming majority — the column is NULL) are unaffected.
    """
    if not existing:
        return
    prior = (existing.get("schedule_building") or {}).get("loan_type")
    if prior is None:
        return
    if prior == incoming.schedule_building.loan_type.value:
        return
    if confirm_loan_type_change:
        return
    raise LoanTypeChangeError(
        f"changing loan_type from '{prior}' to "
        f"'{incoming.schedule_building.loan_type.value}' resets the product's "
        "schedule configuration; re-send with confirm_loan_type_change=true to "
        "confirm"
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
    is strictly validated AND checked for engine-consistency (payoff grid,
    repayment allocation ordering, schedule building). Raises
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
    assert_allocation_consistent_with_engine(cfg)
    assert_schedule_consistent_with_engine(cfg)
    return cfg
