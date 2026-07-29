"""Origination constraints + preliminary quote — the credit product AS the
calculation backbone of the New Application form.

Dave's mandate (2026-07 Originations review): *"the Credit Product is the
calculation backbone of the platform, providing the guardrails and information
required to accurately calculate interest, fees, payment amounts, cost of
borrowing"*. Concretely, the New Application form must be **fully driven** by
the selected product:

  * every field's default / minimum / maximum comes from the product, and an
    out-of-range entry is REFUSED with a field-level error (never a generic 422
    the form can't attach to an input);
  * the form shows a live "Approximate <frequency> Payment", the disclosure
    line **Principal + Interest + Fees = Total (APR)**, and a preliminary
    amortization schedule (`#, Date, Payment, Principal, Interest, Fees,
    Balance`) that BECOMES the booked schedule if the loan proceeds.

This module is the single place those guardrails are resolved and enforced. It
is consumed by:

  * ``GET  /admin/origination/constraints`` — everything the form needs to
    render (bounds, defaults, options, date windows, vendor providers, the
    products available for the vendor/provider);
  * ``POST /admin/origination/quote`` — the live calculation;
  * the application-CREATE paths (``POST /admin/customer-profiles/{id}/
    applications`` and ``POST /clinic/v1/applications``), so the same rules
    cannot be bypassed by calling the API directly.

NO SECOND ENGINE. Every number here comes from the existing engines:

  * instalment + per-period amortization + totals: ``loan_quote.quote_loan``
    (whose monthly walk is arithmetically identical to
    ``loan_servicing.generate_amortization_schedule``, the 30/360 booking
    schedule — asserted in ``tests/test_origination_constraints.py``);
  * fee allocation: ``pricing_config.fee_rows_cents`` (the one fee
    implementation; ``quote_fees_cents`` is its sum);
  * APR: ``loan_quote.compute_apr_bps`` — the Canadian **Cost of Borrowing
    Regulations (SOR/2001-104) s.3-4** figure, ``APR = C / (T x P) x 100``,
    which is NOT the nominal annual interest rate whenever the product charges
    any non-contingent fee;
  * criminal-rate guard: ``loan_quote.exceeds_criminal_rate`` (Criminal Code
    s.347).

POLICY CONFIG CONSUMED HERE (previously stored-but-unread — this module is the
first real reader; see ``POLICY_SECTION_STATUS`` in
``app/schemas/product_policy_config.py``):

  * ``due_dates.default_start_shift_days`` -> the default Start Date is
    ``today + shift``, and that is also the earliest start date accepted;
  * ``due_dates.use_change_start_date``    -> whether the form may move it;
  * ``due_dates.first_due_min_days`` / ``first_due_max_days`` -> the allowed
    custom first-payment window, measured from the start date, ENFORCED on both
    the quote and the create paths;
  * ``due_dates.use_change_first_due_date`` -> whether a custom first payment
    date is accepted at all;
  * ``schedule_building.loan_type`` / ``calculation_basis`` -> selects the
    schedule basis (and only the implemented one is accepted);
  * ``repayment_modes.modes[].is_default`` / ``.availability`` and
    ``future_installments_recalc`` -> the form's repayment-mode selector.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.models.loan import Vendor
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.schemas.pricing_config import (
    FREQUENCY_LABELS,
    PAYMENTS_PER_YEAR,
    PaymentFrequency,
    PricingConfig,
    coerce_frequency,
    fee_rows_cents,
    parse_pricing_config,
    payments_in_term,
)
from app.schemas.product_policy_config import ProductPolicyConfig
from app.services import loan_quote
from app.services.product_policy import policy_for_product
from app.services.servicing_status import step_due_date

__all__ = [
    "ConstraintViolation",
    "ConstraintsResponse",
    "FieldError",
    "OriginationConstraints",
    "OriginationQuote",
    "ProductPickList",
    "SelectionInput",
    "assert_selection_allowed",
    "build_quote",
    "enforce_on_create",
    "product_pick_list",
    "resolve_constraints",
    "schedule_dates",
    "validate_selection",
]


# ---------------------------------------------------------------------------
# Field-level errors (the form highlights the offending input)
# ---------------------------------------------------------------------------


class FieldError(BaseModel):
    """One rejected input, addressed to the form field that produced it."""

    model_config = ConfigDict(extra="forbid")

    field: str
    code: str
    message: str
    #: Echo of the guardrail that was breached, so the form can render the
    #: bound next to the input without a second round trip.
    min: Optional[Any] = None
    max: Optional[Any] = None
    allowed: Optional[list[Any]] = None


class ConstraintViolation(Exception):
    """One or more inputs fell outside the credit product's guardrails."""

    def __init__(self, errors: list[FieldError]):
        self.errors = errors
        super().__init__("; ".join(f"{e.field}: {e.message}" for e in errors))

    def as_detail(self) -> dict:
        """FastAPI ``detail`` body: a machine-readable, per-field error list.

        ``mode="json"`` because the bound echo carries dates for the date
        fields, and a FastAPI ``detail`` is serialized with plain ``json``.
        """
        return {
            "message": "One or more values fall outside the credit product's limits.",
            "errors": [e.model_dump(mode="json", exclude_none=True) for e in self.errors],
        }


