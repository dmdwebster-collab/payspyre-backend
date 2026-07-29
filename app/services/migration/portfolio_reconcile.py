"""Reconcile an imported portfolio against the balances the source STATES.

The account sheet asserts, per loan, both cumulative actuals (payments / fees /
interest / principal paid) and closing balances. The transaction sheet is the
event history those assertions summarize. If the two disagree, the source export
is internally inconsistent — and the ONLY correct response is to say so.

THE RULE: a mismatch is REPORTED, never silently adjusted. Nothing in this module
writes; it compares and returns findings. Quietly "fixing" a balance would
destroy the one thing a migration must preserve — an auditable claim about what
the borrower actually owes.

Pure and DB-free. ``reconcile_persisted`` compares what actually landed in
PaySpyre; ``reconcile_source`` compares the file against itself before anything
is written, so an operator can see the exceptions during a dry run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.services.migration.portfolio_workbook import (
    PortfolioAccount,
    PortfolioTransaction,
    iter_by_account,
)

#: Money is rounded to the cent on the way in, so a 1-cent gap is a rounding
#: artefact of the source's own float arithmetic, not a discrepancy.
DEFAULT_TOLERANCE_CENTS = 1


@dataclass
class Discrepancy:
    account_number: str
    measure: str          # e.g. "principal_paid", "principal_balance"
    stated_cents: Optional[int]
    derived_cents: Optional[int]

    @property
    def delta_cents(self) -> Optional[int]:
        if self.stated_cents is None or self.derived_cents is None:
            return None
        return self.derived_cents - self.stated_cents

    def as_dict(self) -> dict:
        return {
            "account_number": self.account_number,
            "measure": self.measure,
            "stated_cents": self.stated_cents,
            "derived_cents": self.derived_cents,
            "delta_cents": self.delta_cents,
        }


@dataclass
class ReconciliationReport:
    tolerance_cents: int = DEFAULT_TOLERANCE_CENTS
    accounts_checked: int = 0
    accounts_reconciled: int = 0
    #: source pass: accounts the transaction sheet has no rows for.
    accounts_without_transactions: list[str] = field(default_factory=list)
    #: persisted pass: accounts with no loan in PaySpyre (skipped, or not imported).
    accounts_not_imported: list[str] = field(default_factory=list)
    orphan_transaction_accounts: list[str] = field(default_factory=list)
    discrepancies: list[Discrepancy] = field(default_factory=list)

    @property
    def accounts_with_discrepancies(self) -> list[str]:
        seen: list[str] = []
        for d in self.discrepancies:
            if d.account_number not in seen:
                seen.append(d.account_number)
        return seen

    @property
    def ok(self) -> bool:
        return not self.discrepancies and not self.orphan_transaction_accounts

    def as_dict(self, *, max_discrepancies: int = 200) -> dict:
        return {
            "tolerance_cents": self.tolerance_cents,
            "accounts_checked": self.accounts_checked,
            "accounts_reconciled": self.accounts_reconciled,
            "accounts_with_discrepancies": len(self.accounts_with_discrepancies),
            "accounts_without_transactions": self.accounts_without_transactions[:200],
            "accounts_not_imported": self.accounts_not_imported[:200],
            "orphan_transaction_accounts": self.orphan_transaction_accounts[:200],
            "discrepancy_count": len(self.discrepancies),
            "discrepancies": [d.as_dict() for d in self.discrepancies[:max_discrepancies]],
            "ok": self.ok,
        }


def _sum(values: Iterable[Optional[int]]) -> int:
    return sum(v for v in values if v is not None)


def _compare(
    report: ReconciliationReport,
    account_number: str,
    measure: str,
    stated: Optional[int],
    derived: Optional[int],
) -> bool:
    """Record a discrepancy when the two disagree beyond tolerance. Returns True
    when they agree (or when the source stated nothing to compare against)."""
    if stated is None:
        return True
    d = derived or 0
    if abs(d - stated) <= report.tolerance_cents:
        return True
    report.discrepancies.append(Discrepancy(account_number, measure, stated, d))
    return False


def reconcile_source(
    accounts: list[PortfolioAccount],
    transactions: list[PortfolioTransaction],
    *,
    tolerance_cents: int = DEFAULT_TOLERANCE_CENTS,
) -> ReconciliationReport:
    """Tie each account row's stated actuals + balances to its own transactions.

    Two independent checks per account:

    1. CUMULATIVE ACTUALS — the sum of the transaction allocations must equal the
       account's stated ``Payment Amount`` / fees / interest / principal paid.
    2. CLOSING BALANCES — the LAST transaction's running balances must equal the
       account's stated fee / interest / principal balance and total owed.
    """
    report = ReconciliationReport(tolerance_cents=tolerance_cents)
    by_account = iter_by_account(transactions)
    known = {a.account_number for a in accounts}

    for acct_no in by_account:
        if acct_no not in known:
            report.orphan_transaction_accounts.append(acct_no)

    for account in accounts:
        report.accounts_checked += 1
        rows = by_account.get(account.account_number)
        if not rows:
            report.accounts_without_transactions.append(account.account_number)
            continue

        clean = True
        clean &= _compare(report, account.account_number, "payment_total",
                          account.payment_amount_total_cents,
                          _sum(r.payment_cents for r in rows))
        clean &= _compare(report, account.account_number, "fees_paid",
                          account.fees_paid_cents,
                          _sum(r.fees_paid_cents for r in rows))
        clean &= _compare(report, account.account_number, "interest_paid",
                          account.interest_paid_cents,
                          _sum(r.interest_paid_cents for r in rows))
        clean &= _compare(report, account.account_number, "principal_paid",
                          account.principal_paid_cents,
                          _sum(r.principal_paid_cents for r in rows))

        last = rows[-1]
        clean &= _compare(report, account.account_number, "fees_balance",
                          account.fees_balance_cents, last.fees_balance_cents)
        clean &= _compare(report, account.account_number, "interest_balance",
                          account.interest_balance_cents, last.interest_balance_cents)
        clean &= _compare(report, account.account_number, "principal_balance",
                          account.principal_balance_cents, last.principal_balance_cents)
        clean &= _compare(report, account.account_number, "total_owed",
                          account.total_owed_cents, last.total_owed_cents)

        if clean:
            report.accounts_reconciled += 1

    return report


@dataclass
class PersistedLoanTotals:
    """What PaySpyre now holds for one imported loan (fed by the DB query)."""

    account_number: str
    principal_balance_cents: int
    fees_paid_cents: int = 0
    interest_paid_cents: int = 0
    principal_paid_cents: int = 0
    payment_total_cents: int = 0
    ledger_rows: int = 0


def reconcile_persisted(
    accounts: list[PortfolioAccount],
    persisted: dict[str, PersistedLoanTotals],
    *,
    tolerance_cents: int = DEFAULT_TOLERANCE_CENTS,
) -> ReconciliationReport:
    """Tie what LANDED in PaySpyre back to the account sheet's stated figures.

    Run after an apply. An account that does not tie is listed with its measure
    and delta; nothing is corrected.
    """
    report = ReconciliationReport(tolerance_cents=tolerance_cents)
    for account in accounts:
        report.accounts_checked += 1
        totals = persisted.get(account.account_number)
        if totals is None:
            # No loan in PaySpyre for this account — it was skipped by status,
            # or never imported. Listed, not counted as reconciled.
            report.accounts_not_imported.append(account.account_number)
            continue
        clean = True
        clean &= _compare(report, account.account_number, "principal_balance",
                          account.principal_balance_cents, totals.principal_balance_cents)
        clean &= _compare(report, account.account_number, "principal_paid",
                          account.principal_paid_cents, totals.principal_paid_cents)
        clean &= _compare(report, account.account_number, "interest_paid",
                          account.interest_paid_cents, totals.interest_paid_cents)
        clean &= _compare(report, account.account_number, "fees_paid",
                          account.fees_paid_cents, totals.fees_paid_cents)
        clean &= _compare(report, account.account_number, "payment_total",
                          account.payment_amount_total_cents, totals.payment_total_cents)
        if clean:
            report.accounts_reconciled += 1
    return report
