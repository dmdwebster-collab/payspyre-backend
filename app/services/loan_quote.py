"""Loan-terms quote engine — the START of the applicant journey (Dave's spec).

The client adjusts Loan Amount · Term (months) · Payment Frequency within the
product's parameters, and we compute the regulated disclosure figures:

  * Installment payment per the chosen frequency (Monthly / Bi-weekly / …)
  * Total of Payments
  * Cost of Borrowing
  * APR — the standard actuarial Annual Percentage Rate (the Canadian
    Cost-of-Borrowing definition): the annual rate ``r`` such that the present
    value of the installment schedule, discounted at ``r / payments_per_year``,
    equals the amount advanced MINUS any upfront fees. With no fees this equals
    the contract interest rate; fees push it above the contract rate.

This is purely a calculator (no PII, no application yet). The actual booking
schedule lives in loan_servicing; this mirrors its math across frequencies.

NOTE (pending Dave / compliance confirmation): the FEE SCHEDULE — i.e. exactly
which charges count toward the cost of borrowing and therefore enter the APR —
plus the final APR rounding convention and day-count basis are not yet legally
locked. The method implemented here is the standard actuarial APR; ``fees_cents``
defaults to 0 from ``pricing_config["fees_cents"]`` until that schedule is
confirmed, so today's disclosed APR equals the contract rate.
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


def _amortize(amount_cents: int, period_rate: float, n: int) -> list[int]:
    """Amortize at ``period_rate`` and return the actual per-period payment amounts
    (the last entry absorbs rounding / clears the balance). The cent-rounded
    schedule produced here is the EXACT cashflow we later discount for the APR, so
    APR is computed against the same payments the borrower actually makes.
    """
    regular = _regular_installment(amount_cents, period_rate, n)
    balance = amount_cents
    payments: list[int] = []
    for i in range(1, n + 1):
        interest = round(balance * period_rate)
        principal_portion = regular - interest
        if i == n or principal_portion >= balance:
            principal_portion = balance
            payment = interest + principal_portion
        else:
            payment = regular
        balance -= principal_portion
        payments.append(payment)
        if balance <= 0:
            break
    return payments


def compute_apr_bps(
    amount_cents: int,
    annual_rate_bps: int,
    term_months: int,
    frequency: str,
    fees_cents: int = 0,
) -> int:
    """Standard actuarial APR for a quote, in basis points.

    Builds the cent-exact installment schedule at the CONTRACT rate (reusing the
    same amortize logic as ``quote_loan``), then solves for the periodic rate ``i``
    such that the present value of that schedule, discounted at ``i``, equals the
    NET advance (``amount_cents - fees_cents``). Annualizing ``i`` by the number of
    payments per year and converting to bps gives the APR.

    Invariant: ``fees_cents == 0`` -> APR == ``annual_rate_bps`` (an installment
    loan with no fees discloses its contract rate); ``fees_cents > 0`` -> APR >
    ``annual_rate_bps`` (the borrower receives less but repays the same schedule).
    """
    if frequency not in FREQUENCIES:
        raise ValueError(f"Unknown payment frequency '{frequency}'")
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")

    per_year = FREQUENCIES[frequency]["per_year"]
    n = num_payments(term_months, frequency)
    contract_period_rate = annual_rate_bps / 10_000.0 / per_year

    payments = _amortize(amount_cents, contract_period_rate, n)
    net_advance = amount_cents - fees_cents
    if net_advance <= 0:
        raise ValueError("amount_cents must exceed fees_cents")

    def pv_minus_advance(i: float) -> float:
        """PV of the payment schedule discounted at periodic rate ``i``, minus the
        net advance. Monotonically DECREASING in ``i`` (higher discount -> lower PV).
        """
        pv = 0.0
        for k, pay in enumerate(payments, start=1):
            pv += pay / (1 + i) ** k
        return pv - net_advance

    # With no fees the PV at the contract rate equals the advance (to rounding),
    # so the contract period rate is already the root — return the contract rate
    # exactly rather than letting cent-rounding nudge it by a bp.
    if fees_cents == 0:
        return annual_rate_bps

    # Bracket the root. f is decreasing in i; fees>0 means f(contract_rate) > 0,
    # so the APR's periodic rate is ABOVE the contract rate. Grow the upper bound
    # until f goes negative.
    lo = contract_period_rate
    hi = max(contract_period_rate, 0.0) + 1e-9
    f_hi = pv_minus_advance(hi)
    grow_guard = 0
    while f_hi > 0 and grow_guard < 200:
        hi *= 2
        if hi < 1e-6:
            hi = 1e-6
        f_hi = pv_minus_advance(hi)
        grow_guard += 1

    # Bisection on the periodic rate (robust; the function is smooth & monotone).
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = pv_minus_advance(mid)
        if abs(f_mid) < 1e-9 or (hi - lo) < 1e-15:
            break
        if f_mid > 0:
            lo = mid
        else:
            hi = mid
    periodic = (lo + hi) / 2

    apr = periodic * per_year * 10_000.0
    return round(apr)


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
    total_of_payments_cents: int
    cost_of_borrowing_cents: int      # interest + fees (fees=0 pending Dave)
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

    cost_of_borrowing = total - amount_cents + fees_cents
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

    frequencies = cfg.get("payment_frequencies")
    if not frequencies:
        frequencies = list(FREQUENCIES.keys())
    frequencies = [f for f in frequencies if f in FREQUENCIES]

    rate = cfg.get("apr_bps") or cfg.get("annual_rate_bps") or _DEFAULT_RATE_BPS

    return {
        "term_options": sorted(set(int(t) for t in terms)),
        "frequencies": [{"value": f, "label": FREQUENCIES[f]["label"]} for f in frequencies],
        "annual_rate_bps": int(rate),
        "fees_cents": int(cfg.get("fees_cents") or 0),
    }
