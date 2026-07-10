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
    PlatformLoanTransaction,
)
from app.schemas.pricing_config import parse_pricing_config, quote_fees_cents
from app.services import loan_ledger
from app.services.interest_engine import REPAYMENT_MODES, allocate_payment
from app.services.loan_quote import (
    CRIMINAL_RATE_CAP_BPS,
    compute_apr_bps,
    exceeds_criminal_rate,
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
      * RATE  — first the decision's ``apr_bps``; else the product's typed
        pricing config (``interest.annual_rate_bps``; the tolerant loader maps
        legacy ``apr_bps``/``apr_range``-floor onto it); else
        ``_DEFAULT_ANNUAL_RATE_BPS``.
      * TERM  — first the decision's ``term_months``; else the product's typed
        pricing config (``default_term_months``, which the loader maps from
        legacy ``term_months``; else first of ``term_options``; else
        ``term_min_months``); else ``_DEFAULT_TERM_MONTHS``.
    """
    decision = application.decision or {}

    # Principal
    principal_cents = decision.get("amount_cents")
    if principal_cents is None:
        principal_cents = application.requested_amount_cents
    if not principal_cents or principal_cents <= 0:
        raise ValueError("Cannot book a loan with non-positive principal")

    product = getattr(application, "credit_product", None)
    raw_pricing = (getattr(product, "pricing_config", None) or {}) if product else {}
    cfg = parse_pricing_config(raw_pricing, context="loan booking")

    # Rate (bps)
    annual_rate_bps = decision.get("apr_bps")
    if annual_rate_bps is None:
        if cfg.interest is not None:
            annual_rate_bps = cfg.interest.annual_rate_bps
        else:
            annual_rate_bps = _DEFAULT_ANNUAL_RATE_BPS

    # Term (months)
    term_months = decision.get("term_months")
    if term_months is None:
        if cfg.default_term_months is not None:
            term_months = cfg.default_term_months
        elif cfg.term_options:
            term_months = cfg.term_options[0]
        elif cfg.term_min_months is not None:
            term_months = cfg.term_min_months
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

    currency = "CAD"
    product = getattr(application, "credit_product", None)
    if product is not None and getattr(product, "currency", None):
        currency = product.currency

    # Compliance guardrail (fail-closed): refuse to book a loan whose APR reaches
    # the Criminal Code s.347 cap. The calculator already blocks this up front; this
    # is defense-in-depth at the single booking chokepoint so no path can create a
    # criminal-rate loan. APR is the Canadian regulatory figure incl. product fees.
    fees_cents = 0
    if product is not None:
        cfg = parse_pricing_config(
            getattr(product, "pricing_config", None), context="loan booking APR guard"
        )
        # Selection-aware cost-of-borrowing fees for the booked terms (typed fee
        # schedule; contingent on_event fees excluded per SOR/2001-104).
        fees_cents = quote_fees_cents(cfg, principal_cents, term_months, "monthly")
    apr_bps = compute_apr_bps(principal_cents, annual_rate_bps, term_months, "monthly", fees_cents)
    if exceeds_criminal_rate(apr_bps):
        raise ValueError(
            f"Refusing to book loan from application {application.id}: APR "
            f"{apr_bps / 100:.2f}% reaches the Criminal Code s.347 cap "
            f"({CRIMINAL_RATE_CAP_BPS / 100:.0f}%)."
        )

    if first_due_date is None:
        first_due_date = _add_months(date.today(), 1)

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
    *,
    created_by: str = "system",
    comment: Optional[str] = None,
    repayment_mode: str = "regular",
) -> PlatformLoanPayment:
    """Apply a received payment to a loan. MONEY-PATH (WS-A actuals engine +
    WS-F repayment modes).

    ``repayment_mode`` selects the Turnkey allocation semantics
    (03__WP_Servicing, Dave's spec — validated by the pure allocators in
    ``interest_engine``):

      * ``regular`` — accrued interest → principal → fees. The default; the
        only mode that also advances the as-agreed PLAN (installment filling).
      * ``add_on``  — pays the non-accruing add-on bucket ONLY (e.g. NSF
        fees); amount must not exceed the current add-on balance. The plan is
        untouched.
      * ``special`` — 100% principal, bypassing interest/fees (staff-only;
        permission-gated at the endpoint). Amount must not exceed outstanding
        principal. The plan is untouched — Dave's borrower-protection rule:
        extra payments never alter the as-agreed installments.
      * ``payoff``  — the fixed, server-computed closing amount (principal +
        accrued interest + fees + add-on as of the effective date). The
        amount MUST equal ``compute_payoff`` for the effective date exactly
        (non-editable — server computes, client confirms); the loan closes.

    TWO parallel books are updated, with distinct jobs:

    1. THE SCHEDULE (the as-agreed PLAN) — for ``regular`` payments only: the
       payment fills the oldest unpaid/partial installment first (by
       installment_number), flipping installment statuses (``paid`` /
       ``partial``), curing delinquency, and flipping the loan to ``paid_off``
       when every installment is covered. This drives DPD/status only.
       ``suspended`` installments (schedule surgery, WS-F) still absorb cash —
       suspension stops the automated jobs, not the debt.

    2. THE LEDGER (the MONEY TRUTH) — an immutable
       ``platform_loan_transactions`` row is appended for every applied
       payment, allocated by the actuals-based daily-simple-interest engine in
       the selected repayment mode. ``loan.principal_balance_cents`` is
       decremented by the ledger's PRINCIPAL allocation (a late payment
       therefore repays slightly less principal — the extra per-diem days ate
       more interest — and an early payment slightly more).

    CLOSURE (Dave): any payment — whatever the mode — that brings total debt
    (principal + interest due + fees due + add-on) to zero closes the loan:
    status → ``paid_off`` and every remaining open installment is ``waived``
    (no longer owed under the plan; the debt was settled in full).

    Side effects:
      * Inserts a PlatformLoanPayment row (the cash receipt / audit trail).
      * Appends the immutable ledger row (dual dates: effective = processing =
        the received date; backdating is a later, permission-gated workstream).
      * Updates installment ``paid_cents``/status per the plan (see 1).
      * Decrements ``loan.principal_balance_cents`` by the actuals-allocated
        principal (see 2).
      * Flips the loan to ``paid_off`` once every installment is fully paid,
        or once the ledger's total debt hits zero (see CLOSURE).

    Raises ``ValueError`` on a mode-rule violation (unknown mode, add-on /
    special amount caps, payoff exact-match) — callers map it to a 4xx.

    Overpayment beyond everything owed is recorded on the payment/ledger rows
    but not auto-refunded (flagged for Dave; refund policy is a later
    workstream).
    """
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")
    if repayment_mode not in REPAYMENT_MODES:
        raise ValueError(
            f"unknown repayment mode {repayment_mode!r} "
            f"(expected one of {REPAYMENT_MODES})"
        )

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

    # ---- LEDGER (money truth): actuals-based allocation --------------------
    # Balances as of the payment's EFFECTIVE date (per-diem interest accrued
    # from the previous ledger event on actual calendar days), then the
    # selected repayment-mode allocation (regular waterfall / add-on-only /
    # principal-only / full-bucket payoff). The allocator VALIDATES the mode's
    # amount rules (raises ValueError) BEFORE any state is touched — nothing is
    # added to the session until the allocation is known-good.
    effective_date = received_at.date()
    balances_before = loan_ledger.loan_balances(loan, as_of=effective_date)
    allocation = allocate_payment(repayment_mode, amount_cents, balances_before)

    payment = PlatformLoanPayment(
        loan_id=loan.id,
        amount_cents=amount_cents,
        received_at=received_at,
        method=method,
        external_ref=external_ref,
    )
    db.add(payment)

    vendor_id = None
    if getattr(loan, "application_id", None) is not None:
        app_row = (
            db.query(PlatformCreditApplication)
            .filter(PlatformCreditApplication.id == loan.application_id)
            .first()
        )
        vendor_id = getattr(app_row, "vendor_id", None)

    seq = loan_ledger.next_seq(loan)
    txn = PlatformLoanTransaction(
        loan_id=loan.id,
        seq=seq,
        reference=loan_ledger.build_reference(vendor_id, loan.id, seq),
        txn_type="payment",
        payment_type=loan_ledger.payment_type_for_method(method),
        repayment_mode=repayment_mode,
        amount_cents=amount_cents,
        principal_cents=allocation.principal_cents,
        interest_cents=allocation.interest_cents,
        fees_cents=allocation.fees_cents,
        add_on_cents=allocation.add_on_cents,
        effective_date=effective_date,
        processing_date=effective_date,
        created_by=created_by,
        comment=comment,
    )
    loan.transactions.append(txn)  # keeps the in-session ledger view consistent
    db.add(txn)

    # Outstanding principal moves by the LEDGER allocation (decrement, clamped
    # at zero). Decrement — rather than overwrite with the ledger-derived
    # figure — so a loan whose stored balance was established outside the
    # ledger (e.g. a migrated book before its cutover reconciliation row)
    # can never have its balance silently "healed" upward by a payment; for
    # ledger-consistent loans the two are identical (migration 049 reconciles
    # every pre-existing loan at cutover).
    loan.principal_balance_cents = max(
        0, loan.principal_balance_cents - allocation.principal_cents
    )

    # ---- SCHEDULE (as-agreed plan): installment status flipping ------------
    # REGULAR mode only: fill oldest unpaid installment first. This no longer
    # moves money — it only tracks plan progress for DPD / statuses. The other
    # modes deliberately leave the plan untouched (Dave's borrower-protection
    # rule: extra payments never alter the as-agreed installments; add-on fees
    # were never part of the plan; payoff closes the whole loan below).
    schedule = sorted(
        loan.schedule,
        key=lambda s: s.installment_number,
    )

    if repayment_mode == "regular":
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

            item.paid_cents += applied
            remaining -= applied

            if item.paid_cents >= item.total_cents:
                item.status = "paid"
            else:
                item.status = "partial"

    # ---- CLOSURE: the ledger's total debt at zero closes the loan ----------
    # Dave: ANY payment that brings total debt (principal + interest due +
    # fees due + add-on) to zero closes the loan — payoff mode by construction
    # (it pays exactly all four buckets), but a regular payment can land there
    # too. The appended txn is already in loan.transactions, so this reads the
    # POST-payment actuals state.
    balances_after = loan_ledger.loan_balances(loan, as_of=effective_date)
    debt_cleared = balances_after.payoff_cents == 0

    # Loan-level status transitions.
    if debt_cleared:
        # Settled in full: remaining open installments are no longer owed
        # under the plan — waive them (keeps DPD/aging/dunning quiet without
        # pretending they were paid per-installment) and close the loan.
        for item in schedule:
            if item.status not in ("paid", "waived"):
                item.status = "waived"
        loan.status = "paid_off"
    elif all(s.status in ("paid", "waived") for s in schedule) and schedule:
        # The plan is complete → paid_off (existing behaviour). NOTE: the
        # actuals ledger may carry a small residue (per-diem interest vs the
        # schedule's period interest); closure/true-up is the Payoff repayment
        # mode. Flagged for Dave.
        loan.status = "paid_off"
    elif loan.status == "delinquent":
        # Delinquency cure: a delinquent loan returns to ``active`` once it has no
        # overdue, unpaid installment left — the exact inverse of the condition
        # run_delinquency_aging uses to mark it delinquent. Without this, a borrower
        # who catches up stays flagged delinquent forever (and is then excluded from
        # future aging passes, which only re-evaluate ``active`` loans).
        # ``suspended`` installments (schedule surgery, WS-F) are excluded, mirroring
        # the aging job: staff deliberately parked them, so they don't hold the loan
        # in delinquency.
        today = datetime.now(timezone.utc).date()
        has_overdue_unpaid = any(
            s.due_date < today and s.status not in ("paid", "waived", "suspended")
            for s in schedule
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
                    "repayment_mode": repayment_mode,
                    "loan_status": loan.status,
                    "principal_balance_cents": loan.principal_balance_cents,
                    # WS-A ledger row (actuals allocation): ids + cents only.
                    "ledger_reference": txn.reference,
                    "allocation": {
                        "interest_cents": allocation.interest_cents,
                        "principal_cents": allocation.principal_cents,
                        "fees_cents": allocation.fees_cents,
                        "add_on_cents": allocation.add_on_cents,
                        "overpayment_cents": allocation.overpayment_cents,
                    },
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


# Installment statuses the aging job may flip to ``late``. ``suspended`` is
# DELIBERATELY excluded (schedule surgery, WS-F): a staff-suspended installment
# must be skipped by the delinquency/dunning automation — that is the entire
# point of suspending it. Pinned by tests/test_schedule_surgery.py.
_AGEABLE_ITEM_STATUSES = ("scheduled", "partial")


def run_delinquency_aging(db: Session, as_of: date) -> AgingResult:
    """Flip overdue, not-fully-paid installments to ``late`` and their loans to
    ``delinquent``.

    A schedule item is overdue when ``due_date < as_of`` and its status is in
    ``_AGEABLE_ITEM_STATUSES`` (scheduled/partial) — any unpaid balance past
    its due date is delinquent. Already-``late`` items stay late (idempotent).
    ``suspended`` items (schedule surgery, WS-F) are NEVER flipped — staff
    parked them on purpose, and a suspended item likewise never drags its loan
    into ``delinquent``.

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
            PlatformLoanScheduleItem.status.in_(_AGEABLE_ITEM_STATUSES),
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
    fees_due_cents: int = 0
    add_on_balance_cents: int = 0


def compute_payoff(db: Session, loan: PlatformLoan, as_of: date) -> PayoffQuote:
    """Compute the amount required to fully pay off ``loan`` as of ``as_of``.

    ACTUALS ENGINE (WS-A — Turnkey parity, Dave's spec):

        payoff = 100% outstanding principal
               + daily simple interest accrued to ``as_of`` (per-diem on the
                 ACTUAL payment history in the ledger — NOT the schedule)
               + fees due
               + add-on balance
        …and 0% future interest, by construction (interest is accrued only up
        to ``as_of``).

    The header-math invariant (04__WP_Collections) holds exactly:
    principal + interest due + fees due + add-on balance == payoff.

    Pure read: no writes, no commit.
    """
    balances = loan_ledger.loan_balances(loan, as_of=as_of)
    return PayoffQuote(
        as_of=as_of,
        principal_cents=balances.outstanding_principal_cents,
        accrued_interest_cents=balances.interest_due_cents,
        payoff_cents=balances.payoff_cents,
        fees_due_cents=balances.fees_due_cents,
        add_on_balance_cents=balances.add_on_balance_cents,
    )
