"""Loan-terms quote engine — the START of the applicant journey (Dave's spec).

The client adjusts Loan Amount · Term (months) · Payment Frequency within the
product's parameters, and we compute the regulated disclosure figures:

  * Installment payment per the chosen frequency (Monthly / Bi-weekly / …)
  * Total of Payments
  * Cost of Borrowing
  * APR — DEFERRED (a regulated Canadian Cost-of-Borrowing figure that differs
    from the annual interest rate and folds in fees). The engine carries the
    fee + apr hooks but leaves apr_bps None until Dave confirms the fee schedule
    and APR method — we do NOT invent a legal disclosure number.

This is purely a calculator (no PII, no application yet). The actual booking
schedule lives in loan_servicing; this mirrors its math across frequencies.
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
    if period_rate == 0:
        regular = -(-amount_cents // n)  # ceil so the schedule fully amortizes
    else:
        raw = amount_cents * period_rate / (1 - (1 + period_rate) ** -n)
        regular = round(raw)

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
        apr_bps=None,
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
