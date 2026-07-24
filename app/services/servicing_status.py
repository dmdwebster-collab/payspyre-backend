"""Account-Due-As-Of / Amount-to-Move servicing-status layer (pure core).

Dave's proprietary servicing-status model (docs/dave_review_2026-07-21/
AMOUNT_TO_MOVE_MODEL.md). It sits ON TOP of the actuals money engine
(``interest_engine`` / ``loan_ledger``) — it NEVER re-derives interest or
balances. Given the outputs the ledger already produces (per-row running
``outstanding_principal_cents`` etc.) plus the contractual amortization
schedule, it computes the *relationship* between the two Dave tables:

  * **Amortization table = contractual obligation** (what SHOULD happen) — the
    source of installment timing. Never re-derive timing from date arithmetic
    when the schedule already encodes it (Dave: "do not calculate installments
    moved by assuming 14 days = bi-weekly").
  * **Ledger = account history** (what DID happen) — cash + deferments.

The computed fields (Dave's worked-example oracle in
``tests/test_servicing_status.py``):

  1. ``paid_to_date_virtual_cents`` — running cash payments + one installment of
     virtual credit per APPROVED deferred installment (deferments count as
     virtual paid WITHOUT cash; interest still accrues through them).
  2. ``account_due_as_of`` — the contractual installment date the account has
     EARNED THROUGH. A partial payment of at least ``move_pct`` of an
     installment advances the account a full installment for delinquency
     purposes (Dave's "collections flexibility to avoid delinquency" lever).
  3. ``amount_to_move_cents`` — the minimum to advance Account-Due-As-Of ONE
     more installment from the CURRENT earned position (NOT the amount to bring
     current, and NOT re-derived from scratch each row — see the spec's
     "previous position" principle).
  4. ``days_past_due`` — measured off Account-Due-As-Of (NOT the calendar /
     oldest-installment), or ``None`` once principal is zero.
  5. ``next_scheduled_payment_date`` — the next amortization due date strictly
     after ``as_of`` (independent of Account-Due-As-Of, never beyond maturity).
  6. ``is_paid_in_full`` — principal AND add-on/fees balances all zero (a loan
     with $0 principal but open fees stays OPEN).

MONEY: integer cents everywhere. ``move_pct`` is a fraction in [0, 1]; it is
quantized to basis points (``move_bps = round(move_pct * 10_000)``) so the
integer arithmetic is exact — Dave's example is 0.50 (5000 bps) and the product
default is 1.00 (10000 bps: strict, only a full installment moves the account).

The pure functions take no DB and read no clock. The one DB-backed helper,
:func:`build_servicing_status`, assembles the inputs for a real loan (it imports
models/hardship lazily so the pure core stays importable in DB-free tests) and
is what the admin servicing read and the live-DPD paths call.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional, Sequence

from app.schemas.pricing_config import PaymentFrequency

# ---------------------------------------------------------------------------
# move_pct <-> basis points
# ---------------------------------------------------------------------------

#: The product default: only a FULL installment moves the account (strict —
#: preserves the pre-existing per-installment DPD semantics for products that
#: never set ``amount_to_move_pct``). Dave's collections-flexibility example
#: uses 0.50; it is set per credit product.
DEFAULT_MOVE_BPS = 10_000


def move_pct_to_bps(move_pct: Optional[float]) -> int:
    """Quantize a ``move_pct`` fraction to integer basis points.

    ``None`` -> :data:`DEFAULT_MOVE_BPS` (1.00, strict). Clamped to [0, 10000].
    """
    if move_pct is None:
        return DEFAULT_MOVE_BPS
    bps = round(float(move_pct) * 10_000)
    return max(0, min(10_000, bps))


# ---------------------------------------------------------------------------
# Rounding — Dave's Excel formulas use ROUND (half AWAY from zero), which is a
# DIFFERENT tie rule from the interest engine's banker's rounding. In the
# worked example minimum_required = 1296.225 must round to 1296.23 (away),
# where banker's would give 1296.22 — so the tie rule is load-bearing.
# ---------------------------------------------------------------------------


def _round_half_away(numerator: int, denominator: int) -> int:
    """Integer ``round(numerator / denominator)``, ties AWAY from zero.

    ``denominator`` must be positive. Exact integer arithmetic (no float)."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    half = denominator // 2
    if numerator >= 0:
        return (numerator + half) // denominator
    return -((-numerator + half) // denominator)


# ---------------------------------------------------------------------------
# Per-frequency date stepping (used only to project BEYOND the schedule; when
# the schedule already has the row we prefer it — Dave's two-table rule).
# ---------------------------------------------------------------------------


def _edate(d: date, months: int) -> date:
    """Excel EDATE: add ``months`` calendar months, clamping the day to the
    target month's last valid day."""
    idx = d.month - 1 + months
    year = d.year + idx // 12
    month = idx % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def step_due_date(first_due: date, n: int, frequency: PaymentFrequency | str) -> date:
    """The date ``n`` installment-periods after ``first_due`` (n=0 -> first_due).

    Per-frequency stepping (Dave's spec): Bi-Weekly +14d each, Weekly +7d,
    Monthly EDATE(+n), Semi-Monthly EDATE(+n//2) then +15d when n is odd. Used
    only when the contractual schedule does not already carry the row.
    """
    if n <= 0:
        return first_due
    freq = PaymentFrequency(frequency)
    if freq is PaymentFrequency.WEEKLY:
        return first_due + timedelta(days=7 * n)
    if freq is PaymentFrequency.BI_WEEKLY:
        return first_due + timedelta(days=14 * n)
    if freq is PaymentFrequency.MONTHLY:
        return _edate(first_due, n)
    if freq is PaymentFrequency.SEMI_MONTHLY:
        base = _edate(first_due, n // 2)
        return base + timedelta(days=15) if n % 2 else base
    raise ValueError(f"unsupported frequency {frequency!r}")


def advance_due_date(
    first_due: date,
    n: int,
    frequency: PaymentFrequency | str,
    schedule_due_dates: Optional[Sequence[date]] = None,
) -> date:
    """The contractual due date ``n`` installments out from ``first_due``.

    PREFERS the amortization schedule's actual due date (``schedule_due_dates``
    is 0-indexed: index 0 == first_due) when the row exists — the schedule is
    the source of truth for timing. Falls back to :func:`step_due_date` only to
    project past the last scheduled installment (an account paid far ahead of
    maturity).
    """
    if schedule_due_dates and 0 <= n < len(schedule_due_dates):
        return schedule_due_dates[n]
    return step_due_date(first_due, n, frequency)


# ---------------------------------------------------------------------------
# Core scalar primitives (Dave's exact integer/rounding semantics)
# ---------------------------------------------------------------------------


def installments_moved(
    paid_virtual_cents: int, installment_cents: int, move_bps: int
) -> int:
    """How many full installments the account has EARNED THROUGH (Dave's AA).

    ``InstallmentsMoved = 0 if paid_virtual < installment*move% else
    INT((paid_virtual - installment*move%) / installment) + 1``.

    Exact integer arithmetic — ``installment*move%`` is ``installment_cents *
    move_bps / 10_000`` (a fractional cent, e.g. 61.725), so everything is
    scaled by 10_000 to keep the ``INT`` (floor, on a non-negative value in the
    live branch) exact.
    """
    if installment_cents <= 0:
        return 0
    threshold = installment_cents * move_bps  # == installment*move% * 10_000
    scaled_paid = paid_virtual_cents * 10_000
    if scaled_paid < threshold:
        return 0
    numerator = scaled_paid - threshold  # == (paid_virtual - installment*move%) * 10_000
    denominator = installment_cents * 10_000
    return numerator // denominator + 1


def account_due_as_of(
    paid_virtual_cents: int,
    installment_cents: int,
    move_bps: int,
    first_due: date,
    frequency: PaymentFrequency | str,
    schedule_due_dates: Optional[Sequence[date]] = None,
) -> date:
    """The contractual installment date the account has earned through."""
    moved = installments_moved(paid_virtual_cents, installment_cents, move_bps)
    return advance_due_date(first_due, moved, frequency, schedule_due_dates)


def amount_to_move_cents(
    installments_moved_now: int,
    installment_cents: int,
    move_bps: int,
    paid_virtual_cents: int,
) -> int:
    """Minimum cash to advance Account-Due-As-Of ONE more installment.

    From the CURRENT earned position (``installments_moved_now`` == periods
    between first_due and the current Account-Due-As-Of):

      ``installments_required = installments_moved_now + 1``
      ``minimum_required = ROUND(installments_required*installment
                                 - installment*(1 - move%), 2)``   [half away]
      ``amount_to_move   = MAX(minimum_required - paid_virtual, 0)``

    Computed from the current position (NOT re-derived per row) so later
    positions do not wrongly keep showing movement owed. It is the amount to
    advance by ONE installment, NOT the amount to bring the account current.
    """
    installments_required = installments_moved_now + 1
    # installment*(1 - move%) == installment_cents * (10_000 - move_bps) / 10_000.
    # Scale the whole expression by 10_000, then round-half-away back to cents.
    scaled = (
        installments_required * installment_cents * 10_000
        - installment_cents * (10_000 - move_bps)
    )
    minimum_required_cents = _round_half_away(scaled, 10_000)
    return max(minimum_required_cents - paid_virtual_cents, 0)


def days_past_due(
    as_of: date, account_due_as_of_date: date, outstanding_principal_cents: int
) -> Optional[int]:
    """DPD measured off Account-Due-As-Of. ``None`` once principal is zero.

    ``IF(outstanding_principal > 0, MAX(0, as_of - account_due_as_of), None)``.
    An account paid ahead (Account-Due-As-Of in the future) is 0 DPD.
    """
    if outstanding_principal_cents <= 0:
        return None
    return max(0, (as_of - account_due_as_of_date).days)


def next_scheduled_payment_date(
    as_of: date,
    schedule_due_dates: Sequence[date],
    maturity_date: Optional[date] = None,
) -> Optional[date]:
    """The next amortization due date strictly after ``as_of``.

    Independent of Account-Due-As-Of; never beyond ``maturity_date`` (defaults
    to the last scheduled due date); never from ledger history.
    """
    dates = sorted(schedule_due_dates)
    if maturity_date is None and dates:
        maturity_date = dates[-1]
    for d in dates:
        if d > as_of and (maturity_date is None or d <= maturity_date):
            return d
    return None


def is_paid_in_full(
    outstanding_principal_cents: int,
    fees_due_cents: int,
    add_on_balance_cents: int,
) -> bool:
    """PAID IN FULL requires principal AND add-on/fees balances all zero (a loan
    with $0 principal but open fees stays OPEN)."""
    return (
        outstanding_principal_cents <= 0
        and fees_due_cents <= 0
        and add_on_balance_cents <= 0
    )


# ---------------------------------------------------------------------------
# Composite (pure) — full servicing status from ledger rows + schedule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServicingLedgerRow:
    """One servicing event, already reduced from the money ledger + hardship.

    ``cash_paid_cents`` — actual cash allocated at this event (0 for a
    deferment). ``deferment_installments`` — installments credited as virtual
    paid at this event (normally 0; a deferment credits 1). ``outstanding_
    principal_cents`` / ``fees_due_cents`` / ``add_on_balance_cents`` — the
    RUNNING balances after this event, taken verbatim from
    ``loan_ledger.ledger_view`` (this module never recomputes them).
    """

    effective_date: date
    cash_paid_cents: int
    deferment_installments: int = 0
    outstanding_principal_cents: int = 0
    fees_due_cents: int = 0
    add_on_balance_cents: int = 0


@dataclass(frozen=True)
class ServicingRowStatus:
    """Per-row servicing status (the ledger's "Account Due As Of" column)."""

    effective_date: date
    paid_to_date_virtual_cents: int
    installments_moved: int
    account_due_as_of: date
    outstanding_principal_cents: int


@dataclass(frozen=True)
class ServicingStatus:
    """The servicing-status snapshot at ``as_of`` plus the per-row progression."""

    as_of: date
    account_due_as_of: date
    amount_to_move_cents: int
    days_past_due: Optional[int]
    paid_to_date_virtual_cents: int
    next_scheduled_payment_date: Optional[date]
    outstanding_principal_cents: int
    fees_due_cents: int
    add_on_balance_cents: int
    is_paid_in_full: bool
    move_bps: int
    rows: tuple[ServicingRowStatus, ...]

    def to_api_fields(self) -> dict:
        """The additive fields exposed on the admin loan servicing read."""
        return {
            "account_due_as_of": self.account_due_as_of.isoformat(),
            "amount_to_move_cents": self.amount_to_move_cents,
            "days_past_due": self.days_past_due,
            "paid_to_date_virtual_cents": self.paid_to_date_virtual_cents,
            "next_scheduled_payment_date": (
                self.next_scheduled_payment_date.isoformat()
                if self.next_scheduled_payment_date
                else None
            ),
            "is_paid_in_full": self.is_paid_in_full,
        }


def compute_servicing_status(
    *,
    frequency: PaymentFrequency | str,
    installment_cents: int,
    first_due_date: date,
    move_pct: Optional[float] = None,
    move_bps: Optional[int] = None,
    schedule_due_dates: Sequence[date],
    ledger_rows: Sequence[ServicingLedgerRow],
    as_of: date,
    as_of_outstanding_principal_cents: int,
    as_of_fees_due_cents: int = 0,
    as_of_add_on_balance_cents: int = 0,
    maturity_date: Optional[date] = None,
) -> ServicingStatus:
    """Full servicing status from the reduced ledger rows + contractual schedule.

    ``ledger_rows`` MUST be sorted by ``effective_date`` (replay order) and are
    the cash + deferment events; their running balances come from
    ``loan_ledger``. ``as_of_*`` are the balance-view figures at ``as_of``
    (also from ``loan_ledger``) — used for DPD gating and the paid-in-full test,
    so they are correct even when ``as_of`` is after the last event.

    Pass ``move_bps`` directly, or ``move_pct`` (quantized); ``move_bps`` wins.
    """
    resolved_bps = move_bps if move_bps is not None else move_pct_to_bps(move_pct)

    schedule_list = list(schedule_due_dates)
    if maturity_date is None and schedule_list:
        maturity_date = max(schedule_list)

    paid_virtual = 0
    rows_out: list[ServicingRowStatus] = []
    final_moved = 0
    final_aoda = first_due_date

    for row in ledger_rows:
        if row.effective_date > as_of:
            continue
        paid_virtual += row.cash_paid_cents + row.deferment_installments * installment_cents
        moved = installments_moved(paid_virtual, installment_cents, resolved_bps)
        aoda = advance_due_date(first_due_date, moved, frequency, schedule_list)
        rows_out.append(
            ServicingRowStatus(
                effective_date=row.effective_date,
                paid_to_date_virtual_cents=paid_virtual,
                installments_moved=moved,
                account_due_as_of=aoda,
                outstanding_principal_cents=row.outstanding_principal_cents,
            )
        )
        final_moved = moved
        final_aoda = aoda

    move_amount = amount_to_move_cents(
        final_moved, installment_cents, resolved_bps, paid_virtual
    )
    dpd = days_past_due(as_of, final_aoda, as_of_outstanding_principal_cents)
    next_due = next_scheduled_payment_date(as_of, schedule_list, maturity_date)

    return ServicingStatus(
        as_of=as_of,
        account_due_as_of=final_aoda,
        amount_to_move_cents=move_amount,
        days_past_due=dpd,
        paid_to_date_virtual_cents=paid_virtual,
        next_scheduled_payment_date=next_due,
        outstanding_principal_cents=as_of_outstanding_principal_cents,
        fees_due_cents=as_of_fees_due_cents,
        add_on_balance_cents=as_of_add_on_balance_cents,
        is_paid_in_full=is_paid_in_full(
            as_of_outstanding_principal_cents,
            as_of_fees_due_cents,
            as_of_add_on_balance_cents,
        ),
        move_bps=resolved_bps,
        rows=tuple(rows_out),
    )


# ---------------------------------------------------------------------------
# DB-backed resolver — assembles the pure inputs for a real loan.
#
# Imports models/services LAZILY so the pure core above stays importable in
# DB-free tests. This is the seam the admin servicing read and the live-DPD
# paths call.
# ---------------------------------------------------------------------------


def _infer_frequency(schedule_due_dates: Sequence[date]) -> PaymentFrequency:
    """Best-effort frequency from the schedule's own cadence.

    account_due_as_of and next_scheduled index the schedule directly, so the
    frequency only matters for projecting BEYOND maturity (an account paid far
    ahead). We read it off the first gap, defaulting to MONTHLY.
    """
    if len(schedule_due_dates) < 2:
        return PaymentFrequency.MONTHLY
    ordered = sorted(schedule_due_dates)
    gap = (ordered[1] - ordered[0]).days
    if gap <= 8:
        return PaymentFrequency.WEEKLY
    if gap <= 16:
        # 14 == bi-weekly; ~15/16 alternating == semi-monthly. A second gap
        # disambiguates when present.
        if len(ordered) >= 3:
            gap2 = (ordered[2] - ordered[1]).days
            if gap == 14 and gap2 == 14:
                return PaymentFrequency.BI_WEEKLY
            return PaymentFrequency.SEMI_MONTHLY
        return PaymentFrequency.BI_WEEKLY
    return PaymentFrequency.MONTHLY


def resolve_move_bps(db, loan) -> int:
    """The loan's product ``amount_to_move_pct`` as basis points, fail-soft.

    Walks loan -> application -> credit product -> ``policy_config.
    amount_to_move_pct``. Anything missing/invalid -> :data:`DEFAULT_MOVE_BPS`
    (1.00, strict) — the behaviour every existing product has, since the field
    is new and every stored ``policy_config`` predates it.
    """
    from app.models.platform.credit_application import PlatformCreditApplication
    from app.models.platform.credit_product import PlatformCreditProduct
    from app.schemas.product_policy_config import (
        ProductPolicyConfigError,
        parse_product_policy_config,
    )

    application_id = getattr(loan, "application_id", None)
    if application_id is None:
        return DEFAULT_MOVE_BPS
    app_row = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    product_id = getattr(app_row, "credit_product_id", None)
    if product_id is None:
        return DEFAULT_MOVE_BPS
    product = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.id == product_id)
        .first()
    )
    raw = getattr(product, "policy_config", None)
    if not raw:
        return DEFAULT_MOVE_BPS
    try:
        cfg = parse_product_policy_config(raw)
    except ProductPolicyConfigError:
        return DEFAULT_MOVE_BPS
    return move_pct_to_bps(cfg.amount_to_move_pct)