# ---------------------------------------------------------------------------
# Date arithmetic per payment frequency
# ---------------------------------------------------------------------------

def advance(base: date, frequency: PaymentFrequency | str, periods: int) -> date:
    """``base`` advanced by ``periods`` payment periods at ``frequency``.

    ONE stepping implementation, shared with the booking engine and the servicing
    model: this delegates to :func:`servicing_status.step_due_date`, the CEO's
    own rule (Weekly +7d, Bi-Weekly +14d, Monthly EDATE with the Jan-31 ->
    Feb-28 day clamp, Semi-Monthly EDATE(n//2) then +15d on the odd steps).
    A preliminary schedule and the booked one therefore land on the same dates
    by construction rather than by two implementations agreeing.
    """
    return step_due_date(base, periods, frequency)


def schedule_dates(
    first_payment_date: date, frequency: PaymentFrequency | str, count: int
) -> list[date]:
    """Due dates for ``count`` installments starting at ``first_payment_date``."""
    return [advance(first_payment_date, frequency, i) for i in range(count)]


# ---------------------------------------------------------------------------
# The resolved guardrails
# ---------------------------------------------------------------------------


class AmountConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_cents: int
    max_cents: int
    default_cents: int
    currency: str = "CAD"


class TermConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_months: int
    max_months: int
    default_months: int
    #: Discrete choices the product offers, when it restricts to a list.
    options: Optional[list[int]] = None


class RateConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_bps: int
    max_bps: int
    default_bps: int
    #: Roles permitted to move off ``default_bps`` (``interest.rate_edit_roles``).
    edit_roles: list[str] = Field(default_factory=list)
    #: False when min == max: the product prices at a single rate.
    editable: bool = True


class FrequencyOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    label: str
    payments_per_year: int


class FrequencyConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    options: list[FrequencyOption]
    default: str


class DateWindow(BaseModel):
    """A date field's default plus its inclusive allowed window.

    ``*_offset_days`` state the rule (relative to today for the start date,
    relative to the start date for the first payment); the absolute dates are
    the same rule resolved against ``as_of`` / the chosen start date, so the
    form can bound its date picker directly.
    """

    model_config = ConfigDict(extra="forbid")

    default: date
    min: date
    max: Optional[date] = None
    min_offset_days: int
    max_offset_days: Optional[int] = None
    #: False = the product forbids moving this date (`use_change_*` is off).
    editable: bool = True
    relative_to: str


class ScheduleBasis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loan_type: str
    calculation_basis: str
    #: The day-count the preliminary (and booked) schedule uses.
    day_count: str = "30/360"
    late_grace_days: int = 0


class RepaymentModeOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    description: Optional[str] = None
    availability: list[str] = Field(default_factory=list)


class RepaymentModesView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    options: list[RepaymentModeOption]
    default: Optional[str] = None
    future_installments_recalc: str
    loan_closure: str


