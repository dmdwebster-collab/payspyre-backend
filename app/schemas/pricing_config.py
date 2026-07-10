"""Typed credit-product pricing configuration (P0 WS-B, Turnkey parity item 7).

Formalizes the previously free-form ``platform_credit_products.pricing_config``
JSONB into a validated Pydantic schema, fixing the known launch blocker where
the product-create side and the quote engine disagreed on the config shape
(``origination_fee_pct`` was written by seeds/tests but NEVER read by
``loan_quote`` — fees silently dropped from every quote and APR disclosure).

Design rules (P0 build plan invariants):
  * Integer cents and integer basis points EVERYWHERE. No floats in money.
  * One product per amount bracket supports MULTIPLE payment frequencies
    (Dave: "four credit products per bracket is completely ridiculous").
  * Late fees stay DISABLED for Canada: the fee types exist in the taxonomy so
    configs are complete, but validation refuses ``enabled=True`` for them
    (default-charge / provincial CPA legality is an open counsel question —
    see docs/turnkey_parity/07__WP_Settings_Part_1.md §4).
  * Back-compat: existing DB rows carry the old free-form shape. The tolerant
    loader (:func:`parse_pricing_config`) maps the old keys onto this schema
    with behavior-preserving defaults and logs a deprecation event. NO
    destructive migration — old rows keep working at read time.

This module is deliberately dependency-free within the app (pydantic + stdlib
only) so both the quote engine (``app/services/loan_quote.py``) and the product
CRUD path can import it without cycles. APR math stays in ``loan_quote``.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

logger = logging.getLogger(__name__)

PRICING_SCHEMA_VERSION = 1

# Legacy fallback rate — mirrors loan_servicing._DEFAULT_ANNUAL_RATE_BPS so a
# product with NO rate information quotes and books at the same 12.99% it
# always has. (New typed configs default to the Turnkey demo 19.99% instead —
# see InterestConfig.)
LEGACY_DEFAULT_RATE_BPS = 1299

# Turnkey demo values (07__WP_Settings_Part_1.md §2.9): default 19.99%/yr,
# bounds 9.99%–23.99%.
DEFAULT_RATE_BPS = 1999
DEFAULT_MIN_RATE_BPS = 999
DEFAULT_MAX_RATE_BPS = 2399


class PricingConfigError(ValueError):
    """A pricing_config payload failed schema or compliance validation."""


# ---------------------------------------------------------------------------
# Payment frequencies — canonical source of truth (loan_quote derives its
# FREQUENCIES table from these so the two can never diverge again).
# ---------------------------------------------------------------------------

class PaymentFrequency(str, Enum):
    WEEKLY = "weekly"
    BI_WEEKLY = "bi_weekly"
    SEMI_MONTHLY = "semi_monthly"
    MONTHLY = "monthly"


PAYMENTS_PER_YEAR: dict[PaymentFrequency, int] = {
    PaymentFrequency.WEEKLY: 52,
    PaymentFrequency.BI_WEEKLY: 26,
    PaymentFrequency.SEMI_MONTHLY: 24,
    PaymentFrequency.MONTHLY: 12,
}

FREQUENCY_LABELS: dict[PaymentFrequency, str] = {
    PaymentFrequency.WEEKLY: "Weekly",
    PaymentFrequency.BI_WEEKLY: "Bi-weekly",
    PaymentFrequency.SEMI_MONTHLY: "Semi-monthly",
    PaymentFrequency.MONTHLY: "Monthly",
}

# Spelling tolerance for the loader (Dave's spec says "biweekly"; Turkey UI
# says "Bi-weekly"; our engine key is "bi_weekly").
_FREQUENCY_ALIASES: dict[str, PaymentFrequency] = {
    "biweekly": PaymentFrequency.BI_WEEKLY,
    "bi-weekly": PaymentFrequency.BI_WEEKLY,
    "semimonthly": PaymentFrequency.SEMI_MONTHLY,
    "semi-monthly": PaymentFrequency.SEMI_MONTHLY,
}


def coerce_frequency(value: str) -> Optional[PaymentFrequency]:
    """Best-effort mapping of a raw frequency string to the enum (or None)."""
    try:
        return PaymentFrequency(value)
    except ValueError:
        return _FREQUENCY_ALIASES.get(str(value).strip().lower())


def payments_in_term(term_months: int, frequency: PaymentFrequency | str) -> int:
    """How many instalments a term spans at the given frequency.

    Canonical implementation — ``loan_quote.num_payments`` delegates here.
    """
    freq = PaymentFrequency(frequency)
    per_year = PAYMENTS_PER_YEAR[freq]
    if freq is PaymentFrequency.MONTHLY:
        return term_months
    if freq is PaymentFrequency.SEMI_MONTHLY:
        return term_months * 2
    # weekly / bi-weekly don't divide months evenly — scale by the year fraction.
    return max(1, round(term_months * per_year / 12))


# ---------------------------------------------------------------------------
# Fee taxonomy — the 14 Turnkey Lender fee types (07__ file §2.9).
# ---------------------------------------------------------------------------

class FeeType(str, Enum):
    ADMINISTRATION = "administration"            # demoed: $1 per payment
    DISBURSEMENT = "disbursement"                # "we don't charge" — kept for parity
    DOWN_PAYMENT = "down_payment"
    LATE = "late"                                # MUST stay disabled (Canada policy)
    LATE_UNPAID_DUE = "late_unpaid_due"          # MUST stay disabled (Canada policy)
    NSF = "nsf"                                  # demoed: $45, on NSF event, add-on
    ORIGINATION = "origination"                  # demoed: $25, first installment
    PAST_DUE_INTEREST = "past_due_interest"
    PAYMENT_HOLIDAY_INTEREST = "payment_holiday_interest"
    PAYOFF = "payoff"
    PRE_APPROVAL = "pre_approval"
    PRE_DISBURSEMENT = "pre_disbursement"
    REPAYMENT = "repayment"
    SALES_TAX = "sales_tax"


#: Fee types that must remain DISABLED until counsel confirms legality of late
#: fees vs NSF-only default charges under provincial Consumer Protection Acts.
LATE_FEE_TYPES = frozenset({FeeType.LATE, FeeType.LATE_UNPAID_DUE})


class FeeCalc(str, Enum):
    FIXED_CENTS = "fixed_cents"   # ``amount`` is integer cents
    RATE_BPS = "rate_bps"         # ``amount`` is integer basis points of principal


class ChargeTiming(str, Enum):
    PER_PAYMENT = "per_payment"        # charged on every scheduled installment
    AT_ORIGINATION = "at_origination"  # charged once (first installment)
    ON_EVENT = "on_event"              # contingent (NSF etc.) — EXCLUDED from APR


class FeeConfig(BaseModel):
    """One fee definition on a credit product.

    A later workstream consumes these to actually CHARGE fees at runtime; this
    schema only defines them and feeds the APR/cost-of-borrowing math.
    """

    model_config = ConfigDict(extra="forbid")

    fee_type: FeeType
    calc: FeeCalc
    amount: int = Field(..., ge=0, description="Integer cents (fixed_cents) or basis points (rate_bps)")
    charge_timing: ChargeTiming
    add_on: bool = Field(
        default=False,
        description="True = accrues to the non-interest-bearing add-on bucket (e.g. NSF)",
    )
    enabled: bool = True
    per_frequency_amounts: Optional[dict[PaymentFrequency, int]] = Field(
        default=None,
        description=(
            "Optional per-payment-frequency override of `amount` (same unit as "
            "`calc`). Dave: a $1/payment fee distorts APR far more at weekly than "
            "monthly, so fees must be adjustable per repayment period."
        ),
    )

    @model_validator(mode="after")
    def _check(self) -> "FeeConfig":
        if self.fee_type in LATE_FEE_TYPES and self.enabled:
            raise ValueError(
                f"Fee type '{self.fee_type.value}' must remain disabled in Canada "
                "(late-fee legality vs NSF-only default charges is an open counsel "
                "question). Include it with enabled=false or omit it."
            )
        if self.per_frequency_amounts:
            for freq, amt in self.per_frequency_amounts.items():
                if amt < 0:
                    raise ValueError(
                        f"per_frequency_amounts[{freq.value}] must be >= 0"
                    )
        return self

    def amount_for(self, frequency: PaymentFrequency) -> int:
        """The effective amount at a frequency (per-frequency override wins)."""
        if self.per_frequency_amounts and frequency in self.per_frequency_amounts:
            return self.per_frequency_amounts[frequency]
        return self.amount


class InterestConfig(BaseModel):
    """Interest rate: platform default + the bounds staff may price within.

    ``rate_edit_roles`` = which staff roles may set a non-default rate (within
    [min, max]) on a specific application. Enforcement of the role gate happens
    where rates are set (underwriting/offer editing — later workstream); this
    schema carries the policy.
    """

    model_config = ConfigDict(extra="forbid")

    annual_rate_bps: int = Field(default=DEFAULT_RATE_BPS, ge=0, le=10_000)
    min_rate_bps: int = Field(default=DEFAULT_MIN_RATE_BPS, ge=0, le=10_000)
    max_rate_bps: int = Field(default=DEFAULT_MAX_RATE_BPS, ge=0, le=10_000)
    rate_edit_roles: list[str] = Field(default_factory=lambda: ["admin"])

    @model_validator(mode="after")
    def _bounds(self) -> "InterestConfig":
        if not (self.min_rate_bps <= self.annual_rate_bps <= self.max_rate_bps):
            raise ValueError(
                "interest bounds must satisfy min_rate_bps <= annual_rate_bps "
                f"<= max_rate_bps (got {self.min_rate_bps} <= "
                f"{self.annual_rate_bps} <= {self.max_rate_bps})"
            )
        return self


class PricingConfig(BaseModel):
    """The typed shape of ``platform_credit_products.pricing_config``.

    Amount/term fields are optional because the product row's
    ``min_amount_cents``/``max_amount_cents`` columns remain authoritative for
    amounts; when set here they add config-internal consistency and extra APR
    boundary points at validation time.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=PRICING_SCHEMA_VERSION)
    interest: Optional[InterestConfig] = Field(
        default=None,
        description=(
            "None = unconfigured; readers fall back to the legacy 12.99% platform "
            "default. Product create/PATCH normalization fills this in."
        ),
    )
    payment_frequencies: list[PaymentFrequency] = Field(
        default_factory=lambda: [PaymentFrequency.MONTHLY], min_length=1
    )
    amount_min_cents: Optional[int] = Field(default=None, gt=0)
    amount_max_cents: Optional[int] = Field(default=None, gt=0)
    term_min_months: Optional[int] = Field(default=None, gt=0)
    term_max_months: Optional[int] = Field(default=None, gt=0)
    term_options: Optional[list[int]] = Field(
        default=None, description="Discrete term choices offered by the UI slider"
    )
    default_term_months: Optional[int] = Field(
        default=None, gt=0, description="Booking fallback term when a decision carries none"
    )
    fees: list[FeeConfig] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _version(cls, v: int) -> int:
        if v != PRICING_SCHEMA_VERSION:
            raise ValueError(f"Unsupported pricing_config schema_version {v} (expected {PRICING_SCHEMA_VERSION})")
        return v

    @field_validator("payment_frequencies")
    @classmethod
    def _dedupe_frequencies(cls, v: list[PaymentFrequency]) -> list[PaymentFrequency]:
        seen: list[PaymentFrequency] = []
        for f in v:
            if f not in seen:
                seen.append(f)
        return seen

    @field_validator("term_options")
    @classmethod
    def _term_options(cls, v: Optional[list[int]]) -> Optional[list[int]]:
        if v is None:
            return None
        if not v:
            return None
        if any(int(t) <= 0 for t in v):
            raise ValueError("term_options must all be positive months")
        return sorted({int(t) for t in v})

    @model_validator(mode="after")
    def _cross_checks(self) -> "PricingConfig":
        if (
            self.amount_min_cents is not None
            and self.amount_max_cents is not None
            and self.amount_min_cents > self.amount_max_cents
        ):
            raise ValueError("amount_min_cents must be <= amount_max_cents")
        if (
            self.term_min_months is not None
            and self.term_max_months is not None
            and self.term_min_months > self.term_max_months
        ):
            raise ValueError("term_min_months must be <= term_max_months")
        if self.default_term_months is not None:
            if self.term_min_months is not None and self.default_term_months < self.term_min_months:
                raise ValueError("default_term_months is below term_min_months")
            if self.term_max_months is not None and self.default_term_months > self.term_max_months:
                raise ValueError("default_term_months is above term_max_months")
        seen: set[tuple] = set()
        for fee in self.fees:
            key = (fee.fee_type, fee.calc, fee.charge_timing)
            if key in seen:
                raise ValueError(
                    f"Duplicate fee definition: {fee.fee_type.value}/"
                    f"{fee.calc.value}/{fee.charge_timing.value}"
                )
            seen.add(key)
        return self


