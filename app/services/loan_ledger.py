"""Loan ledger service — maps ``platform_loan_transactions`` rows into the pure
actuals engine (``interest_engine``) and back. WS-A.

Split of responsibilities (flow_engine idiom):

  * ``interest_engine``   — pure math (no models, no DB).
  * THIS MODULE           — row ↔ event mapping, reference/seq generation, and
    the composed views (balances, running-balance ledger view). Reads only the
    loan's in-memory relationships (``loan.transactions``) so it stays cheap
    and DB-free-testable; callers own commits.
  * ``loan_servicing``    — writes (record_payment appends ledger rows).

The ledger is IMMUTABLE (DB WORM trigger, migration 044): corrections are
``reversal`` rows referencing the original via ``reverses_transaction_id``.
"""
from __future__ import annotations

from datetime import date
from typing import Optional, Sequence

from app.models.platform.loan import PlatformLoan, PlatformLoanTransaction
from app.services.interest_engine import (
    DEFAULT_CONFIG,
    BalanceView,
    InterestEngineConfig,
    LedgerEvent,
    compute_balances,
)

# Payment-method → payment_type enum mapping (reconciliation dimension).
# Unknown / bank-rail methods land on 'eft' (Zumrails PAD is an EFT rail).
_METHOD_TO_PAYMENT_TYPE = (
    ("cash", "cash"),
    ("cheque", "check"),
    ("check", "check"),
    ("card", "credit_card"),
    ("adjust", "adjustment"),
)


def payment_type_for_method(method: Optional[str]) -> str:
    m = (method or "").lower()
    for needle, mapped in _METHOD_TO_PAYMENT_TYPE:
        if needle in m:
            return mapped
    return "eft"


def sorted_transactions(loan: PlatformLoan) -> list[PlatformLoanTransaction]:
    """The loan's ledger rows in replay order: effective date, then seq."""
    return sorted(
        list(loan.transactions or []),
        key=lambda t: (t.effective_date, t.seq),
    )


def next_seq(loan: PlatformLoan) -> int:
    """Next per-loan sequence number (1-based)."""
    rows = list(loan.transactions or [])
    return (max((t.seq for t in rows), default=0)) + 1


def build_reference(vendor_id, loan_id, seq: int) -> str:
    """Dave's mandate: reference = ``{vendor_id}-{loan_id}-{seq}``, each
    component independently filterable. ``none`` when the loan has no vendor
    (e.g. migrated book)."""
    return f"{vendor_id or 'none'}-{loan_id}-{seq}"


def _event_for_row(
    row: PlatformLoanTransaction,
    by_id: dict,
) -> Optional[LedgerEvent]:
    """Map one ledger row to a pure LedgerEvent (or None for no-ops).

    * payment / adjustment — allocations REDUCE the buckets (cash in or a
      vendor-accommodation reduction of amount owed).
    * fee — allocations CHARGE the fee buckets (fees_cents → loan fees due,
      add_on_cents → non-accruing add-on balance). Fee rows never carry
      principal/interest.
    * disbursement — informational: the principal advance is anchored on
      ``loan.principal_cents`` + ``loan.disbursed_at`` (never double-counted).
    * reversal — the exact opposite of the referenced row's event, applied at
      the REVERSAL's own effective date.
    """
    if row.txn_type in ("payment", "adjustment"):
        return LedgerEvent(
            effective_date=row.effective_date,
            principal_paid_cents=row.principal_cents or 0,
            interest_paid_cents=row.interest_cents or 0,
            fees_paid_cents=row.fees_cents or 0,
            add_on_paid_cents=row.add_on_cents or 0,
        )
    if row.txn_type == "fee":
        return LedgerEvent(
            effective_date=row.effective_date,
            fees_charged_cents=row.fees_cents or 0,
            add_on_charged_cents=row.add_on_cents or 0,
        )
    if row.txn_type == "disbursement":
        return None
    if row.txn_type == "reversal":
        original = by_id.get(row.reverses_transaction_id)
        if original is None or original.txn_type == "reversal":
            return None  # dangling / chained reversals: no-op (defensive)
        forward = _event_for_row(original, by_id)
        if forward is None:
            return None
        return LedgerEvent(
            effective_date=row.effective_date,
            # Flip paid ↔ charged so the reversal exactly compensates.
            principal_charged_cents=forward.principal_paid_cents,
            interest_charged_cents=forward.interest_paid_cents,
            fees_charged_cents=forward.fees_paid_cents,
            add_on_charged_cents=forward.add_on_paid_cents,
            fees_paid_cents=forward.fees_charged_cents,
            add_on_paid_cents=forward.add_on_charged_cents,
        )
    return None


