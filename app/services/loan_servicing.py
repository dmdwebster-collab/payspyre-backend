"""Loan Servicing (LMS) service — P9.0 spine.

This module is the foundational servicing layer that replaces Turnkey Lender's
LMS responsibilities. It covers three things, in order of trust:

1. ``generate_amortization_schedule`` — a PURE function computing a standard
   equal-payment (fully amortizing) schedule with exact integer-cent arithmetic.
   No DB, no side effects. This is the math the rest of the LMS is built on, so
   it is the most heavily tested piece.

2. ``create_loan_from_application`` — books a PlatformLoan + its schedule from an
   approved application.

3. ``record_payment`` / ``get_loan_status`` — apply received funds against the
   schedule (oldest unpaid installment first) and report state.

MONEY: everything is integer cents. We never use float for stored balances. The
only float used is inside the amortization math (the standard annuity payment
formula); it is immediately quantized to cents and the rounding remainder is
reconciled into the final installment so the schedule sums EXACTLY to
principal + total interest.
"""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanPayment,
    PlatformLoanScheduleItem,
    PlatformLoanStatement,
)


# Money-IN audit event. Mirrors the money-OUT events emitted by
# ``loan_lifecycle`` (loan_booked / loan_disbursed) so the LMS cash path leaves a
# complete auditable history. Rows carry only ids + amounts (cents) — never PII.
LOAN_PAYMENT_RECORDED_EVENT = "loan_payment_recorded"


# ---------------------------------------------------------------------------
# Defaults / assumptions (documented)
# ---------------------------------------------------------------------------
#
# When an approved application's decision does not carry explicit pricing, we
# fall back to the product's pricing_config, then to these conservative defaults.
# See ``_resolve_pricing`` for the precedence order.
_DEFAULT_ANNUAL_RATE_BPS = 1299  # 12.99% APR
_DEFAULT_TERM_MONTHS = 24


def _add_months(d: date, months: int) -> date:
    """Return ``d`` advanced by ``months`` calendar months, clamping the day to
    the last valid day of the target month (so e.g. Jan 31 + 1 month -> Feb 28).
    """
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    # Last day of target month: day 1 of the following month minus one day logic,
    # done without external libs.
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    last_day = (next_month_first.toordinal() - 1)
    last_day_of_month = date.fromordinal(last_day).day
    day = min(d.day, last_day_of_month)
    return date(year, month, day)


@dataclass(frozen=True)
class ScheduleRow:
    """A single computed installment (pure, DB-free)."""

    installment_number: int
    due_date: date
    principal_cents: int
    interest_cents: int
    total_cents: int


def _actual_360_schedule(
    principal_cents: int,
    annual_rate_bps: int,
    term_months: int,
    first_due_date: date,
    accrual_start_date: date,
) -> list[ScheduleRow]:
    """Equal-payment schedule with actual/360 interest accrual (Turnkey-legacy).

    Interest each period = balance * annual_rate * actual_days / 360, where ``days``
    is the real calendar gap to the due date (and accrual_start -> first due for the
    first installment). The regular payment is solved (integer cents, by bisection) so
    the loan fully amortizes within ``term_months``; the final installment absorbs the
    rounding remainder, preserving the SAME exact tie-out invariants as 30/360.
    """
    annual = annual_rate_bps / 10_000.0
    due_dates = [_add_months(first_due_date, n) for n in range(term_months)]
    period_starts = [accrual_start_date] + due_dates[:-1]
    days = [max(0, (due_dates[i] - period_starts[i]).days) for i in range(term_months)]

    def interest_at(balance: int, i: int) -> int:
        return int(round(balance * annual * days[i] / 360.0))

    if annual == 0.0:
        # Interest-free: even principal split, remainder to the final row.
        base = principal_cents // term_months
        rows: list[ScheduleRow] = []
        running = 0
        for n in range(1, term_months + 1):
            prin = base if n < term_months else principal_cents - running
            running += prin
            rows.append(ScheduleRow(n, due_dates[n - 1], prin, 0, prin))
        return rows

    def balance_after_term(payment: int) -> int:
        bal = principal_cents
        for i in range(term_months):
            prin = payment - interest_at(bal, i)
            prin = 0 if prin < 0 else (bal if prin > bal else prin)
            bal -= prin
        return bal

    # Bisection for the smallest integer-cent payment that amortizes within term.
    lo = principal_cents // term_months  # floor: the zero-interest payment
    hi = lo + interest_at(principal_cents, 0) + 1
    while balance_after_term(hi) > 0:
        hi *= 2
    while lo < hi:
        mid = (lo + hi) // 2
        if balance_after_term(mid) <= 0:
            hi = mid
        else:
            lo = mid + 1
    payment_cents = lo

    rows = []
    balance = principal_cents
    for n in range(1, term_months + 1):
        interest = interest_at(balance, n - 1)
        if n < term_months:
            principal_part = payment_cents - interest
            principal_part = 0 if principal_part < 0 else (
                balance if principal_part > balance else principal_part
            )
            balance -= principal_part
        else:
            principal_part = balance  # final installment absorbs the remainder exactly
            balance = 0
        rows.append(
            ScheduleRow(
                installment_number=n,
                due_date=due_dates[n - 1],
                principal_cents=principal_part,
                interest_cents=interest,
                total_cents=principal_part + interest,
            )
        )
    return rows