class FeeLine(BaseModel):
    """One configured, non-contingent fee — the Fees column's provenance."""

    model_config = ConfigDict(extra="forbid")

    fee_type: str
    calc: str
    amount: int
    charge_timing: str
    contingent: bool = False


class OriginationConstraints(BaseModel):
    """Everything the New Application form needs for ONE credit product."""

    model_config = ConfigDict(extra="forbid")

    credit_product_id: UUID
    credit_product_code: str
    credit_product_name: str
    credit_product_version: int
    as_of: date
    amount: AmountConstraint
    term: TermConstraint
    rate: RateConstraint
    frequency: FrequencyConstraint
    start_date: DateWindow
    first_payment_date: DateWindow
    schedule: ScheduleBasis
    repayment_modes: RepaymentModesView
    fees: list[FeeLine] = Field(default_factory=list)
    #: Which config key each guardrail was resolved from — so the admin UI can
    #: point staff at the product tab that owns a limit they want changed.
    sources: dict[str, str] = Field(default_factory=dict)


class ProviderOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    application_count: int


class ProductOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credit_product_id: UUID
    code: str
    name: str
    min_amount_cents: int
    max_amount_cents: int
    currency: str
    is_default: bool = False


class ProductPickList(BaseModel):
    """The vendor -> provider -> product cascade for the form's selectors."""

    model_config = ConfigDict(extra="forbid")

    vendor_id: Optional[UUID] = None
    vendor_name: Optional[str] = None
    providers: list[ProviderOption] = Field(default_factory=list)
    #: True when the vendor has exactly one provider — the UI auto-selects and
    #: locks the field.
    single_provider: bool = False
    provider: Optional[str] = None
    products: list[ProductOption] = Field(default_factory=list)
    #: True when exactly one product is available — auto-select and lock.
    single_product: bool = False
    default_credit_product_id: Optional[UUID] = None


class ConstraintsResponse(BaseModel):
    """The ONE call the New Application form drives off."""

    model_config = ConfigDict(extra="forbid")

    selection: ProductPickList
    constraints: Optional[OriginationConstraints] = None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _pricing(product: PlatformCreditProduct) -> PricingConfig:
    """The product's typed pricing config (tolerant of the legacy shape)."""
    return parse_pricing_config(product.pricing_config, context="origination constraints")


def _amount_bounds(product: PlatformCreditProduct, cfg: PricingConfig) -> tuple[int, int]:
    """The product row's amount columns, tightened by the config's own bounds.

    The columns stay authoritative (that is the pre-existing rule every
    origination path already applies); a config that also declares bounds can
    only narrow the window, never widen it.
    """
    lo = int(product.min_amount_cents)
    hi = int(product.max_amount_cents)
    if cfg.amount_min_cents is not None:
        lo = max(lo, cfg.amount_min_cents)
    if cfg.amount_max_cents is not None:
        hi = min(hi, cfg.amount_max_cents)
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def _default_frequency(cfg: PricingConfig) -> PaymentFrequency:
    """The frequency the form pre-selects.

    ``pricing_config.default_payment_frequency`` when set (the field this
    workstream ADDED — the schema previously had nowhere to say which of the
    enabled frequencies is the default). Otherwise Monthly if the product
    offers it, else the first enabled frequency. Monthly is preferred over
    "first in the list" because the tolerant legacy loader synthesises the full
    four-frequency list for every pre-schema product row, in an order that
    means nothing — defaulting those to Weekly would be an accident, not a
    product decision.
    """
    if cfg.default_payment_frequency is not None and (
        cfg.default_payment_frequency in cfg.payment_frequencies
    ):
        return cfg.default_payment_frequency
    if PaymentFrequency.MONTHLY in cfg.payment_frequencies:
        return PaymentFrequency.MONTHLY
    return cfg.payment_frequencies[0]


