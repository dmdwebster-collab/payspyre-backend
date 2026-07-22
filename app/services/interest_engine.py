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


# ---------------------------------------------------------------------------
# Regular-payment allocation ordering (MONEY PATH, product-configurable)
#
# The categories a repayment ordering may name, and the order the allocator
# resolves them in when a product supplies none. These MUST stay identical to
# ``app.schemas.product_policy_config.ALLOCATION_CATEGORIES`` /
# ``DEFAULT_REPAYMENT_PRIORITY``; they are duplicated rather than imported so
# this module stays dependency-free (pure core), and a test asserts equality.
# ---------------------------------------------------------------------------

ALLOCATION_CATEGORIES = ("interest", "principal", "origination", "administration", "nsf")

DEFAULT_REPAYMENT_PRIORITY = (
    "interest",
    "principal",
    "origination",
    "administration",
    "nsf",
)

#: ``origination`` and ``administration`` are both scheduled fees and share the
#: engine's single ``fees_due`` bucket. Their order relative to each other is
#: therefore unobservable — whichever comes first drains the bucket.
_SCHEDULED_FEE_CATEGORIES = frozenset({"origination", "administration"})


def _resolved_repayment_priority(
    priority: Optional[Sequence[str]],
) -> tuple[str, ...]:
    """Normalise a configured repayment ordering, or fall back to the default.

    ``None`` / empty → :data:`DEFAULT_REPAYMENT_PRIORITY` (the behaviour every
    loan has today; a product with ``policy_config = NULL`` lands here). Any
    other value MUST be a permutation of :data:`ALLOCATION_CATEGORIES` — a
    partial order would leave some dollars' destination undefined.
    """
    if not priority:
        return DEFAULT_REPAYMENT_PRIORITY
    order = tuple(priority)
    if sorted(order) != sorted(ALLOCATION_CATEGORIES):
        raise ValueError(
            f"repayment priority {list(order)} must be a permutation of "
            f"{list(ALLOCATION_CATEGORIES)}"
        )
    return order


def allocate_regular_payment(
    amount_cents: int,
    balances: BalanceView,
    priority: Optional[Sequence[str]] = None,
) -> PaymentAllocation:
    """Turnkey "Regular" repayment mode, allocated in the CONFIGURED order.

    ``priority`` is the product's
    ``policy_config.allocation_priority.repayment`` list (see
    :mod:`app.schemas.product_policy_config`). ``None`` — the case for every
    product whose ``policy_config`` is NULL — uses
    :data:`DEFAULT_REPAYMENT_PRIORITY`, which reproduces the historical
    hard-coded waterfall exactly: accrued interest → principal → scheduled
    fees.

    Bucket mapping:
      * ``interest``      → ``interest_due_cents``
      * ``principal``     → ``outstanding_principal_cents``
      * ``origination`` / ``administration`` → the single ``fees_due_cents``
        bucket; whichever appears first drains it, the second is then a no-op.
      * ``nsf``           → the add-on bucket, which a regular payment NEVER
        touches by construction (it is targeted only by the Add-on repayment
        mode). Its position is therefore inert — the ordering schema pins it
        last so a product cannot advertise otherwise.

    Anything left after every reachable bucket is recorded as principal
    (overpayment; see :class:`PaymentAllocation`), unchanged.
    """
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")

    order = _resolved_repayment_priority(priority)

    interest = 0
    principal = 0
    fees = 0
    fees_capacity = balances.fees_due_cents
    remaining = amount_cents

    for category in order:
        if remaining <= 0:
            break
        if category == "interest":
            interest = min(remaining, balances.interest_due_cents)
            remaining -= interest
        elif category == "principal":
            principal = min(remaining, balances.outstanding_principal_cents)
            remaining -= principal
        elif category in _SCHEDULED_FEE_CATEGORIES:
            take = min(remaining, fees_capacity)
            fees += take
            fees_capacity -= take
            remaining -= take
        # "nsf": unreachable from a regular payment — no-op by construction.

    # Residual cash: keep it on the row as principal so the ledger row ties out
    # to the cash received; the engine clamps outstanding principal at zero.
    return PaymentAllocation(
        interest_cents=interest,
        principal_cents=principal + remaining,
        fees_cents=fees,
        add_on_cents=0,
        overpayment_cents=remaining,
    )


