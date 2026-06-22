"""Turnkey Lender -> PaySpyre payment-history importer (mapping + ledger-only persist).

This is the ETL that loads HISTORICAL loan payments out of a Turnkey payment-ledger
export so the AI training dataset has real performance signal (on-time vs. late, paid
amounts, cadence). It mirrors ``turnkey.py`` (pure Excel->dataclass mapping, all
column-name + unit ASSUMPTIONS centralized in one place) and ``turnkey_persist.py``
(idempotent DB writes keyed on a stable external reference).

==============================================================================
CRITICAL CORRECTNESS RULE — HISTORICAL PAYMENTS ARE LEDGER ROWS ONLY
==============================================================================
Do NOT route these through ``loan_servicing.record_payment``. ``record_payment``
applies cash to the amortization schedule AND reduces ``principal_balance_cents`` —
but migrated loans already have their CURRENT outstanding balance set by the loan
import (``turnkey_persist.persist_loans``). Re-applying the historical payments on top
of that snapshot would DOUBLE-COUNT and corrupt every migrated balance.

So this importer inserts raw ``PlatformLoanPayment`` rows ONLY. It never touches the
schedule, the balance, or any loan field. The payments exist purely as an analytics /
training-data ledger of what the borrower actually paid in the legacy system.
==============================================================================

KNOWN MIGRATION ASSUMPTIONS (flagged here, not solved): the real Turnkey payment-ledger
export format isn't available in this repo, so every column name and amount unit is
declared in the ``Col`` / ``AMOUNT_IS_DOLLARS`` block at the top. When the real file
lands, correct THOSE constants — the rest of the module needs no changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

from app.models.platform.loan import PlatformLoan, PlatformLoanPayment


# =============================================================================
# TURNKEY PAYMENT-LEDGER ASSUMPTIONS — correct these against the real export.
# =============================================================================
# The export is assumed to be a sheet of rows where each row is a single posted
# payment, addressed by the legacy Turnkey account number. Columns are referenced
# by NAME (the parser accepts dict-like rows keyed by header) so that a header
# rename is a one-line fix here. ``parse_payments`` also accepts plain mapping rows.
class Col:
    ACCT = "Acct#"            # legacy Turnkey account number (-> PlatformLoan.legacy_account_number)
    DATE = "Payment Date"     # posting / value date of the payment
    AMOUNT = "Amount"         # payment amount (see AMOUNT_IS_DOLLARS below)
    METHOD = "Method"         # payment method/channel, e.g. "PAD", "EFT", "Card"
    TXN_ID = "Transaction Id"  # stable Turnkey payment/transaction id (-> external_ref)


# Turnkey exports money as DOLLARS (e.g. 123.45). PaySpyre stores integer cents, so we
# multiply by 100 on the way in. If a future export is already in cents, flip this.
AMOUNT_IS_DOLLARS = True

# The PaySpyre payment ``method`` recorded for an imported historical payment. If the
# source row carries its own method we keep it (prefixed) so the provenance is obvious;
# otherwise we fall back to this constant.
DEFAULT_METHOD = "turnkey_migration"

# external_ref is namespaced so a historical Turnkey id can never collide with a live
# rail reference (e.g. a Zumrails transaction id) on the same loan.
EXTERNAL_REF_PREFIX = "turnkey:"

# Accepted date string formats (in addition to native date / datetime cells).
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S")
# =============================================================================


@dataclass(frozen=True)
class MappedPayment:
    """A single historical payment, normalized for the PaySpyre ledger.

    Frozen because it is a pure value derived from one export row; nothing downstream
    mutates it. ``received_at`` is a tz-aware UTC datetime ready for the
    ``platform_loan_payments.received_at`` column.
    """

    legacy_account_number: str
    amount_cents: int
    received_at: datetime
    method: str
    external_ref: str


def _to_cents(v: Any) -> Optional[int]:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return None
    if isinstance(v, str):
        s = v.strip().replace("$", "").replace(",", "")
        if not s:
            return None
        try:
            v = float(s)
        except ValueError:
            return None
    return round(v * 100) if AMOUNT_IS_DOLLARS else int(round(v))


def _to_dt(v: Any) -> Optional[datetime]:
    """Parse a cell into a tz-aware UTC datetime, or None if unparseable."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=timezone.utc)
    if isinstance(v, str):
        s = v.strip()
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _get(row: Any, key: str) -> Any:
    """Read a column from a mapping-style row (dict / openpyxl-with-headers)."""
    if hasattr(row, "get"):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return None


@dataclass
class PaymentMapResult:
    """Outcome of mapping an export: clean records + per-row warnings (never crashes)."""

    payments: list[MappedPayment] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)   # human-readable warnings
    total_rows: int = 0