def events_for_rows(
    rows: Sequence[PlatformLoanTransaction],
) -> list[tuple[PlatformLoanTransaction, Optional[LedgerEvent]]]:
    by_id = {r.id: r for r in rows}
    return [(r, _event_for_row(r, by_id)) for r in rows]


def _accrual_start(loan: PlatformLoan) -> Optional[date]:
    """Interest accrues from the DISBURSEMENT date (money out the door).

    A loan that never disbursed accrues nothing. Flagged for Dave: confirm
    against the "account due as of" Excel (contract date vs disbursement date).
    """
    disbursed_at = getattr(loan, "disbursed_at", None)
    if disbursed_at is None:
        return None
    return disbursed_at.date() if hasattr(disbursed_at, "date") else disbursed_at


def loan_balances(
    loan: PlatformLoan,
    as_of: date,
    config: InterestEngineConfig = DEFAULT_CONFIG,
) -> BalanceView:
    """The actuals balance view for a loan at ``as_of``.

    outstanding principal + interest due + fees due + add-on == payoff.
    """
    rows = sorted_transactions(loan)
    events = [ev for _, ev in events_for_rows(rows) if ev is not None]
    return compute_balances(
        principal_cents=loan.principal_cents,
        annual_rate_bps=loan.annual_rate_bps,
        accrual_start=_accrual_start(loan),
        events=events,
        as_of=as_of,
        config=config,
    )


def ledger_view(
    loan: PlatformLoan,
    as_of: date,
    config: InterestEngineConfig = DEFAULT_CONFIG,
) -> dict:
    """Ledger rows + running category balances after each row + the header
    balance view at ``as_of`` (Dave: running balances pinned above the ledger).

    Running balances are computed by replaying the actuals engine up to (and
    including) each row at that row's effective date — so ``interest_due`` in a
    running row reflects interest accrued to THAT date, not to ``as_of``.
    """
    rows = sorted_transactions(loan)
    mapped = events_for_rows(rows)

    out_rows: list[dict] = []
    prefix: list[LedgerEvent] = []
    for row, ev in mapped:
        if ev is not None:
            prefix.append(ev)
        running = compute_balances(
            principal_cents=loan.principal_cents,
            annual_rate_bps=loan.annual_rate_bps,
            accrual_start=_accrual_start(loan),
            events=prefix,
            as_of=row.effective_date,
            config=config,
        )
        out_rows.append(
            {
                "id": str(row.id),
                "seq": row.seq,
                "reference": row.reference,
                "txn_type": row.txn_type,
                "payment_type": row.payment_type,
                "repayment_mode": row.repayment_mode,
                "amount_cents": row.amount_cents,
                "principal_cents": row.principal_cents,
                "interest_cents": row.interest_cents,
                "fees_cents": row.fees_cents,
                "add_on_cents": row.add_on_cents,
                "effective_date": row.effective_date.isoformat(),
                "processing_date": row.processing_date.isoformat(),
                "reverses_transaction_id": (
                    str(row.reverses_transaction_id)
                    if row.reverses_transaction_id
                    else None
                ),
                "created_by": row.created_by,
                "comment": row.comment,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "running_balances": {
                    "outstanding_principal_cents": running.outstanding_principal_cents,
                    "interest_due_cents": running.interest_due_cents,
                    "fees_due_cents": running.fees_due_cents,
                    "add_on_balance_cents": running.add_on_balance_cents,
                },
            }
        )

    balances = compute_balances(
        principal_cents=loan.principal_cents,
        annual_rate_bps=loan.annual_rate_bps,
        accrual_start=_accrual_start(loan),
        events=[ev for _, ev in mapped if ev is not None],
        as_of=as_of,
        config=config,
    )
    return {
        "loan_id": str(loan.id),
        "as_of": as_of.isoformat(),
        "transactions": out_rows,
        "balances": {
            "outstanding_principal_cents": balances.outstanding_principal_cents,
            "interest_due_cents": balances.interest_due_cents,
            "fees_due_cents": balances.fees_due_cents,
            "add_on_balance_cents": balances.add_on_balance_cents,
            # Header invariant (04__WP_Collections): the four buckets sum to payoff.
            "payoff_cents": balances.payoff_cents,
        },
    }