def allocate_add_on_payment(
    amount_cents: int, balances: BalanceView
) -> PaymentAllocation:
    """Turnkey "Add-on" repayment mode: pays the ADD-ON bucket ONLY.

    Dave (03__WP_Servicing f0053+): "add-on payments target the add-on section
    … where any fees that don't incur interest live — NSF fees as an example."
    No interest, principal or scheduled-fee allocation ever happens here, and
    the amount may not exceed the current add-on balance (there is nowhere else
    for the cash to go — an overshoot would silently strand money).
    """
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")
    if amount_cents > balances.add_on_balance_cents:
        raise ValueError(
            f"add-on payment ({amount_cents}) exceeds the add-on balance "
            f"({balances.add_on_balance_cents})"
        )
    return PaymentAllocation(
        interest_cents=0,
        principal_cents=0,
        fees_cents=0,
        add_on_cents=amount_cents,
        overpayment_cents=0,
    )


def allocate_special_payment(
    amount_cents: int, balances: BalanceView
) -> PaymentAllocation:
    """Turnkey "Special" repayment mode: 100% to PRINCIPAL, bypassing interest.

    Dave: "a special payment is designed to go 100 percent to principal."
    Staff-only + permission-gated at the endpoint layer (this pure function is
    policy-free). The amount may not exceed outstanding principal — a special
    payment beyond principal has no defined bucket for the excess.
    """
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")
    if amount_cents > balances.outstanding_principal_cents:
        raise ValueError(
            f"special payment ({amount_cents}) exceeds outstanding principal "
            f"({balances.outstanding_principal_cents})"
        )
    return PaymentAllocation(
        interest_cents=0,
        principal_cents=amount_cents,
        fees_cents=0,
        add_on_cents=0,
        overpayment_cents=0,
    )


def allocate_payoff_payment(
    amount_cents: int, balances: BalanceView
) -> PaymentAllocation:
    """Turnkey "Payoff" repayment mode: the fixed, NON-EDITABLE closing amount.

    Dave: "payoff is designed to close the account and calculate a fixed amount
    that is not editable" = all principal + accrued interest + fees due +
    add-on balance as of the effective date (0% future interest by
    construction). The amount MUST equal the server-computed payoff exactly —
    the server computes, the client only confirms.
    """
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")
    if amount_cents != balances.payoff_cents:
        raise ValueError(
            f"payoff amount ({amount_cents}) must equal the computed payoff "
            f"({balances.payoff_cents}) as of {balances.as_of.isoformat()}"
        )
    return PaymentAllocation(
        interest_cents=balances.interest_due_cents,
        principal_cents=balances.outstanding_principal_cents,
        fees_cents=balances.fees_due_cents,
        add_on_cents=balances.add_on_balance_cents,
        overpayment_cents=0,
    )


# The four Turnkey repayment modes (03__WP_Servicing §"Submit repayment").
REPAYMENT_MODES = ("regular", "add_on", "special", "payoff")

_ALLOCATORS = {
    "regular": allocate_regular_payment,
    "add_on": allocate_add_on_payment,
    "special": allocate_special_payment,
    "payoff": allocate_payoff_payment,
}


def allocate_payment(
    mode: str,
    amount_cents: int,
    balances: BalanceView,
    repayment_priority: Optional[Sequence[str]] = None,
) -> PaymentAllocation:
    """Dispatch one payment to its repayment-mode allocator (pure).

    ``repayment_priority`` (the product's configured allocation ordering) is
    meaningful only for the ``regular`` mode — the other three modes target a
    single named bucket each (add-on / principal / everything) and have no
    waterfall to order. Passing it for those modes is accepted and ignored.

    Every allocator preserves the BalanceView invariant: applying the returned
    allocation as a LedgerEvent moves the payoff down by exactly
    ``amount_cents - overpayment_cents`` (property-tested in
    tests/test_repayment_modes.py). Raises ``ValueError`` for an unknown mode
    or a mode-rule violation (amount caps / payoff exact-match).
    """
    if mode == "regular":
        return allocate_regular_payment(amount_cents, balances, repayment_priority)
    allocator = _ALLOCATORS.get(mode)
    if allocator is None:
        raise ValueError(
            f"unknown repayment mode {mode!r} (expected one of {REPAYMENT_MODES})"
        )
    return allocator(amount_cents, balances)