def resolve_constraints(
    product: PlatformCreditProduct,
    *,
    as_of: Optional[date] = None,
    policy: Optional[ProductPolicyConfig] = None,
) -> OriginationConstraints:
    """Resolve every New-Application guardrail from ONE credit product.

    Pure apart from reading the product row: no DB writes, no clock beyond
    ``as_of`` (injected so tests are deterministic).
    """
    today = as_of or date.today()
    cfg = _pricing(product)
    pol = policy if policy is not None else policy_for_product(product)

    amount_min, amount_max = _amount_bounds(product, cfg)
    amount_default = cfg.default_amount_cents or amount_min
    amount_default = min(max(amount_default, amount_min), amount_max)

    term_min, term_max, term_options = loan_quote._term_bounds(cfg)
    term_default = cfg.default_term_months or term_min
    term_default = min(max(term_default, term_min), term_max)

    interest = loan_quote._rate_bounds(cfg)

    freqs = list(cfg.payment_frequencies)
    default_freq = _default_frequency(cfg)

    # --- due-date policy: the previously-unread `due_dates` section ----------
    dd = pol.due_dates
    start_default = today + timedelta(days=dd.default_start_shift_days)
    start_window = DateWindow(
        default=start_default,
        min=start_default,
        max=None,
        min_offset_days=dd.default_start_shift_days,
        max_offset_days=None,
        editable=dd.use_change_start_date,
        relative_to="today",
    )
    first_min = start_default + timedelta(days=dd.first_due_min_days)
    first_max = start_default + timedelta(days=dd.first_due_max_days)
    natural_first = advance(start_default, default_freq, 1)
    first_window = DateWindow(
        default=min(max(natural_first, first_min), first_max),
        min=first_min,
        max=first_max,
        min_offset_days=dd.first_due_min_days,
        max_offset_days=dd.first_due_max_days,
        editable=dd.use_change_first_due_date,
        relative_to="start_date",
    )

    sb = pol.schedule_building
    rm = pol.repayment_modes
    default_mode = next((m.key for m in rm.modes if m.is_default), None)

    return OriginationConstraints(
        credit_product_id=product.id,
        credit_product_code=product.code,
        credit_product_name=product.name,
        credit_product_version=int(getattr(product, "version", 1) or 1),
        as_of=today,
        amount=AmountConstraint(
            min_cents=amount_min,
            max_cents=amount_max,
            default_cents=amount_default,
            currency=product.currency or "CAD",
        ),
        term=TermConstraint(
            min_months=term_min,
            max_months=term_max,
            default_months=term_default,
            options=list(cfg.term_options) if cfg.term_options else None,
        ),
        rate=RateConstraint(
            min_bps=interest.min_rate_bps,
            max_bps=interest.max_rate_bps,
            default_bps=interest.annual_rate_bps,
            edit_roles=list(interest.rate_edit_roles or []),
            editable=interest.min_rate_bps != interest.max_rate_bps,
        ),
        frequency=FrequencyConstraint(
            options=[
                FrequencyOption(
                    value=f.value,
                    label=FREQUENCY_LABELS[f],
                    payments_per_year=PAYMENTS_PER_YEAR[f],
                )
                for f in freqs
            ],
            default=default_freq.value,
        ),
        start_date=start_window,
        first_payment_date=first_window,
        schedule=ScheduleBasis(
            loan_type=sb.loan_type.value,
            calculation_basis=sb.calculation_basis.value,
            late_grace_days=sb.late_grace_days,
        ),
        repayment_modes=RepaymentModesView(
            options=[
                RepaymentModeOption(
                    key=m.key,
                    name=m.name,
                    description=m.description,
                    availability=[a.value for a in m.availability],
                )
                for m in rm.modes
            ],
            default=default_mode,
            future_installments_recalc=rm.future_installments_recalc.value,
            loan_closure=rm.loan_closure.value,
        ),
        fees=[
            FeeLine(
                fee_type=f.fee_type.value,
                calc=f.calc.value,
                amount=f.amount_for(default_freq),
                charge_timing=f.charge_timing.value,
                contingent=f.charge_timing.value == "on_event",
            )
            for f in cfg.fees
            if f.enabled
        ],
        sources={
            "amount": "credit_product.min/max_amount_cents + pricing_config.amount_*",
            "term": "pricing_config.term_min_months/term_max_months/term_options",
            "rate": "pricing_config.interest",
            "frequency": "pricing_config.payment_frequencies/default_payment_frequency",
            "start_date": "policy_config.due_dates.default_start_shift_days",
            "first_payment_date": (
                "policy_config.due_dates.first_due_min_days/first_due_max_days"
            ),
            "schedule": "policy_config.schedule_building",
            "repayment_modes": "policy_config.repayment_modes",
            "fees": "pricing_config.fees",
        },
    )