def generate_amortization_schedule(
    principal_cents: int,
    annual_rate_bps: int,
    term_months: int,
    first_due_date: date,
    *,
    day_count: str = "30/360",
    accrual_start_date: Optional[date] = None,
) -> list[ScheduleRow]:
    """Compute a standard fully-amortizing (equal-payment) schedule.

    PURE function — no DB, no side effects, deterministic.

    ``day_count`` selects how per-period interest accrues:
      * ``"30/360"`` (default): every period is a flat month, interest = balance *
        annual_rate / 12. This is PaySpyre's native convention.
      * ``"actual/360"``: interest = balance * annual_rate * ACTUAL_DAYS / 360, where
        the day count is the real calendar gap between consecutive due dates (and, for
        the first installment, between ``accrual_start_date`` and ``first_due_date``).
        This matches Turnkey Lender's legacy book (reconciled to the cent against real
        loans), so migrated/legacy-consistent loans can be originated identically.
        ``accrual_start_date`` defaults to one month before ``first_due_date``.

    Method (30/360):
      * Monthly rate r = annual_rate_bps / 10_000 / 12.
      * For r == 0 (interest-free): payment = principal / term, all interest 0.
      * Otherwise the standard annuity payment:
            P = principal * r / (1 - (1 + r)^-term)
        is computed in float, then quantized to whole cents.
      * Each period: interest = round(balance * r); principal = payment - interest;
        balance -= principal.
      * EXACT TIE-OUT: floating-point + rounding leaves a few cents of drift. The
        FINAL installment absorbs all remaining principal (principal_cents =
        outstanding balance) and its interest is computed on that balance, so:
            sum(principal_cents) == principal_cents (input), and
            sum(total_cents)     == principal + sum(interest_cents) exactly.
        No fractional cents ever escape.

    Both conventions guarantee the same exact tie-out invariants.

    Returns rows ordered by installment_number (1-based). Due dates step monthly
    from ``first_due_date``.
    """
    if principal_cents <= 0:
        raise ValueError("principal_cents must be positive")
    if term_months <= 0:
        raise ValueError("term_months must be positive")
    if annual_rate_bps < 0:
        raise ValueError("annual_rate_bps must be non-negative")
    if day_count not in ("30/360", "actual/360"):
        raise ValueError("day_count must be '30/360' or 'actual/360'")

    if day_count == "actual/360":
        return _actual_360_schedule(
            principal_cents,
            annual_rate_bps,
            term_months,
            first_due_date,
            accrual_start_date or _add_months(first_due_date, -1),
        )

    monthly_rate = (annual_rate_bps / 10_000.0) / 12.0

    if monthly_rate == 0.0:
        # Interest-free: split principal evenly, remainder to the final row.
        base = principal_cents // term_months
        rows: list[ScheduleRow] = []
        running = 0
        for n in range(1, term_months + 1):
            if n < term_months:
                prin = base
            else:
                prin = principal_cents - running  # absorb remainder
            running += prin
            rows.append(
                ScheduleRow(
                    installment_number=n,
                    due_date=_add_months(first_due_date, n - 1),
                    principal_cents=prin,
                    interest_cents=0,
                    total_cents=prin,
                )
            )
        return rows

    # Standard annuity payment, quantized to whole cents.
    factor = (1.0 + monthly_rate) ** (-term_months)
    payment = principal_cents * monthly_rate / (1.0 - factor)
    payment_cents = int(round(payment))

    rows = []
    balance = principal_cents
    for n in range(1, term_months + 1):
        if n < term_months:
            interest = int(round(balance * monthly_rate))
            principal_part = payment_cents - interest
            # Guard: never amortize more principal than remains.
            if principal_part > balance:
                principal_part = balance
            balance -= principal_part
            total = principal_part + interest
        else:
            # Final installment absorbs all remaining principal exactly.
            interest = int(round(balance * monthly_rate))
            principal_part = balance
            balance = 0
            total = principal_part + interest

        rows.append(
            ScheduleRow(
                installment_number=n,
                due_date=_add_months(first_due_date, n - 1),
                principal_cents=principal_part,
                interest_cents=interest,
                total_cents=total,
            )
        )

    return rows


