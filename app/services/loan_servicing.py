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
from datetime import date, datetime
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanPayment,
    PlatformLoanScheduleItem,
)


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


def generate_amortization_schedule(
    principal_cents: int,
    annual_rate_bps: int,
    term_months: int,
    first_due_date: date,
) -> list[ScheduleRow]:
    """Compute a standard fully-amortizing (equal-payment) schedule.

    PURE function — no DB, no side effects, deterministic.

    Method:
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

    Returns rows ordered by installment_number (1-based). Due dates step monthly
    from ``first_due_date``.
    """
    if principal_cents <= 0:
        raise ValueError("principal_cents must be positive")
    if term_months <= 0:
        raise ValueError("term_months must be positive")
    if annual_rate_bps < 0:
        raise ValueError("annual_rate_bps must be non-negative")

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

    # Loan-level status: fully paid off?
    if all(s.status in ("paid", "waived") for s in schedule) and schedule:
        loan.status = "paid_off"
        loan.principal_balance_cents = 0

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