# ---------------------------------------------------------------------------
# The vendor -> provider -> product cascade
# ---------------------------------------------------------------------------


def _province_ok(vendor: Optional[Vendor], product: PlatformCreditProduct) -> bool:
    """Province gate — fail-OPEN unless both sides state a comparable code.

    ``vendors.province`` is free text (it may hold "BC" or "British Columbia"),
    so we only filter when it is a 2-letter code AND the product restricts its
    provinces. Anything else keeps today's behaviour: the product is offered.
    """
    provinces = product.provinces
    if not provinces or not isinstance(provinces, list):
        return True
    code = (getattr(vendor, "province", None) or "").strip().upper()
    if len(code) != 2:
        return True
    return code in {str(p).strip().upper() for p in provinces}


def product_pick_list(
    db: Session,
    *,
    vendor_id: Optional[UUID] = None,
    provider: Optional[str] = None,
) -> ProductPickList:
    """The vendor's providers and the credit products available to them.

    ``single_provider`` / ``single_product`` tell the UI when to auto-select and
    lock a selector (Dave: with one product there is nothing to choose).
    ``default_credit_product_id`` is the product this vendor/provider has used
    most (ties broken by product code), else the only available product.
    """
    vendor: Optional[Vendor] = None
    providers: list[ProviderOption] = []
    if vendor_id is not None:
        vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
        rows = (
            db.query(PlatformCreditApplication.provider_name)
            .filter(PlatformCreditApplication.vendor_id == vendor_id)
            .all()
        )
        counts: dict[str, int] = {}
        for (name,) in rows:
            if name:
                counts[name] = counts.get(name, 0) + 1
        providers = [
            ProviderOption(name=n, application_count=c)
            for n, c in sorted(counts.items(), key=lambda kv: kv[0])
        ]

    products = [
        p
        for p in (
            db.query(PlatformCreditProduct)
            .filter(PlatformCreditProduct.status == "active")
            .order_by(PlatformCreditProduct.code)
            .all()
        )
        if _province_ok(vendor, p)
    ]

    # Usage-derived default: what this vendor (and provider, when given) books.
    usage: dict[UUID, int] = {}
    if vendor_id is not None:
        q = db.query(PlatformCreditApplication.credit_product_id).filter(
            PlatformCreditApplication.vendor_id == vendor_id
        )
        if provider:
            q = q.filter(PlatformCreditApplication.provider_name == provider)
        for (pid,) in q.all():
            if pid is not None:
                usage[pid] = usage.get(pid, 0) + 1

    default_id: Optional[UUID] = None
    if len(products) == 1:
        default_id = products[0].id
    elif products:
        ranked = sorted(products, key=lambda p: (-usage.get(p.id, 0), p.code))
        if usage.get(ranked[0].id, 0) > 0:
            default_id = ranked[0].id

    return ProductPickList(
        vendor_id=vendor_id,
        vendor_name=getattr(vendor, "business_name", None),
        providers=providers,
        single_provider=len(providers) == 1,
        provider=provider or (providers[0].name if len(providers) == 1 else None),
        products=[
            ProductOption(
                credit_product_id=p.id,
                code=p.code,
                name=p.name,
                min_amount_cents=int(p.min_amount_cents),
                max_amount_cents=int(p.max_amount_cents),
                currency=p.currency or "CAD",
                is_default=p.id == default_id,
            )
            for p in products
        ],
        single_product=len(products) == 1,
        default_credit_product_id=default_id,
    )


# ---------------------------------------------------------------------------
# Validation — ONE validator, shared by the quote and the create paths
# ---------------------------------------------------------------------------