def _resolve_pricing(application: PlatformCreditApplication) -> tuple[int, int, int]:
    """Resolve (principal_cents, annual_rate_bps, term_months) for booking.

    Precedence / assumptions (documented):
      * PRINCIPAL = the application's ``requested_amount_cents`` (the approved
        amount). If the decision carries an explicit ``amount_cents`` it wins
        (the decision can approve a different amount than requested).
      * RATE  — first the decision's ``apr_bps``; else the product's
        ``pricing_config`` (``apr_bps`` if present, else first value of
        ``apr_range`` interpreted as a percentage -> bps); else
        ``_DEFAULT_ANNUAL_RATE_BPS``.
      * TERM  — first the decision's ``term_months``; else the product's
        ``pricing_config`` (``term_months`` if present, else first of
        ``term_options``); else ``_DEFAULT_TERM_MONTHS``.
    """
    decision = application.decision or {}

    # Principal
    principal_cents = decision.get("amount_cents")
    if principal_cents is None:
        principal_cents = application.requested_amount_cents
    if not principal_cents or principal_cents <= 0:
        raise ValueError("Cannot book a loan with non-positive principal")

    product = getattr(application, "credit_product", None)
    pricing_config = (getattr(product, "pricing_config", None) or {}) if product else {}

    # Rate (bps)
    annual_rate_bps = decision.get("apr_bps")
    if annual_rate_bps is None:
        if "apr_bps" in pricing_config:
            annual_rate_bps = pricing_config["apr_bps"]
        elif pricing_config.get("apr_range"):
            # apr_range is in PERCENT (e.g. [7.99, 28.99]); use the floor.
            annual_rate_bps = int(round(float(pricing_config["apr_range"][0]) * 100))
        else:
            annual_rate_bps = _DEFAULT_ANNUAL_RATE_BPS

    # Term (months)
    term_months = decision.get("term_months")
    if term_months is None:
        if "term_months" in pricing_config:
            term_months = pricing_config["term_months"]
        elif pricing_config.get("term_options"):
            term_months = pricing_config["term_options"][0]
        else:
            term_months = _DEFAULT_TERM_MONTHS

    return int(principal_cents), int(annual_rate_bps), int(term_months)