def _approved_deferment_events(db, loan) -> list[tuple[date, int]]:
    """(effective_date, installments) for each APPROVED hardship deferment.

    A deferment credits one installment of virtual-paid per deferred
    installment (Dave: "Deferments count as virtual payments"), WITHOUT cash —
    interest keeps accruing. We count applied deferments (status active /
    completed with an ``applied_at``); the credit lands at the applied date.

    KNOWN OPEN QUESTION (flagged for Dave / PR): the deferment also SUSPENDS the
    installment and APPENDS its amount as an end-of-contract custom transaction.
    If that appended amount is later paid in CASH, that cash is also counted in
    paid-to-date-virtual, so the deferred installment could be credited twice
    toward Account-Due-As-Of. Dave's worked example never repays the appended
    amount, so it does not arise there; we implement the straightforward virtual
    credit and leave the reconciliation of the appended repayment to Dave.
    """
    from app.models.platform.hardship import PlatformHardshipRequest

    rows = (
        db.query(PlatformHardshipRequest)
        .filter(PlatformHardshipRequest.loan_id == loan.id)
        .all()
    )
    events: list[tuple[date, int]] = []
    for row in rows:
        if row.kind != "deferment" or row.status not in ("active", "completed"):
            continue
        applied = row.applied_at
        if applied is None:
            continue
        eff = applied.date() if hasattr(applied, "date") else applied
        count = len((row.params or {}).get("installment_ids", []))
        if count > 0:
            events.append((eff, count))
    return events


