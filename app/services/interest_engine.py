"""Actuals-based daily-simple-interest engine (pure functional core). WS-A / P0.

Turnkey-parity gap #2 (GAP_ANALYSIS): Dave's product is daily simple interest
recomputed from *actual* payment dates — a late payment accrues extra per-diem
days, an early payment fewer. The amortization schedule remains the as-agreed
PLAN; this engine + the immutable loan ledger (``platform_loan_transactions``)
are the MONEY TRUTH.

Follows the ``flow_engine`` pure-core idiom: every function here is pure — no
DB, no I/O, no clock reads. Callers (``loan_ledger`` / ``loan_servicing``) map
ledger rows to :class:`LedgerEvent` values and persist results.

MONEY: integer cents everywhere. Interest for a span of days is computed in
exact integer arithmetic and rounded half-to-even ONCE per span (between
consecutive ledger events), matching the repo's existing convention of rounding
per accrual period (``loan_quote`` / ``loan_servicing._actual_360_schedule``
round per period, not per day).

CONVENTIONS (defensible standards, flagged for Dave until his two Excel files
— "account due as of" and "amount to move" — arrive; all constants live in
:class:`InterestEngineConfig` so they can be re-pointed in one place):

  * Day count: ACT/365-Fixed — per-diem = outstanding_principal × annual_rate
    / 365. (Turnkey's schedule generator reconciled to actual/360, but Dave's
    daily-simple-interest narration is per-diem on the outstanding balance;
    ACT/365 is the standard Canadian consumer-lending convention.)
  * Accrual starts on the DISBURSEMENT date (money out the door) and interest
    for a span covers ``[period_start, period_end)`` — the disbursement day
    itself accrues, the payment's own effective date does not (a payment
    effective the day after disbursement pays exactly 1 day of per-diem).
  * A payment's interest/principal split is evaluated at its EFFECTIVE date
    (Dave's dual-date mandate: effective = when money is treated as applied;
    processing = when it was recorded).
  * Interest paid beyond what has actually accrued (possible on backfilled
    schedule-allocated history) is redirected to principal — money is
    conserved, never dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Sequence


@dataclass(frozen=True)
class InterestEngineConfig:
    """Every policy constant of the actuals engine, in ONE place.

    ``day_count_denominator`` — days per year in the per-diem (ACT/365-Fixed).
    Flagged for Dave: confirm against his "account due as of" Excel (365 vs
    360 vs 365.25).
    """

    day_count_denominator: int = 365


DEFAULT_CONFIG = InterestEngineConfig()


@dataclass(frozen=True)
class LedgerEvent:
    """One money event, already mapped from a ledger row (or synthesized).

    ``*_paid_cents`` reduce the respective bucket (cash in / adjustments);
    ``*_charged_cents`` increase it (fee assessments). Values are normally
    non-negative — direction is carried by which field is used (a reversal is
    mapped by the caller into the opposite fields of the original event). The
    one sanctioned exception: a cutover-reconciliation ADJUSTMENT row may carry
    a negative ``principal_paid_cents`` to move outstanding principal UP onto
    the operational balance (migration 049).
    """

    effective_date: date
    principal_paid_cents: int = 0
    interest_paid_cents: int = 0
    fees_paid_cents: int = 0
    add_on_paid_cents: int = 0
    fees_charged_cents: int = 0
    add_on_charged_cents: int = 0
    # Principal put BACK on the loan (e.g. reversal of a payment's principal).
    principal_charged_cents: int = 0
    # Accrued-interest put back (reversal of a payment's interest portion).
    interest_charged_cents: int = 0


@dataclass(frozen=True)
class BalanceView:
    """The ledger header math. INVARIANT (Dave, 04__WP_Collections):

        outstanding principal + interest due + fees due + add-on balance
            == payoff amount as of ``as_of``

    Payoff charges 0% future interest by construction — interest is accrued
    only up to (not beyond) ``as_of``.
    """

    as_of: date
    outstanding_principal_cents: int
    interest_due_cents: int
    fees_due_cents: int
    add_on_balance_cents: int

    @property
    def payoff_cents(self) -> int:
        return (
            self.outstanding_principal_cents
            + self.interest_due_cents
            + self.fees_due_cents
            + self.add_on_balance_cents
        )


def accrue_interest_cents(
    outstanding_principal_cents: int,
    annual_rate_bps: int,
    days: int,
    config: InterestEngineConfig = DEFAULT_CONFIG,
) -> int:
    """Simple interest for ``days`` on a constant principal, in integer cents.

    Exact integer arithmetic, rounded half-to-even once for the whole span
    (bankers' rounding — same tie behaviour as Python's ``round()`` used by
    ``loan_quote``, but with no float error).
    """
    if days <= 0 or outstanding_principal_cents <= 0 or annual_rate_bps <= 0:
        return 0
    numerator = outstanding_principal_cents * annual_rate_bps * days
    denominator = 10_000 * config.day_count_denominator
    quotient, remainder = divmod(numerator, denominator)
    doubled = remainder * 2
    if doubled > denominator or (doubled == denominator and quotient % 2 == 1):
        quotient += 1
    return quotient


def compute_balances(
    principal_cents: int,
    annual_rate_bps: int,
    accrual_start: Optional[date],
    events: Sequence[LedgerEvent],
    as_of: date,
    config: InterestEngineConfig = DEFAULT_CONFIG,
) -> BalanceView:
    """Replay the ledger's actual history and return the balance view at ``as_of``.

    * ``principal_cents`` — the original advance (anchor). Outstanding principal
      starts here at ``accrual_start``; ledger disbursement rows are mapped by
      the caller as no-ops so the advance is never double-counted.
    * ``accrual_start`` — the disbursement date. ``None`` means money never went
      out: no interest accrues (defensible: interest cannot accrue before the
      borrower has the funds).
    * ``events`` — MUST be sorted by (effective_date, recorded order). Events
      dated after ``as_of`` are ignored.

    Walk: accrue per-diem on the outstanding principal between consecutive
    event effective-dates, apply each event (interest first — excess interest
    payment redirects to principal), then accrue the final span to ``as_of``.
    """
    outstanding = max(0, principal_cents)
    interest_due = 0
    fees_due = 0
    add_on = 0
    cursor = accrual_start

    for ev in events:
        if ev.effective_date > as_of:
            continue
        # Accrue the span up to this event's effective date.
        if cursor is not None and ev.effective_date > cursor:
            interest_due += accrue_interest_cents(
                outstanding, annual_rate_bps, (ev.effective_date - cursor).days, config
            )
            cursor = ev.effective_date

        # Interest bucket (charges first so a same-row reversal nets correctly).
        interest_due += ev.interest_charged_cents
        excess_interest_paid = max(0, ev.interest_paid_cents - interest_due)
        interest_due = max(0, interest_due - ev.interest_paid_cents)

        # Principal: paid + any interest overpayment (money conservation).
        outstanding += ev.principal_charged_cents
        outstanding = max(0, outstanding - ev.principal_paid_cents - excess_interest_paid)

        # Fee buckets (add-on never accrues interest — Dave's mandate).
        fees_due = max(0, fees_due + ev.fees_charged_cents - ev.fees_paid_cents)
        add_on = max(0, add_on + ev.add_on_charged_cents - ev.add_on_paid_cents)

    if cursor is not None and as_of > cursor:
        interest_due += accrue_interest_cents(
            outstanding, annual_rate_bps, (as_of - cursor).days, config
        )

    return BalanceView(
        as_of=as_of,
        outstanding_principal_cents=outstanding,
        interest_due_cents=interest_due,
        fees_due_cents=fees_due,
        add_on_balance_cents=add_on,
    )


@dataclass(frozen=True)
class PaymentAllocation:
    """How one payment's cash splits across the buckets (integer cents).

    ``overpayment_cents`` is the slice of ``principal_cents`` that exceeded the
    outstanding principal (cash received beyond everything owed). It is kept
    inside the principal allocation so the ledger row still sums to the cash
    amount; the balance walk clamps outstanding at zero. Refund/credit policy
    is a later workstream (flagged for Dave).
    """

    interest_cents: int
    principal_cents: int
    fees_cents: int
    add_on_cents: int = 0
    overpayment_cents: int = 0

    @property
    def total_cents(self) -> int:
        return (
            self.interest_cents + self.principal_cents + self.fees_cents + self.add_on_cents
        )


def allocate_regular_payment(
    amount_cents: int, balances: BalanceView
) -> PaymentAllocation:
    """Turnkey "Regular" repayment mode: accrued interest → principal → fees.

    Add-on balance is NOT touched by a regular payment (it is targeted only by
    the Add-on repayment mode — a later workstream). Anything left after fees
    is recorded as principal (overpayment; see ``PaymentAllocation``).
    """
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")

    interest = min(amount_cents, balances.interest_due_cents)
    remaining = amount_cents - interest

    principal = min(remaining, balances.outstanding_principal_cents)
    remaining -= principal

    fees = min(remaining, balances.fees_due_cents)
    remaining -= fees

    # Residual cash: keep it on the row as principal so the ledger row ties out
    # to the cash received; the engine clamps outstanding principal at zero.
    return PaymentAllocation(
        interest_cents=interest,
        principal_cents=principal + remaining,
        fees_cents=fees,
        add_on_cents=0,
        overpayment_cents=remaining,
    )