def create_loan_from_application(
    db: Session,
    application: PlatformCreditApplication,
    first_due_date: Optional[date] = None,
) -> PlatformLoan:
    """Book a PlatformLoan + amortization schedule from an APPROVED application.

    The loan is created in ``pending_disbursement`` with the full principal as
    its balance and the schedule generated. It does NOT disburse funds and does
    NOT move to ``active`` — disbursement (Zumrails payout) and the executed loan
    agreement (SignNow) are downstream seams handled outside this PR; flipping to
    ``active`` + setting ``disbursed_at`` happens then.

    ``first_due_date`` defaults to one month from today.
    """
    if application.status != "approved":
        raise ValueError(
            f"Cannot book a loan from application {application.id} with "
            f"status '{application.status}' (must be 'approved')"
        )

    principal_cents, annual_rate_bps, term_months = _resolve_pricing(application)

    if first_due_date is None:
        first_due_date = _add_months(date.today(), 1)

    currency = "CAD"
    product = getattr(application, "credit_product", None)
    if product is not None and getattr(product, "currency", None):
        currency = product.currency

    loan = PlatformLoan(
        application_id=application.id,
        principal_cents=principal_cents,
        annual_rate_bps=annual_rate_bps,
        term_months=term_months,
        status="pending_disbursement",
        principal_balance_cents=principal_cents,
        currency=currency,
    )
    db.add(loan)
    db.flush()  # assign loan.id without committing the whole unit of work

    for row in generate_amortization_schedule(
        principal_cents, annual_rate_bps, term_months, first_due_date
    ):
        db.add(
            PlatformLoanScheduleItem(
                loan_id=loan.id,
                installment_number=row.installment_number,
                due_date=row.due_date,
                principal_cents=row.principal_cents,
                interest_cents=row.interest_cents,
                total_cents=row.total_cents,
                status="scheduled",
                paid_cents=0,
            )
        )

    db.commit()
    db.refresh(loan)
    return loan


def record_payment(
    db: Session,
    loan: PlatformLoan,
    amount_cents: int,
    received_at: datetime,
    method: str,
    external_ref: Optional[str] = None,
) -> PlatformLoanPayment:
    """Apply a received payment to a loan.

    APPLICATION ORDER: oldest unpaid/partial installment first (by
    installment_number). The payment is consumed across installments until the
    funds are exhausted; each installment is filled up to its ``total_cents``.

    Side effects:
      * Inserts a PlatformLoanPayment row (the cash receipt / audit trail).
      * Increments ``paid_cents`` on the installments it covers and flips their
        status: ``paid`` when fully covered, ``partial`` otherwise.
      * Reduces ``loan.principal_balance_cents`` by the PRINCIPAL portion of the
        installments covered (interest does not reduce principal balance).
      * Flips the loan to ``paid_off`` once every installment is fully paid.

    Overpayment beyond the final installment is recorded on the payment row but
    not auto-refunded (out of scope for the spine) — it simply leaves the loan
    paid off with the receipt on file.
    """
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")

    # Idempotency: a payment rail (Zumrails collection webhook) can deliver the same
    # receipt more than once (at-least-once / retries). Dedupe on external_ref so a
    # replay returns the original receipt instead of double-applying cash to
    # principal — a money-conservation hole. Backed by a partial unique index on
    # (loan_id, external_ref) (migration 033) for the concurrent case.
    if external_ref:
        existing = (
            db.query(PlatformLoanPayment)
            .filter(
                PlatformLoanPayment.loan_id == loan.id,
                PlatformLoanPayment.external_ref == external_ref,
            )
            .first()
        )
        if existing is not None:
            return existing

    payment = PlatformLoanPayment(
        loan_id=loan.id,
        amount_cents=amount_cents,
        received_at=received_at,
        method=method,
        external_ref=external_ref,
    )
    db.add(payment)

    # Schedule rows in due order. Use the relationship if loaded; otherwise query.
    schedule = sorted(
        loan.schedule,
        key=lambda s: s.installment_number,
    )

    remaining = amount_cents
    for item in schedule:
        if remaining <= 0:
            break
        if item.status in ("paid", "waived"):
            continue

        outstanding = item.total_cents - item.paid_cents
        if outstanding <= 0:
            item.status = "paid"
            continue

        applied = min(remaining, outstanding)

        # Principal portion of THIS applied amount: principal is filled before
        # interest within the installment's own (principal, interest) split.
        principal_outstanding = max(
            0, item.principal_cents - max(0, item.paid_cents)
        )
        principal_applied = min(applied, principal_outstanding)

        item.paid_cents += applied
        remaining -= applied

        if item.paid_cents >= item.total_cents:
            item.status = "paid"
        else:
            item.status = "partial"

        loan.principal_balance_cents = max(
            0, loan.principal_balance_cents - principal_applied
        )

    # Loan-level status transitions.
    if all(s.status in ("paid", "waived") for s in schedule) and schedule:
        loan.status = "paid_off"
        loan.principal_balance_cents = 0
    elif loan.status == "delinquent":
        # Delinquency cure: a delinquent loan returns to ``active`` once it has no
        # overdue, unpaid installment left — the exact inverse of the condition
        # run_delinquency_aging uses to mark it delinquent. Without this, a borrower
        # who catches up stays flagged delinquent forever (and is then excluded from
        # future aging passes, which only re-evaluate ``active`` loans).
        today = datetime.now(timezone.utc).date()
        has_overdue_unpaid = any(
            s.due_date < today and s.status not in ("paid", "waived") for s in schedule
        )
        if not has_overdue_unpaid:
            loan.status = "active"

    # Money-IN audit row, added to the SAME transaction so it persists atomically
    # with the applied payment via the commit below (only reached on a real applied
    # payment — the idempotent-replay path returned the original receipt above).
    # db.add only (no flush/commit/query) keeps this safe under the DB-free fakes;
    # application_id is read straight off the loan (no patient_id lookup).
    application_id = getattr(loan, "application_id", None)
    db.add(
        PlatformEvent(
            event_type=LOAN_PAYMENT_RECORDED_EVENT,
            actor="system",
            application_id=application_id,
            payload={
                "v": 1,
                "actor": {"type": "system", "id": "system"},
                "loan_id": str(loan.id),
                "application_id": str(application_id) if application_id else None,
                "after": {
                    "amount_cents": amount_cents,
                    "external_ref": external_ref,
                    "method": method,
                    "loan_status": loan.status,
                    "principal_balance_cents": loan.principal_balance_cents,
                },
            },
        )
    )

    db.commit()
    db.refresh(payment)
    return payment