def map_payment(row: Any) -> tuple[Optional[MappedPayment], Optional[str]]:
    """Map one export row. Returns (MappedPayment, None) on success, or
    (None, warning) if the row is invalid — we collect warnings rather than raise so a
    single bad row never aborts a whole import."""
    acct = _get(row, Col.ACCT)
    acct_s = str(acct).strip() if acct not in (None, "") else ""
    if not acct_s:
        return None, "row missing account number — skipped"

    cents = _to_cents(_get(row, Col.AMOUNT))
    if cents is None:
        return None, f"acct {acct_s}: unparseable amount — skipped"
    if cents <= 0:
        return None, f"acct {acct_s}: non-positive amount ({cents}c) — skipped"

    received_at = _to_dt(_get(row, Col.DATE))
    if received_at is None:
        return None, f"acct {acct_s}: missing/unparseable payment date — skipped"

    txn = _get(row, Col.TXN_ID)
    txn_s = str(txn).strip() if txn not in (None, "") else ""
    if not txn_s:
        # Without a stable id we cannot dedupe on re-import; refuse rather than risk
        # inserting duplicate ledger rows on a retry.
        return None, f"acct {acct_s}: missing Turnkey transaction id — skipped"

    raw_method = _get(row, Col.METHOD)
    method = (
        f"{DEFAULT_METHOD}:{str(raw_method).strip()}"
        if raw_method not in (None, "")
        else DEFAULT_METHOD
    )

    return (
        MappedPayment(
            legacy_account_number=acct_s,
            amount_cents=cents,
            received_at=received_at,
            method=method,
            external_ref=f"{EXTERNAL_REF_PREFIX}{txn_s}",
        ),
        None,
    )


def map_payments(rows: Iterable[Any]) -> PaymentMapResult:
    """Map an iterable of export rows into a PaymentMapResult (pure; no DB)."""
    result = PaymentMapResult()
    for row in rows:
        if row is None:
            continue
        result.total_rows += 1
        mp, warning = map_payment(row)
        if mp is not None:
            result.payments.append(mp)
        elif warning is not None:
            result.invalid.append(warning)
    return result


@dataclass
class PaymentPersistResult:
    imported: int = 0
    skipped_duplicate: int = 0
    unmatched_account: int = 0
    invalid: int = 0
    unmatched_accts: list[str] = field(default_factory=list)


def persist_payments(
    db: Session,
    mapped: list[MappedPayment],
    *,
    invalid_count: int = 0,
    commit: bool = True,
) -> PaymentPersistResult:
    """Insert historical payments as PlatformLoanPayment LEDGER ROWS ONLY.

    LEDGER-ONLY (see the module docstring): this NEVER calls record_payment, never
    touches the schedule, and never changes ``principal_balance_cents``. It only INSERTs
    payment rows for analytics / training.

    Idempotent: each payment is keyed on its stable ``external_ref`` (the namespaced
    Turnkey transaction id). The DB carries a UNIQUE index on
    ``(loan_id, external_ref) WHERE external_ref IS NOT NULL`` (migration 033); we also
    pre-SELECT existing (loan_id, external_ref) pairs so a re-import skips dupes cleanly
    instead of relying on IntegrityError. A payment whose account number does not match a
    migrated loan is reported as ``unmatched_account`` (not inserted, not crashed).
    """
    result = PaymentPersistResult(invalid=invalid_count)

    # acct -> loan_id for every migrated loan (resolve once).
    loan_by_acct = {
        acct: loan_id
        for (acct, loan_id) in db.query(
            PlatformLoan.legacy_account_number, PlatformLoan.id
        )
        .filter(PlatformLoan.legacy_account_number.isnot(None))
        .all()
    }

    # Existing (loan_id, external_ref) pairs so we can pre-check for dupes.
    seen: set[tuple[Any, str]] = {
        (loan_id, ref)
        for (loan_id, ref) in db.query(
            PlatformLoanPayment.loan_id, PlatformLoanPayment.external_ref
        )
        .filter(PlatformLoanPayment.external_ref.isnot(None))
        .all()
    }

    for mp in mapped:
        loan_id = loan_by_acct.get(mp.legacy_account_number)
        if loan_id is None:
            result.unmatched_account += 1
            result.unmatched_accts.append(mp.legacy_account_number)
            continue
        key = (loan_id, mp.external_ref)
        if key in seen:
            result.skipped_duplicate += 1
            continue
        db.add(
            PlatformLoanPayment(
                loan_id=loan_id,
                amount_cents=mp.amount_cents,
                received_at=mp.received_at,
                method=mp.method,
                external_ref=mp.external_ref,
            )
        )
        seen.add(key)
        result.imported += 1

    if commit:
        db.commit()
    return result
