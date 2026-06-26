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
Which specific charges count toward C (the fee schedule) is configured per
credit product (``pricing_config["fees_cents"]``). The s.3(2)(a) option to round
the APR to the nearest 1/8 % is NOT applied — we disclose the unrounded rate,
which is also compliant. This calc should get a final legal/QA pass against a
known worked example before go-live.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Payment frequencies and how many payments fall in a year. Confirm the offered
# set with Dave (these are the standard Canadian instalment frequencies).
FREQUENCIES: dict[str, dict] = {
    "monthly": {"label": "Monthly", "per_year": 12},
    "semi_monthly": {"label": "Semi-monthly", "per_year": 24},
    "bi_weekly": {"label": "Bi-weekly", "per_year": 26},
    "weekly": {"label": "Weekly", "per_year": 52},
}


def num_payments(term_months: int, frequency: str) -> int:
    """How many instalments a term spans at the given frequency."""
    per_year = FREQUENCIES[frequency]["per_year"]
    if frequency == "monthly":
        return term_months
    if frequency == "semi_monthly":
        return term_months * 2
    # weekly / bi-weekly don't divide months evenly — scale by the year fraction.
    return max(1, round(term_months * per_year / 12))


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
    apr_bps: Optional[int] = None     # DEFERRED — see module docstring
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
        schedule_preview=schedule,
    )


# --- product parameter extraction (drives the term/frequency selectors) -----

_DEFAULT_TERMS = [12, 24, 36, 48, 60]
_DEFAULT_RATE_BPS = 1290


def product_terms(pricing_config: Optional[dict]) -> dict:
    """The adjustable parameters a product allows: term options, frequencies, rate.

    Reads pricing_config defensively; falls back to sensible defaults so the
    calculator renders even for a thinly-configured product.
    """
    cfg = pricing_config or {}
    terms = cfg.get("term_options") or cfg.get("term_months")
    if isinstance(terms, int):
        terms = [terms]
    if not terms:
        terms = list(_DEFAULT_TERMS)
    term_options = sorted(set(int(t) for t in terms))

    # Term is a continuous range (the calculator uses a slider) — derive the bounds
    # from explicit config if present, else from the discrete options.
    term_min = int(cfg.get("term_min") or term_options[0])
    term_max = int(cfg.get("term_max") or term_options[-1])
    if term_min > term_max:
        term_min, term_max = term_max, term_min

    frequencies = cfg.get("payment_frequencies")
    if not frequencies:
        frequencies = list(FREQUENCIES.keys())
    frequencies = [f for f in frequencies if f in FREQUENCIES]

    rate = cfg.get("apr_bps") or cfg.get("annual_rate_bps") or _DEFAULT_RATE_BPS

    return {
        "term_options": term_options,
        "term_min": term_min,
        "term_max": term_max,
        "frequencies": [{"value": f, "label": FREQUENCIES[f]["label"]} for f in frequencies],
        "annual_rate_bps": int(rate),
        "fees_cents": int(cfg.get("fees_cents") or 0),
    }