# ---------------------------------------------------------------------------
# Fee math for quoting / APR (cost of borrowing "C" under SOR/2001-104 s.3)
# ---------------------------------------------------------------------------

def quote_fees_cents(
    cfg: PricingConfig,
    amount_cents: int,
    term_months: int,
    frequency: PaymentFrequency | str,
) -> int:
    """Total non-contingent fees over the term for one selection, in cents.

    This is the fee component of the cost of borrowing (C) for the Canadian
    regulatory APR. ``on_event`` fees (NSF etc.) are contingent default charges
    and are EXCLUDED from the cost of borrowing; disabled fees are excluded.

      * fixed_cents + at_origination  -> amount once
      * fixed_cents + per_payment     -> amount x number of scheduled payments
      * rate_bps    + at_origination  -> round(principal x bps / 10_000) once
      * rate_bps    + per_payment     -> that value on every scheduled payment
    """
    freq = PaymentFrequency(frequency)
    n = payments_in_term(term_months, freq)
    total = 0
    for fee in cfg.fees:
        if not fee.enabled or fee.charge_timing is ChargeTiming.ON_EVENT:
            continue
        amt = fee.amount_for(freq)
        if fee.calc is FeeCalc.RATE_BPS:
            value = round(amount_cents * amt / 10_000)
        else:
            value = amt
        total += value * (n if fee.charge_timing is ChargeTiming.PER_PAYMENT else 1)
    return int(total)


