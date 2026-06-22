"""Turnkey Lender -> PaySpyre loan-book importer (pure mapping layer).

Transforms rows from a Turnkey "Accounts" export into PaySpyre loan records. PURE
and DB-free: it parses, status-maps, validates, and (for active loans) builds a
snapshot forward schedule, returning normalized dataclasses ready to persist. The
DB-write step is deliberately separate (it needs a target environment + the
application_id decision below) — this layer is unit-tested against synthetic rows and
can be dry-run against the real export to preview a migration.

KNOWN MIGRATION DECISION (flagged, not solved here): ``PlatformLoan.application_id`` is
NOT NULL + unique, but a Turnkey loan has no PaySpyre application. Persisting these
requires either a nullable/`source='turnkey'` column or stub applications — resolve
before the write step.

Reconciliation (see the actual/360 work) showed Turnkey accrues interest actual/360, so
the forward schedules are generated with ``day_count='actual/360'`` to stay consistent
with the legacy book.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Optional

from app.services.loan_servicing import ScheduleRow, generate_amortization_schedule


# --- Turnkey "Accounts" sheet column indices (0-based; header row 3, data row 4+) ---
class Col:
    VENDOR = 1
    PROVIDER = 2
    ACCT = 3
    STATUS = 4
    SUBSTATUS = 5
    DAYS_PAST_DUE = 6
    AMOUNT_FINANCED = 15
    TERM = 16
    RATE = 17  # decimal fraction, e.g. 0.0599
    REGULAR_PAYMENT = 18
    COST_OF_BORROWING = 20
    FIRST_PMT = 21
    FINAL_PMT = 22
    ORIGINATION = 30
    INTEREST_PAID = 38
    PRINCIPAL_PAID = 39
    PRINCIPAL_BALANCE = 42
    NEXT_DUE = 44


# Turnkey (Status, Sub-Status) -> (PaySpyre loan status, import?). Anything not here is
# surfaced as a warning and NOT imported until mapped.
STATUS_MAP: dict[tuple[str, str], tuple[str, bool]] = {
    ("OPEN", "ACTIVE"): ("active", True),
    ("CLOSED", "PAID"): ("paid_off", True),
    ("CLOSED", "RENEWED"): ("paid_off", True),     # settled via a new (separate) loan
    ("CLOSED", "RBO"): ("paid_off", True),         # re-bought-out: settled
    ("CLOSED", "WRITE OFF"): ("charged_off", True),
    ("CLOSED", "TRANSFER"): ("cancelled", True),
    ("VOIDED", "VOIDED"): ("cancelled", False),    # voided: do not import
}


@dataclass
class TurnkeyLoan:
    acct: str
    vendor: Optional[str]
    provider: Optional[str]
    tk_status: str
    tk_substatus: str
    days_past_due: int
    amount_financed_cents: Optional[int]
    term_months: Optional[int]
    annual_rate_bps: Optional[int]
    regular_payment_cents: Optional[int]
    cost_of_borrowing_cents: Optional[int]
    origination_date: Optional[date]
    first_pmt_date: Optional[date]
    final_pmt_date: Optional[date]
    next_due_date: Optional[date]
    interest_paid_cents: Optional[int]
    principal_paid_cents: Optional[int]
    principal_balance_cents: Optional[int]


@dataclass
class MappedLoan:
    acct: str
    status: str                         # PaySpyre loan status
    principal_cents: int                # ORIGINAL amount financed (historical)
    annual_rate_bps: int
    term_months: int
    principal_balance_cents: int        # CURRENT outstanding (0 for closed loans)
    disbursed_at: Optional[date]
    currency: str = "CAD"
    forward_schedule: list[ScheduleRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _cents(v: Any) -> Optional[int]:
    return round(v * 100) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _bps(v: Any) -> Optional[int]:
    return round(v * 10_000) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _as_date(v: Any) -> Optional[date]:
    if isinstance(v, datetime):
        return v.date()
    return v if isinstance(v, date) else None


def _int(v: Any, default: int = 0) -> int:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def parse_account_row(row: tuple) -> Optional[TurnkeyLoan]:
    """Parse one Accounts data row. Returns None for header/blank/total rows."""
    if not row or len(row) <= Col.PRINCIPAL_BALANCE:
        return None
    acct = row[Col.ACCT]
    if acct in (None, "", "Acct#") or not row[Col.STATUS]:
        return None
    return TurnkeyLoan(
        acct=str(acct),
        vendor=row[Col.VENDOR],
        provider=row[Col.PROVIDER],
        tk_status=str(row[Col.STATUS]).strip().upper(),
        tk_substatus=str(row[Col.SUBSTATUS] or "").strip().upper(),
        days_past_due=_int(row[Col.DAYS_PAST_DUE]),
        amount_financed_cents=_cents(row[Col.AMOUNT_FINANCED]),
        term_months=_int(row[Col.TERM]) or None,
        annual_rate_bps=_bps(row[Col.RATE]),
        regular_payment_cents=_cents(row[Col.REGULAR_PAYMENT]),
        cost_of_borrowing_cents=_cents(row[Col.COST_OF_BORROWING]),
        origination_date=_as_date(row[Col.ORIGINATION]),
        first_pmt_date=_as_date(row[Col.FIRST_PMT]),
        final_pmt_date=_as_date(row[Col.FINAL_PMT]),
        next_due_date=_as_date(row[Col.NEXT_DUE]),
        interest_paid_cents=_cents(row[Col.INTEREST_PAID]),
        principal_paid_cents=_cents(row[Col.PRINCIPAL_PAID]),
        principal_balance_cents=_cents(row[Col.PRINCIPAL_BALANCE]),
    )


def parse_accounts(rows: Iterable[tuple]) -> list[TurnkeyLoan]:
    return [tk for r in rows if (tk := parse_account_row(r)) is not None]


def _months_inclusive(start: date, end: date) -> int:
    """Whole monthly due-dates from start..end inclusive (>=0)."""
    n = (end.year - start.year) * 12 + (end.month - start.month) + 1
    return max(0, n)


def _forward_schedule(tk: TurnkeyLoan, warnings: list[str]) -> list[ScheduleRow]:
    """Snapshot-and-service-forward: re-amortize the CURRENT balance over the remaining
    term from the next due date, on Turnkey's actual/360 convention. The borrower keeps
    paying; nothing about the past changes."""
    bal = tk.principal_balance_cents
    if not bal or bal <= 0 or not tk.next_due_date or not tk.final_pmt_date or not tk.annual_rate_bps:
        warnings.append("active loan: insufficient data for a forward schedule (skipped)")
        return []
    remaining = _months_inclusive(tk.next_due_date, tk.final_pmt_date)
    if remaining <= 0:
        warnings.append("active loan: non-positive remaining term (skipped forward schedule)")
        return []
    return generate_amortization_schedule(
        bal, tk.annual_rate_bps, remaining, tk.next_due_date, day_count="actual/360"
    )


def map_loan(tk: TurnkeyLoan, *, snapshot: bool = True) -> Optional[MappedLoan]:
    """Map a TurnkeyLoan to a PaySpyre MappedLoan, or None if it should not be imported."""
    warnings: list[str] = []
    mapped = STATUS_MAP.get((tk.tk_status, tk.tk_substatus))
    if mapped is None:
        warnings.append(
            f"unmapped Turnkey status ({tk.tk_status}/{tk.tk_substatus}) — NOT imported"
        )
        return None  # surfaced by build_report via map_all
    status, do_import = mapped
    if not do_import:
        return None

    if not tk.amount_financed_cents or tk.amount_financed_cents <= 0:
        warnings.append("non-positive amount financed")
    if not tk.term_months or tk.term_months <= 0:
        warnings.append("missing/invalid term")
    if tk.annual_rate_bps is None or tk.annual_rate_bps < 0:
        warnings.append("missing/invalid rate")

    balance = tk.principal_balance_cents or 0
    if status == "active":
        if balance <= 0:
            warnings.append("active loan with zero/negative outstanding balance")
        elif tk.amount_financed_cents and balance > tk.amount_financed_cents:
            warnings.append("outstanding balance exceeds original principal")
    else:
        balance = 0  # closed loans carry no outstanding balance

    m = MappedLoan(
        acct=tk.acct,
        status=status,
        principal_cents=tk.amount_financed_cents or 0,
        annual_rate_bps=tk.annual_rate_bps or 0,
        term_months=tk.term_months or 0,
        principal_balance_cents=balance,
        disbursed_at=tk.origination_date,
        warnings=warnings,
    )
    if status == "active" and snapshot:
        m.forward_schedule = _forward_schedule(tk, m.warnings)
    return m


@dataclass
class ImportReport:
    total_rows: int
    importable: int
    skipped_unmapped: list[str]                 # acct numbers
    by_paspyre_status: dict[str, int]
    by_turnkey_status: dict[str, int]
    loans_with_warnings: list[tuple[str, list[str]]]
    total_principal_cents: int
    total_outstanding_cents: int


def build_report(rows: Iterable[tuple], *, snapshot: bool = True) -> tuple[list[MappedLoan], ImportReport]:
    import collections

    tks = parse_accounts(rows)
    mapped: list[MappedLoan] = []
    skipped: list[str] = []
    by_tk: dict[str, int] = collections.Counter()
    for tk in tks:
        by_tk[f"{tk.tk_status}/{tk.tk_substatus}"] += 1
        m = map_loan(tk, snapshot=snapshot)
        if m is None:
            if (tk.tk_status, tk.tk_substatus) not in STATUS_MAP:
                skipped.append(tk.acct)
        else:
            mapped.append(m)
    by_ps: dict[str, int] = collections.Counter(m.status for m in mapped)
    report = ImportReport(
        total_rows=len(tks),
        importable=len(mapped),
        skipped_unmapped=skipped,
        by_paspyre_status=dict(by_ps),
        by_turnkey_status=dict(by_tk),
        loans_with_warnings=[(m.acct, m.warnings) for m in mapped if m.warnings],
        total_principal_cents=sum(m.principal_cents for m in mapped),
        total_outstanding_cents=sum(m.principal_balance_cents for m in mapped),
    )
    return mapped, report
