"""Loan-terms quote engine — the START of the applicant journey (Dave's spec).

The client adjusts Loan Amount · Term (months) · Payment Frequency within the
product's parameters, and we compute the regulated disclosure figures:

  * Installment payment per the chosen frequency (Monthly / Bi-weekly / …)
  * Principal / Interest / Fees / Total of Payments breakdown
  * APR — computed by the ONE method Canadian law allows: the Cost of Borrowing
    Regulations (SOR/2001-104) s.3(1):

        APR = ( C / (T × P) ) × 100

    where, over the term of the loan:
      C = the cost of borrowing  (total interest + applicable fees), in cents;
      P = the average of the principal outstanding at the end of each interest
          period, before subtracting that period's payment (rule 3(2)(b): each
          instalment is applied first to the accumulated cost of borrowing, then
          to principal), in cents;
      T = the term of the loan in years (rule 3(2)(c): a month is 1/12, a week
          1/52, a day 1/365 of a year).
    s.4: if there is no cost of borrowing other than interest (fees = 0), the
    APR IS the annual interest rate — so we return the contract rate directly.

This is purely a calculator (no PII, no application yet). The actual booking
schedule lives in loan_servicing; this mirrors its math across frequencies.

COMPLIANCE NOTE: this implements SOR/2001-104 s.3-4 as quoted by Dave
(https://laws-lois.justice.gc.ca/eng/regulations/sor-2001-104/page-1.html).
Which specific charges count toward C is configured per credit product via the
typed fee schedule (``app/schemas/pricing_config.py``; contingent ``on_event``
default charges like NSF are excluded). The s.3(2)(a) option to round
the APR to the nearest 1/8 % is NOT applied — we disclose the unrounded rate,
which is also compliant. This calc should get a final legal/QA pass against a
known worked example before go-live.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.schemas.pricing_config import (
    FREQUENCY_LABELS,
    PAYMENTS_PER_YEAR,
    InterestConfig,
    PaymentFrequency,
    PricingConfig,
    PricingConfigError,
    origination_lump_fees_cents,
    parse_pricing_config,
    payments_in_term,
    quote_fees_cents,
)

# Payment frequencies and how many payments fall in a year — derived from the
# canonical table in app/schemas/pricing_config.py so the product schema and
# the quote engine can never diverge on the frequency set again.
FREQUENCIES: dict[str, dict] = {
    f.value: {"label": FREQUENCY_LABELS[f], "per_year": PAYMENTS_PER_YEAR[f]}
    for f in (
        PaymentFrequency.MONTHLY,
        PaymentFrequency.SEMI_MONTHLY,
        PaymentFrequency.BI_WEEKLY,
        PaymentFrequency.WEEKLY,
    )
}


# Criminal Code (Canada) s.347 criminal rate of interest. Bill C-47 lowered it from
# 60% effective annual rate to 35% APR, in force 2026-01-01. A loan whose APR exceeds
# this is a criminal-rate loan. Expressed in bps (35% = 3500 bps). The number should
# be legally confirmed; this constant centralizes it so enforcement has one source.
CRIMINAL_RATE_CAP_BPS = 3500


def exceeds_criminal_rate(apr_bps: int) -> bool:
    """True if the disclosed APR is at/above the s.347 criminal-rate cap."""
    return apr_bps >= CRIMINAL_RATE_CAP_BPS


def num_payments(term_months: int, frequency: str) -> int:
    """How many instalments a term spans at the given frequency.

    Delegates to the canonical implementation next to the pricing schema so
    fee math and schedule math always agree on the payment count.
    """
    return payments_in_term(term_months, frequency)


def _regular_installment(amount_cents: int, period_rate: float, n: int) -> int:
    """The regular instalment: zero-rate splits evenly, else the annuity payment."""
    if period_rate == 0:
        return -(-amount_cents // n)  # ceil so the schedule fully amortizes
    raw = amount_cents * period_rate / (1 - (1 + period_rate) ** -n)
    return round(raw)


def compute_apr_bps(
    amount_cents: int,
    annual_rate_bps: int,
    term_months: int,
    frequency: str,
    fees_cents: int = 0,
) -> int:
    """Canadian regulatory APR (SOR/2001-104 s.3-4), in basis points.

        APR = ( C / (T × P) ) × 100

    C = total interest + fees over the term; P = average principal outstanding at
    the end of each interest period (before that period's payment); T = term in
    years. See the module docstring for the regulatory citations.

    s.4 special case: with no cost of borrowing other than interest (fees == 0),
    the APR IS the annual interest rate, so we return ``annual_rate_bps`` directly.
    With fees > 0 the formula yields an APR above the contract rate.
    """
    if frequency not in FREQUENCIES:
        raise ValueError(f"Unknown payment frequency '{frequency}'")
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")
    if fees_cents < 0:
        raise ValueError("fees_cents must be non-negative")

    # s.4 — interest is the only cost of borrowing → APR is the annual interest rate.
    if fees_cents == 0:
        return annual_rate_bps

    per_year = FREQUENCIES[frequency]["per_year"]
    n = num_payments(term_months, frequency)
    period_rate = annual_rate_bps / 10_000.0 / per_year
    regular = _regular_installment(amount_cents, period_rate, n)

    # Walk the regulatory amortization (s.3(2)(b): payment applied to accumulated
    # cost of borrowing first, then principal), accumulating the two inputs we need:
    #   opening_sum — Σ principal outstanding at the end of each period BEFORE its
    #                 payment (i.e. the opening balance of the period) → averages to P
    #   total_interest — Σ interest accrued each period → the interest part of C
    balance = amount_cents
    opening_sum = 0
    total_interest = 0
    periods = 0
    for i in range(1, n + 1):
        opening_sum += balance
        interest = round(balance * period_rate)
        principal_portion = regular - interest
        if i == n or principal_portion >= balance:
            principal_portion = balance
        total_interest += interest
        balance -= principal_portion
        periods += 1
        if balance <= 0:
            break

    # C, P in cents (the cents cancel in C/P); T in years from the period count.
    avg_principal = opening_sum / periods            # P
    cost_of_borrowing = total_interest + fees_cents  # C
    term_years = periods / per_year                  # T
    if avg_principal <= 0 or term_years <= 0:
        return annual_rate_bps

    apr_fraction = cost_of_borrowing / (term_years * avg_principal)
    return round(apr_fraction * 10_000.0)


@dataclass
class Quote:
    amount_cents: int
    annual_rate_bps: int
    term_months: int
    frequency: str
    frequency_label: str
    num_payments: int
    installment_cents: int            # the regular payment
    final_installment_cents: int      # last payment absorbs rounding
    total_of_payments_cents: int      # principal + interest + fees
    interest_cents: int               # total interest over the term
    cost_of_borrowing_cents: int      # interest + fees
    fees_cents: int = 0
    apr_bps: Optional[int] = None     # Canadian regulatory APR (SOR/2001-104 s.3-4)
    exceeds_criminal_rate: bool = False  # APR >= s.347 cap (compliance guardrail)
    schedule_preview: list[dict] = field(default_factory=list)


def quote_loan(
    amount_cents: int,
    annual_rate_bps: int,
    term_months: int,
    frequency: str,
    *,
    fees_cents: int = 0,
    preview_rows: int = 0,
) -> Quote:
    """Compute the disclosure figures for one set of selected terms.

    Amortizes period-by-period (same convention as loan_servicing's native
    monthly schedule, generalised to the period rate) so Total of Payments and
    Cost of Borrowing are EXACT, not installment×n estimates.
    """
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")
    if term_months <= 0:
        raise ValueError("term_months must be positive")
    if frequency not in FREQUENCIES:
        raise ValueError(f"Unknown payment frequency '{frequency}'")

    n = num_payments(term_months, frequency)
    period_rate = annual_rate_bps / 10_000.0 / FREQUENCIES[frequency]["per_year"]

    # Regular installment: zero-rate splits evenly; else the standard annuity payment.
    regular = _regular_installment(amount_cents, period_rate, n)

    # Amortize to get the exact total + a clean final payment.
    balance = amount_cents
    total = 0
    schedule: list[dict] = []
    final = regular
    for i in range(1, n + 1):
        interest = round(balance * period_rate)
        principal_portion = regular - interest
        if i == n or principal_portion >= balance:
            principal_portion = balance
            payment = interest + principal_portion
            final = payment
        else:
            payment = regular
        balance -= principal_portion
        total += payment
        if preview_rows and i <= preview_rows:
            schedule.append({
                "number": i, "payment_cents": payment,
                "principal_cents": principal_portion, "interest_cents": interest,
                "balance_cents": balance,
            })
        if balance <= 0:
            n = i
            break

    interest_cents = total - amount_cents
    cost_of_borrowing = interest_cents + fees_cents
    apr_bps = compute_apr_bps(amount_cents, annual_rate_bps, term_months, frequency, fees_cents)

    return Quote(
        amount_cents=amount_cents,
        annual_rate_bps=annual_rate_bps,
        term_months=term_months,
        frequency=frequency,
        frequency_label=FREQUENCIES[frequency]["label"],
        num_payments=n,
        installment_cents=regular,
        final_installment_cents=final,
        total_of_payments_cents=total + fees_cents,
        interest_cents=interest_cents,
        cost_of_borrowing_cents=cost_of_borrowing,
        fees_cents=fees_cents,
        apr_bps=apr_bps,
        exceeds_criminal_rate=exceeds_criminal_rate(apr_bps),
        schedule_preview=schedule,
    )


# --- product parameter extraction (drives the term/frequency selectors) -----

_DEFAULT_TERMS = [12, 24, 36, 48, 60]
_DEFAULT_RATE_BPS = 1299  # 12.99% — mirror loan_servicing._DEFAULT_ANNUAL_RATE_BPS
# so an unconfigured product quotes the same rate it would book at.


def _term_bounds(cfg: PricingConfig) -> tuple[int, int, list[int]]:
    """(term_min, term_max, term_options) with the engine's historical defaults."""
    term_options = cfg.term_options or list(_DEFAULT_TERMS)
    term_min = cfg.term_min_months or term_options[0]
    term_max = cfg.term_max_months or term_options[-1]
    if term_min > term_max:
        term_min, term_max = term_max, term_min
    return term_min, term_max, term_options


def _rate_bounds(cfg: PricingConfig) -> InterestConfig:
    """The product's interest config, or the legacy 12.99% platform default."""
    if cfg.interest is not None:
        return cfg.interest
    return InterestConfig(
        annual_rate_bps=_DEFAULT_RATE_BPS,
        min_rate_bps=_DEFAULT_RATE_BPS,
        max_rate_bps=_DEFAULT_RATE_BPS,
    )


def product_fees_cents(
    pricing_config: Optional[dict], amount_cents: int, term_months: int, frequency: str
) -> int:
    """The cost-of-borrowing fees (cents) for ONE selection on a product.

    This replaces the old amount/term-blind ``pricing_config["fees_cents"]``
    lump: per-payment fees scale with the payment count (a $1/payment admin fee
    compounds more often bi-weekly than monthly — exactly the APR effect Dave
    demoed in Turkey), rate fees scale with the advance, and contingent
    (``on_event``) fees like NSF are excluded per SOR/2001-104.
    """
    cfg = parse_pricing_config(pricing_config)
    return quote_fees_cents(cfg, amount_cents, term_months, frequency)


def product_worst_case_apr_bps(min_amount_cents: int, pricing_config: Optional[dict]) -> int:
    """The HIGHEST regulatory APR a product can produce, in bps.

    APR is driven up by a small advance (any fixed fee is then a larger fraction),
    a short term (a lump fee's APR contribution scales ~1/T), a frequent payment
    period (per-payment fees recur more often), and the TOP of the rate band. We
    evaluate every enabled frequency at both term bounds with the max allowed
    rate and take the max. Used to refuse a product config that could ever book
    a criminal-rate (s.347) loan, catching it at configuration, not at booking.
    """
    cfg = parse_pricing_config(pricing_config)
    interest = _rate_bounds(cfg)
    term_min, term_max, _ = _term_bounds(cfg)
    return max(
        compute_apr_bps(
            min_amount_cents,
            interest.max_rate_bps,
            t,
            freq.value,
            quote_fees_cents(cfg, min_amount_cents, t, freq),
        )
        for freq in cfg.payment_frequencies
        for t in {term_min, term_max}
    )


def province_cap_hook(province: Optional[str], apr_bps: int) -> bool:
    """Stub interface for the future per-province compliance engine.

    Will validate a disclosed APR against the province's maximum (including
    high-cost-credit licensing thresholds; multi-province products must clear
    the LOWEST cap). Returns ok (True) for every province until the compliance
    engine lands — the Criminal Code s.347 cap remains the binding federal
    check in the meantime.
    """
    return True


def validate_pricing_config(
    pricing_config: Optional[dict],
    min_amount_cents: int,
    max_amount_cents: int,
    provinces: Optional[list[str]] = None,
) -> PricingConfig:
    """Validate + normalize a product's pricing_config (create/PATCH gate).

    1. Parses the payload through the typed schema (tolerant of the legacy
       shape; raises :class:`PricingConfigError` on invalid payloads — which
       includes any enabled late fee, per Canadian policy).
    2. Normalizes: fills ``interest`` so the stored config is explicit about
       the rate it will quote/book at.
    3. Computes the Canadian regulatory APR for EVERY enabled payment frequency
       at the boundary amounts/terms and the top of the rate band, and refuses
       any configuration that can reach the Criminal Code s.347 cap.
    4. Runs each boundary APR through :func:`province_cap_hook` (stub — future
       per-province compliance engine seam).

    Returns the normalized :class:`PricingConfig`; callers should persist
    ``.model_dump(mode="json", exclude_none=True)`` so the DB converges on the
    typed shape.
    """
    cfg = parse_pricing_config(pricing_config, context="product create/update")
    if cfg.interest is None:
        # Pin the legacy platform default explicitly so quote == booking == disclosure.
        cfg = cfg.model_copy(update={"interest": _rate_bounds(cfg)})

    interest = cfg.interest
    term_min, term_max, _ = _term_bounds(cfg)
    amounts = {min_amount_cents, max_amount_cents}
    if cfg.amount_min_cents is not None:
        amounts.add(cfg.amount_min_cents)
    if cfg.amount_max_cents is not None:
        amounts.add(cfg.amount_max_cents)
    rates = {interest.annual_rate_bps, interest.min_rate_bps, interest.max_rate_bps}

    for freq in cfg.payment_frequencies:
        for amount in amounts:
            for term in {term_min, term_max}:
                fees = quote_fees_cents(cfg, amount, term, freq)
                for rate in rates:
                    apr = compute_apr_bps(amount, rate, term, freq.value, fees)
                    if exceeds_criminal_rate(apr):
                        raise PricingConfigError(
                            f"This pricing can produce an APR of {apr / 100:.2f}% "
                            f"({FREQUENCY_LABELS[freq]}, ${amount / 100:,.2f} over "
                            f"{term} months at {rate / 100:.2f}%), at/above the "
                            f"Criminal Code s.347 cap ({CRIMINAL_RATE_CAP_BPS / 100:.0f}%). "
                            "Lower the interest rate or fees, or disable that payment frequency."
                        )
                    for province in provinces or [None]:
                        if not province_cap_hook(province, apr):
                            raise PricingConfigError(
                                f"APR {apr / 100:.2f}% ({FREQUENCY_LABELS[freq]}) exceeds "
                                f"the provincial cap for {province}."
                            )
    return cfg


def product_terms(pricing_config: Optional[dict]) -> dict:
    """The adjustable parameters a product allows: term options, frequencies, rate.

    Parses pricing_config through the typed schema (tolerant of the legacy
    shape); falls back to sensible defaults so the calculator renders even for
    a thinly-configured product.

    NOTE: ``fees_cents`` here is the LEGACY fixed at-origination lump kept for
    back-compat readers; selection-aware fee math (per-payment / rate-based /
    per-frequency fees) lives in :func:`product_fees_cents`.
    """
    cfg = parse_pricing_config(pricing_config)
    term_min, term_max, term_options = _term_bounds(cfg)
    return {
        "term_options": term_options,
        "term_min": term_min,
        "term_max": term_max,
        "frequencies": [
            {"value": f.value, "label": FREQUENCY_LABELS[f]} for f in cfg.payment_frequencies
        ],
        "annual_rate_bps": _rate_bounds(cfg).annual_rate_bps,
        "fees_cents": origination_lump_fees_cents(cfg),
    }