def origination_lump_fees_cents(cfg: PricingConfig) -> int:
    """Back-compat lump: the fixed at-origination fee total in cents.

    Matches exactly what the pre-schema ``pricing_config["fees_cents"]`` key
    could express (a single fixed lump); rate-based and per-payment fees are
    NOT representable as an amount-independent lump and are excluded here.
    Selection-aware callers must use :func:`quote_fees_cents` instead.
    """
    return sum(
        fee.amount
        for fee in cfg.fees
        if fee.enabled
        and fee.calc is FeeCalc.FIXED_CENTS
        and fee.charge_timing is ChargeTiming.AT_ORIGINATION
    )


# ---------------------------------------------------------------------------
# Tolerant loader for the legacy free-form JSONB shape (NO destructive
# migration — old DB rows parse at read time).
# ---------------------------------------------------------------------------

# Keys that mark a payload as already-typed. Legacy configs never carried any
# of these ("payment_frequencies" existed in BOTH shapes so it is not a marker).
_TYPED_MARKERS = ("schema_version", "interest", "fees")

# Legacy keys the loader understands (used for the deprecation log).
_LEGACY_KEYS = (
    "apr_bps", "annual_rate_bps", "apr_range", "fees_cents",
    "origination_fee_pct", "term_options", "term_months", "term_min",
    "term_max", "payment_frequencies",
)