class SelectionInput(BaseModel):
    """What the form (or an API caller) chose. Every field optional so the
    create paths can validate only what they carry."""

    model_config = ConfigDict(extra="forbid")

    amount_cents: Optional[int] = None
    term_months: Optional[int] = None
    annual_rate_bps: Optional[int] = None
    frequency: Optional[str] = None
    start_date: Optional[date] = None
    first_payment_date: Optional[date] = None


def validate_selection(
    constraints: OriginationConstraints, selection: SelectionInput
) -> list[FieldError]:
    """Check a selection against the product's guardrails.

    Returns EVERY breach (not just the first) so the form can highlight all the
    offending inputs at once. A ``None`` field is skipped: it means "not chosen
    here", and the caller's own defaults apply.
    """
    errors: list[FieldError] = []
    a = constraints.amount
    if selection.amount_cents is not None and not (
        a.min_cents <= selection.amount_cents <= a.max_cents
    ):
        errors.append(
            FieldError(
                field="amount_cents",
                code="amount_out_of_range",
                message=(
                    f"Amount must be between {a.min_cents / 100:,.2f} and "
                    f"{a.max_cents / 100:,.2f} {a.currency} for "
                    f"{constraints.credit_product_name}."
                ),
                min=a.min_cents,
                max=a.max_cents,
            )
        )

    t = constraints.term
    if selection.term_months is not None:
        if not (t.min_months <= selection.term_months <= t.max_months):
            errors.append(
                FieldError(
                    field="term_months",
                    code="term_out_of_range",
                    message=(
                        f"Term must be between {t.min_months} and {t.max_months} "
                        "months for this product."
                    ),
                    min=t.min_months,
                    max=t.max_months,
                    allowed=list(t.options) if t.options else None,
                )
            )
        elif t.options and selection.term_months not in t.options:
            errors.append(
                FieldError(
                    field="term_months",
                    code="term_not_offered",
                    message=(
                        f"This product only offers terms of "
                        f"{', '.join(str(o) for o in t.options)} months."
                    ),
                    min=t.min_months,
                    max=t.max_months,
                    allowed=list(t.options),
                )
            )

    r = constraints.rate
    if selection.annual_rate_bps is not None and not (
        r.min_bps <= selection.annual_rate_bps <= r.max_bps
    ):
        errors.append(
            FieldError(
                field="annual_rate_bps",
                code="rate_out_of_band",
                message=(
                    f"Interest rate must be between {r.min_bps / 100:.2f}% and "
                    f"{r.max_bps / 100:.2f}% for this product."
                ),
                min=r.min_bps,
                max=r.max_bps,
            )
        )

    allowed_freqs = [o.value for o in constraints.frequency.options]
    resolved_freq = None
    if selection.frequency is not None:
        coerced = coerce_frequency(selection.frequency)
        if coerced is None or coerced.value not in allowed_freqs:
            errors.append(
                FieldError(
                    field="frequency",
                    code="frequency_not_offered",
                    message=(
                        f"Payment frequency '{selection.frequency}' is not offered by "
                        f"this product (allowed: {', '.join(allowed_freqs)})."
                    ),
                    allowed=allowed_freqs,
                )
            )
        else:
            resolved_freq = coerced

    # --- dates: the product's due-date policy, enforced ----------------------
    sw = constraints.start_date
    start = selection.start_date
    if start is not None:
        if not sw.editable and start != sw.default:
            errors.append(
                FieldError(
                    field="start_date",
                    code="start_date_not_editable",
                    message=(
                        "This product does not allow the start date to be changed; "
                        f"it must be {sw.default.isoformat()}."
                    ),
                    min=sw.default,
                    max=sw.default,
                )
            )
        elif start < sw.min:
            errors.append(
                FieldError(
                    field="start_date",
                    code="start_date_too_early",
                    message=(
                        f"Start date must be on or after {sw.min.isoformat()} "
                        f"({sw.min_offset_days} day(s) after today)."
                    ),
                    min=sw.min,
                    max=sw.max,
                )
            )

    fw = constraints.first_payment_date
    first = selection.first_payment_date
    if first is not None:
        if not fw.editable:
            errors.append(
                FieldError(
                    field="first_payment_date",
                    code="first_payment_date_not_editable",
                    message=(
                        "This product does not allow a custom first payment date."
                    ),
                )
            )
        else:
            # The window is measured from the CHOSEN start date when one was
            # supplied; otherwise from the product's default start date.
            anchor = start if start is not None else sw.default
            lo = anchor + timedelta(days=fw.min_offset_days)
            hi = anchor + timedelta(days=fw.max_offset_days or 0)
            if first < lo or first > hi:
                errors.append(
                    FieldError(
                        field="first_payment_date",
                        code="first_payment_date_out_of_window",
                        message=(
                            f"First payment date must fall between {lo.isoformat()} "
                            f"and {hi.isoformat()} — {fw.min_offset_days} to "
                            f"{fw.max_offset_days} days after the start date "
                            f"({anchor.isoformat()})."
                        ),
                        min=lo,
                        max=hi,
                    )
                )

    # Term x frequency must yield at least one installment.
    if (
        not errors
        and selection.term_months is not None
        and resolved_freq is not None
        and payments_in_term(selection.term_months, resolved_freq) < 1
    ):  # pragma: no cover — payments_in_term floors at 1
        errors.append(
            FieldError(
                field="term_months",
                code="term_yields_no_payments",
                message="This term and payment frequency produce no installments.",
            )
        )
    return errors