def get_loan_status(db: Session, loan_id: UUID) -> Optional[dict]:
    """Return a status snapshot for a loan, or None if not found.

    Snapshot includes core terms, current balance, and a roll-up of the schedule
    (counts by status, next due installment, total scheduled vs. total paid).
    """
    loan = db.query(PlatformLoan).filter(PlatformLoan.id == loan_id).first()
    if loan is None:
        return None

    schedule = sorted(loan.schedule, key=lambda s: s.installment_number)

    total_scheduled = sum(s.total_cents for s in schedule)
    total_paid = sum(s.paid_cents for s in schedule)

    status_counts: dict[str, int] = {}
    for s in schedule:
        status_counts[s.status] = status_counts.get(s.status, 0) + 1

    next_due = next(
        (s for s in schedule if s.status not in ("paid", "waived")), None
    )

    return {
        "loan_id": str(loan.id),
        "application_id": str(loan.application_id),
        "status": loan.status,
        "currency": loan.currency,
        "principal_cents": loan.principal_cents,
        "principal_balance_cents": loan.principal_balance_cents,
        "annual_rate_bps": loan.annual_rate_bps,
        "term_months": loan.term_months,
        "disbursed_at": loan.disbursed_at.isoformat() if loan.disbursed_at else None,
        "schedule": {
            "installments": len(schedule),
            "status_counts": status_counts,
            "total_scheduled_cents": total_scheduled,
            "total_paid_cents": total_paid,
            "total_outstanding_cents": total_scheduled - total_paid,
            "next_due_installment_number": next_due.installment_number if next_due else None,
            "next_due_date": next_due.due_date.isoformat() if next_due else None,
        },
    }


# ===========================================================================
# Delinquency aging
# ===========================================================================


@dataclass(frozen=True)
class AgingResult:
    """Summary of one ``run_delinquency_aging`` pass (DB-free value object)."""

    as_of: date
    installments_flagged_late: int
    loans_marked_delinquent: list[str]