def build_servicing_status(db, loan, as_of: date) -> Optional[ServicingStatus]:
    """Assemble and compute the servicing status for a real loan (DB-backed).

    Returns ``None`` for a loan with no amortization schedule (nothing to
    anchor Account-Due-As-Of on — e.g. still pending disbursement). Reads the
    running balances from ``loan_ledger`` (never recomputes interest) and merges
    approved hardship deferments as virtual credits.
    """
    from app.services import loan_ledger

    schedule = sorted(loan.schedule or [], key=lambda s: s.installment_number)
    if not schedule:
        return None

    installment_cents = schedule[0].total_cents
    first_due = schedule[0].due_date
    schedule_due_dates = [s.due_date for s in schedule]
    maturity = max(schedule_due_dates)
    frequency = _infer_frequency(schedule_due_dates)
    move_bps = resolve_move_bps(db, loan)

    # Cash events from the immutable ledger + their running balances (the money
    # engine's own outputs — this module never recomputes them).
    view = loan_ledger.ledger_view(loan, as_of)
    type_by_id = {t["id"]: t["txn_type"] for t in view["transactions"]}
    ledger_rows: list[ServicingLedgerRow] = []
    for t in view["transactions"]:
        eff = date.fromisoformat(t["effective_date"])
        cash = 0
        if t["txn_type"] == "payment":
            cash = t["amount_cents"]
        elif t["txn_type"] == "reversal":
            # A reversal of a cash payment removes that cash from paid-to-date.
            orig = type_by_id.get(t["reverses_transaction_id"])
            if orig == "payment":
                cash = -t["amount_cents"]
        rb = t["running_balances"]
        ledger_rows.append(
            ServicingLedgerRow(
                effective_date=eff,
                cash_paid_cents=cash,
                deferment_installments=0,
                outstanding_principal_cents=rb["outstanding_principal_cents"],
                fees_due_cents=rb["fees_due_cents"],
                add_on_balance_cents=rb["add_on_balance_cents"],
            )
        )

    # Deferment virtual-credit events (no cash; balance is the ledger balance at
    # the deferment's effective date — principal unchanged, interest accrued).
    for eff, count in _approved_deferment_events(db, loan):
        if eff > as_of:
            continue
        bal = loan_ledger.loan_balances(loan, eff)
        ledger_rows.append(
            ServicingLedgerRow(
                effective_date=eff,
                cash_paid_cents=0,
                deferment_installments=count,
                outstanding_principal_cents=bal.outstanding_principal_cents,
                fees_due_cents=bal.fees_due_cents,
                add_on_balance_cents=bal.add_on_balance_cents,
            )
        )

    # Stable sort by effective date (payments recorded before a same-day
    # deferment keep their order).
    ledger_rows.sort(key=lambda r: r.effective_date)

    balances = loan_ledger.loan_balances(loan, as_of)
    return compute_servicing_status(
        frequency=frequency,
        installment_cents=installment_cents,
        first_due_date=first_due,
        move_bps=move_bps,
        schedule_due_dates=schedule_due_dates,
        ledger_rows=ledger_rows,
        as_of=as_of,
        as_of_outstanding_principal_cents=balances.outstanding_principal_cents,
        as_of_fees_due_cents=balances.fees_due_cents,
        as_of_add_on_balance_cents=balances.add_on_balance_cents,
        maturity_date=maturity,
    )