# De-spam guard: warn once per distinct legacy key-signature per process.
_warned_legacy_signatures: set[frozenset] = set()


def parse_pricing_config(raw: Optional[dict], *, context: str = "") -> PricingConfig:
    """Parse a stored/submitted ``pricing_config`` into the typed schema.

    * ``None`` / ``{}``            -> unconfigured defaults (legacy semantics:
      all four frequencies, no explicit rate — readers fall back to 12.99%).
    * Typed shape (has one of ``schema_version``/``interest``/``fees``)
      -> strict validation (unknown keys rejected).
    * Anything else                -> tolerant legacy mapping + deprecation log.

    Raises :class:`PricingConfigError` on invalid payloads.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise PricingConfigError(f"pricing_config must be an object, got {type(raw).__name__}")

    if any(k in raw for k in _TYPED_MARKERS):
        try:
            return PricingConfig.model_validate(raw)
        except ValidationError as exc:
            raise PricingConfigError(f"pricing_config failed schema validation: {exc}") from exc

    return _from_legacy(raw, context=context)


def _from_legacy(raw: dict, *, context: str = "") -> PricingConfig:
    """Map the old free-form shape onto the typed schema, preserving the exact
    read-time behavior the quote/booking engines had for each legacy key."""
    legacy_keys = [k for k in _LEGACY_KEYS if k in raw]
    if legacy_keys:
        sig = frozenset(legacy_keys)
        if sig not in _warned_legacy_signatures:
            _warned_legacy_signatures.add(sig)
            logger.warning(
                "deprecated legacy pricing_config shape (keys=%s)%s — parsed via "
                "tolerant loader; re-save the product to normalize to the typed schema",
                sorted(legacy_keys),
                f" [{context}]" if context else "",
            )

    # --- interest: apr_bps > annual_rate_bps > apr_range floor (PERCENT) -----
    interest: Optional[InterestConfig] = None
    rate = raw.get("apr_bps")
    if rate is None:
        rate = raw.get("annual_rate_bps")
    apr_range = raw.get("apr_range") or None
    min_bps = max_bps = None
    if apr_range:
        try:
            min_bps = int(round(float(apr_range[0]) * 100))
            max_bps = int(round(float(apr_range[-1]) * 100))
        except (TypeError, ValueError, IndexError) as exc:
            raise PricingConfigError(f"Invalid legacy apr_range {apr_range!r}") from exc
        if rate is None:
            rate = min_bps  # floor — matches loan_quote/loan_servicing resolution
    if rate is not None:
        rate = int(rate)
        interest = InterestConfig(
            annual_rate_bps=rate,
            min_rate_bps=min(min_bps, rate) if min_bps is not None else rate,
            max_rate_bps=max(max_bps, rate) if max_bps is not None else rate,
        )

    # --- terms ----------------------------------------------------------------
    term_options = raw.get("term_options")
    if isinstance(term_options, int):
        term_options = [term_options]
    default_term = raw.get("term_months")
    if default_term is not None:
        default_term = int(default_term)
        if not term_options:
            term_options = [default_term]
    term_min = raw.get("term_min")
    term_max = raw.get("term_max")
    if term_min is not None and term_max is not None and int(term_min) > int(term_max):
        term_min, term_max = term_max, term_min  # old product_terms swapped, keep tolerant

    # --- frequencies: absent/empty legacy config offered ALL four --------------
    raw_freqs = raw.get("payment_frequencies")
    if raw_freqs:
        freqs = [f for f in (coerce_frequency(v) for v in raw_freqs) if f is not None]
        if not freqs:
            freqs = list(PaymentFrequency)
    else:
        freqs = list(PaymentFrequency)

    # --- fees -------------------------------------------------------------------
    fees: list[FeeConfig] = []
    fees_cents = raw.get("fees_cents")
    if fees_cents:
        fees.append(
            FeeConfig(
                fee_type=FeeType.ORIGINATION,
                calc=FeeCalc.FIXED_CENTS,
                amount=int(fees_cents),
                charge_timing=ChargeTiming.AT_ORIGINATION,
            )
        )
    orig_pct = raw.get("origination_fee_pct")
    if orig_pct:
        fees.append(
            FeeConfig(
                fee_type=FeeType.ORIGINATION,
                calc=FeeCalc.RATE_BPS,
                amount=int(round(float(orig_pct) * 10_000)),
                charge_timing=ChargeTiming.AT_ORIGINATION,
            )
        )

    try:
        return PricingConfig(
            interest=interest,
            payment_frequencies=freqs,
            term_min_months=int(term_min) if term_min is not None else None,
            term_max_months=int(term_max) if term_max is not None else None,
            term_options=[int(t) for t in term_options] if term_options else None,
            default_term_months=default_term,
            fees=fees,
        )
    except ValidationError as exc:
        raise PricingConfigError(f"Legacy pricing_config failed validation: {exc}") from exc