def run_delinquency_aging(db: Session, as_of: date) -> AgingResult:
    """Flip overdue, not-fully-paid installments to ``late`` and their loans to
    ``delinquent``.

    A schedule item is overdue when ``due_date < as_of`` and it is not already
    ``paid``/``waived`` (i.e. status in {scheduled, partial, late}) — any
    unpaid balance past its due date is delinquent. Already-``late`` items stay
    late (idempotent).

    A loan is marked ``delinquent`` when it has at least one overdue, unpaid
    installment AND it is currently ``active`` (we never override a terminal
    status like ``paid_off`` / ``charged_off`` / ``cancelled``, nor disturb a
    loan still in ``pending_disbursement``).

    Designed to run as a scheduled job (e.g. nightly). Idempotent: running it
    twice on the same ``as_of`` produces the same end state. Commits once.

    Returns an ``AgingResult`` summarizing the pass.
    """
    overdue_items = (
        db.query(PlatformLoanScheduleItem)
        .filter(
            PlatformLoanScheduleItem.due_date < as_of,
            PlatformLoanScheduleItem.status.in_(("scheduled", "partial")),
        )
        .all()
    )

    flagged = 0
    affected_loan_ids: set = set()
    for item in overdue_items:
        item.status = "late"
        flagged += 1
        affected_loan_ids.add(item.loan_id)

    # Any loan with a late item (newly flagged or pre-existing) and an active
    # status becomes delinquent.
    late_loan_ids = {
        row[0]
        for row in (
            db.query(PlatformLoanScheduleItem.loan_id)
            .filter(PlatformLoanScheduleItem.status == "late")
            .all()
        )
    }
    late_loan_ids |= affected_loan_ids

    marked: list[str] = []
    if late_loan_ids:
        loans = (
            db.query(PlatformLoan)
            .filter(
                PlatformLoan.id.in_(late_loan_ids),
                PlatformLoan.status == "active",
            )
            .all()
        )
        for loan in loans:
            loan.status = "delinquent"
            marked.append(str(loan.id))

    db.commit()
    return AgingResult(
        as_of=as_of,
        installments_flagged_late=flagged,
        loans_marked_delinquent=marked,
    )


# ===========================================================================
# Statement generation
# ===========================================================================


def generate_statement(
    db: Session,
    loan: PlatformLoan,
    period: tuple[date, date],
) -> PlatformLoanStatement:
    """Generate (and persist) a billing statement for ``loan`` over ``period``.

    ``period`` is an inclusive ``(period_start, period_end)`` window.

    The statement snapshots:
      * ``opening_balance_cents`` — principal balance entering the window,
        reconstructed as ``current principal_balance + principal paid AFTER the
        window's end`` (so it is correct even when generated retroactively).
      * ``principal_paid_cents`` / ``interest_paid_cents`` — split of the cash
        received in the window, allocated principal-first within each payment
        the same way ``record_payment`` does.
      * ``closing_balance_cents`` — ``opening - principal_paid_in_window``.

    Idempotent: regenerating the same (loan, period) returns the existing row.
    Commits once.
    """
    period_start, period_end = period
    if period_end < period_start:
        raise ValueError("period_end must be >= period_start")

    existing = (
        db.query(PlatformLoanStatement)
        .filter(
            PlatformLoanStatement.loan_id == loan.id,
            PlatformLoanStatement.period_start == period_start,
            PlatformLoanStatement.period_end == period_end,
        )
        .first()
    )
    if existing is not None:
        return existing

    payments = sorted(loan.payments, key=lambda p: p.received_at)

    def _in_window(p: PlatformLoanPayment) -> bool:
        d = p.received_at.date()
        return period_start <= d <= period_end

    def _after_window(p: PlatformLoanPayment) -> bool:
        return p.received_at.date() > period_end

    # Split principal/interest by window. CRITICAL: every payment must be replayed
    # against ONE shared schedule state in chronological order — replaying the
    # in-window and after-window subsets independently (each from a zero state)
    # mis-allocates later payments to early installments, corrupting the split for
    # any loan whose payments span more than one period.
    principal_in_window, interest_in_window, principal_after_window = _split_payments_windowed(
        loan, payments, _in_window, _after_window
    )

    # Reconstruct the opening balance: current outstanding principal plus
    # anything paid down after the window's end plus what was paid in-window.
    closing_balance = loan.principal_balance_cents + principal_after_window
    opening_balance = closing_balance + principal_in_window

    statement = PlatformLoanStatement(
        loan_id=loan.id,
        period_start=period_start,
        period_end=period_end,
        opening_balance_cents=opening_balance,
        principal_paid_cents=principal_in_window,
        interest_paid_cents=interest_in_window,
        closing_balance_cents=closing_balance,
    )
    db.add(statement)
    db.commit()
    db.refresh(statement)
    return statement