def assert_selection_allowed(
    constraints: OriginationConstraints, selection: SelectionInput
) -> None:
    """:func:`validate_selection`, raising :class:`ConstraintViolation`."""
    errors = validate_selection(constraints, selection)
    if errors:
        raise ConstraintViolation(errors)


# ---------------------------------------------------------------------------
# The quote / preview
# ---------------------------------------------------------------------------


class ScheduleRowOut(BaseModel):
    """One preliminary amortization row — Dave's exact column set."""

    model_config = ConfigDict(extra="forbid")

    number: int
    date: date
    payment_cents: int          # principal + interest + fees for this row
    principal_cents: int
    interest_cents: int
    fees_cents: int
    balance_cents: int          # principal outstanding AFTER this row


class QuoteTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_cents: int
    interest_cents: int
    fees_cents: int
    total_cents: int


class OriginationQuote(BaseModel):
    """The live calculation behind the New Application form."""

    model_config = ConfigDict(extra="forbid")

    credit_product_id: UUID
    amount_cents: int
    term_months: int
    annual_rate_bps: int
    frequency: str
    frequency_label: str
    payments_per_year: int
    num_payments: int
    start_date: date
    first_payment_date: date
    #: "Approximate <frequency> Payment" — the regular instalment INCLUDING any
    #: per-payment fee, which is what the borrower is actually debited.
    approximate_payment_cents: int
    approximate_payment_label: str
    #: Principal + interest only (the annuity instalment), for reconciliation.
    installment_cents: int
    final_payment_cents: int
    totals: QuoteTotals
    #: Canadian Cost of Borrowing APR (SOR/2001-104 s.3-4). NOT the nominal
    #: annual interest rate whenever a non-contingent fee is charged.
    apr_bps: int
    cost_of_borrowing_cents: int
    exceeds_criminal_rate: bool
    apr_basis: str = (
        "Canadian Cost of Borrowing Regulations SOR/2001-104 s.3-4: "
        "APR = C / (T x P) x 100, where C is the total cost of borrowing "
        "(interest + non-contingent fees), P the average principal outstanding "
        "at the end of each period before that period's payment, and T the term "
        "in years. Contingent default charges (NSF) are excluded."
    )
    disclosure_line: str
    schedule: list[ScheduleRowOut]
    fees: list[FeeLine] = Field(default_factory=list)


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def build_quote(
    product: PlatformCreditProduct,
    selection: SelectionInput,
    *,
    as_of: Optional[date] = None,
    constraints: Optional[OriginationConstraints] = None,
) -> OriginationQuote:
    """Validate a selection then compute the full preliminary quote.

    Raises :class:`ConstraintViolation` (field-level) when any input is outside
    the product's guardrails. Unspecified inputs fall back to the product's
    defaults, so the form's first render needs no user input at all.
    """
    c = constraints or resolve_constraints(product, as_of=as_of)
    assert_selection_allowed(c, selection)

    amount = selection.amount_cents if selection.amount_cents is not None else c.amount.default_cents
    term = selection.term_months if selection.term_months is not None else c.term.default_months
    rate = (
        selection.annual_rate_bps
        if selection.annual_rate_bps is not None
        else c.rate.default_bps
    )
    freq = coerce_frequency(selection.frequency) if selection.frequency else None
    freq = freq or PaymentFrequency(c.frequency.default)
    start = selection.start_date or c.start_date.default
    if selection.first_payment_date is not None:
        first = selection.first_payment_date
    else:
        # Default: one payment period after the start date, pulled into the
        # product's first-due window when the natural date falls outside it.
        lo = start + timedelta(days=c.first_payment_date.min_offset_days)
        hi = start + timedelta(days=c.first_payment_date.max_offset_days or 0)
        first = min(max(advance(start, freq, 1), lo), hi)

    n = payments_in_term(term, freq)
    # THE amortization engine — not a second implementation. `preview_rows=n`
    # asks loan_quote for every row; fees are layered on separately below so the
    # Fees column and the cost-of-borrowing total come from one place.
    q = loan_quote.quote_loan(amount, rate, term, freq.value, fees_cents=0, preview_rows=n)
    rows = q.schedule_preview
    cfg = _pricing(product)
    fee_rows = fee_rows_cents(cfg, amount, freq, len(rows))
    fees_total = int(sum(fee_rows))

    dates = schedule_dates(first, freq, len(rows))
    schedule = [
        ScheduleRowOut(
            number=row["number"],
            date=dates[i],
            payment_cents=row["payment_cents"] + fee_rows[i],
            principal_cents=row["principal_cents"],
            interest_cents=row["interest_cents"],
            fees_cents=fee_rows[i],
            balance_cents=row["balance_cents"],
        )
        for i, row in enumerate(rows)
    ]

    apr_bps = loan_quote.compute_apr_bps(amount, rate, term, freq.value, fees_total)
    interest_total = q.interest_cents
    total = amount + interest_total + fees_total
    per_payment_fee = fee_rows[-1] if fee_rows else 0
    label = FREQUENCY_LABELS[freq]

    return OriginationQuote(
        credit_product_id=product.id,
        amount_cents=amount,
        term_months=term,
        annual_rate_bps=rate,
        frequency=freq.value,
        frequency_label=label,
        payments_per_year=PAYMENTS_PER_YEAR[freq],
        num_payments=len(schedule),
        start_date=start,
        first_payment_date=first,
        approximate_payment_cents=q.installment_cents + per_payment_fee,
        approximate_payment_label=f"Approximate {label} Payment",
        installment_cents=q.installment_cents,
        final_payment_cents=schedule[-1].payment_cents if schedule else 0,
        totals=QuoteTotals(
            principal_cents=amount,
            interest_cents=interest_total,
            fees_cents=fees_total,
            total_cents=total,
        ),
        apr_bps=apr_bps,
        cost_of_borrowing_cents=interest_total + fees_total,
        exceeds_criminal_rate=loan_quote.exceeds_criminal_rate(apr_bps),
        disclosure_line=(
            f"{_money(amount)} + {_money(interest_total)} + {_money(fees_total)} "
            f"= {_money(total)} ({apr_bps / 100:.2f}% APR)"
        ),
        schedule=schedule,
        fees=c.fees,
    )


# ---------------------------------------------------------------------------
# Create-path helper (server-side enforcement)
# ---------------------------------------------------------------------------


def enforce_on_create(
    product: PlatformCreditProduct,
    selection: SelectionInput,
    *,
    as_of: Optional[date] = None,
) -> OriginationConstraints:
    """Guard an application-CREATE call with the same rules as the quote.

    Called by every origination entry point so the product's limits cannot be
    bypassed by posting straight to the API. Raises
    :class:`ConstraintViolation`; a product whose stored pricing config is
    unparseable raises :class:`PricingConfigError` (a platform config fault, not
    a caller error — endpoints map it to 502).
    """
    c = resolve_constraints(product, as_of=as_of)
    assert_selection_allowed(c, selection)
    return c