def _split_payments_windowed(
    loan: PlatformLoan, payments: list, in_window, after_window
) -> tuple[int, int, int]:
    """Replay ALL payments oldest-first against ONE shared schedule state and
    bucket each applied chunk's principal/interest by window.

    Returns ``(principal_in_window, interest_in_window, principal_after_window)``.

    Allocation mirrors ``record_payment``: each payment fills installments
    oldest-first, principal-before-interest within an installment. By maintaining
    a single ``paid`` map across the full chronological payment stream, a payment
    is allocated to the installments it actually paid (after earlier payments
    consumed prior installments) — not to installment 1 from scratch. ``payments``
    should already be sorted by ``received_at``; we sort defensively.
    """
    schedule = sorted(loan.schedule, key=lambda s: s.installment_number)
    paid = {s.installment_number: 0 for s in schedule}

    principal_in = interest_in = principal_after = 0
    for payment in sorted(payments, key=lambda p: p.received_at):
        is_in = in_window(payment)
        is_after = after_window(payment)
        remaining = payment.amount_cents
        for item in schedule:
            if remaining <= 0:
                break
            outstanding = item.total_cents - paid[item.installment_number]
            if outstanding <= 0:
                continue
            applied = min(remaining, outstanding)
            principal_outstanding = max(
                0, item.principal_cents - paid[item.installment_number]
            )
            principal_applied = min(applied, principal_outstanding)
            interest_applied = applied - principal_applied
            if is_in:
                principal_in += principal_applied
                interest_in += interest_applied
            elif is_after:
                principal_after += principal_applied
            paid[item.installment_number] += applied
            remaining -= applied
    return principal_in, interest_in, principal_after


# ===========================================================================
# Payoff
# ===========================================================================


@dataclass(frozen=True)
class PayoffQuote:
    """A payoff quote (DB-free value object). All amounts in integer cents."""

    as_of: date
    principal_cents: int
    accrued_interest_cents: int
    payoff_cents: int


def compute_payoff(db: Session, loan: PlatformLoan, as_of: date) -> PayoffQuote:
    """Compute the amount required to fully pay off ``loan`` as of ``as_of``.

    payoff = remaining principal + accrued interest.

    * REMAINING PRINCIPAL is ``loan.principal_balance_cents`` (the spine reduces
      it by the principal portion of each payment).
    * ACCRUED INTEREST is the unpaid interest on every installment whose
      ``due_date <= as_of`` — interest that has already accrued (the borrower
      has reached that period) but has not yet been covered by payments. For
      each such installment, the unpaid interest is the installment's interest
      minus the interest portion already paid into it; partial payments fill
      principal before interest (mirrors ``record_payment``), so a partially
      paid installment may still owe some or all of its interest.

    Future installments (due after ``as_of``) contribute no accrued interest —
    the borrower pays off early and is not charged unearned future interest.

    Pure read: no writes, no commit.
    """
    schedule = sorted(loan.schedule, key=lambda s: s.installment_number)

    accrued_interest = 0
    for item in schedule:
        if item.due_date > as_of:
            continue
        if item.status == "waived":
            continue
        # Of what's been paid into this installment, principal is satisfied
        # first; the remainder (if any) covers interest.
        interest_paid = max(0, item.paid_cents - item.principal_cents)
        unpaid_interest = max(0, item.interest_cents - interest_paid)
        accrued_interest += unpaid_interest

    principal = max(0, loan.principal_balance_cents)
    return PayoffQuote(
        as_of=as_of,
        principal_cents=principal,
        accrued_interest_cents=accrued_interest,
        payoff_cents=principal + accrued_interest,
    )
