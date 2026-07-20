"""Turnkey -> PaySpyre cutover CSV import (WS-D): parse, validate, apply.

Implements the template-driven, preview-then-confirm import Dave specced from
Turnkey's Tools -> Import workplace (docs/turnkey_parity/09__WP_Tools.md) for
migrating the live book (~580 customers / ~560 loans) INTO PaySpyre.

Layering (mirrors the existing ``turnkey.py`` / ``turnkey_persist.py`` split):

* PARSE + VALIDATE — pure, DB-free. ``validate_csv`` takes the CSV text plus an
  ``ImportContext`` of lookup sets (the DB shell builds it) and returns a
  ``ValidationResult``: normalized row payloads + a PII-safe report. NOTHING is
  written to domain tables at validation time.

* APPLY — ``apply_rows`` writes the normalized rows idempotently, per-row
  savepoints so one bad row never aborts the batch:
    - customers: find-or-create keyed on legacy customer id, then email.
    - loans:     the migration-035 turnkey path (source='turnkey_migration',
                 legacy_account_number unique key, application_id NULL,
                 agreement signed / disbursement completed) — the same shape
                 ``turnkey_persist.persist_loans`` writes — plus the new
                 ``patient_id`` borrower link (migration 047).
    - payments:  LEDGER-ONLY PlatformLoanPayment inserts, deduped on a stable
                 external_ref. NEVER routed through record_payment — migrated
                 balances are already current snapshots; re-applying history
                 would double-count (see turnkey_payments.py's correctness
                 rule).
    - disbursements: mark the funding leg of migrated loans completed-historical
                 (disbursed_at / disbursement_ref backfill; no money moves).

FILE FORMAT: CSV only (stdlib ``csv``). .xlsx support is pending dependency
approval (openpyxl is NOT a project dependency and must not be assumed).

MONEY: dollars in the file, integer cents in the DB. Parsing is strict — an
ambiguous amount (>2 decimals, malformed thousands groups, parentheses,
exponents) is REJECTED with a row error, never guessed.

PII: validation/apply errors reference row NUMBERS and field NAMES only —
never the values of PII fields (names, emails, DOB, addresses, phones).
Legacy account numbers are internal loan identifiers (already logged by the
existing migration tooling) and may appear in reports.

SIN is deliberately NOT an importable column: full SINs live only encrypted on
``platform_patients.sin_encrypted`` and must never transit a CSV round-trip.
If the legacy book's SINs need migrating, that is a separate, encrypted-path
exercise (flagged for Dave).
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanPayment,
    PlatformLoanScheduleItem,
)
from app.models.platform.patient import PlatformPatient
from app.models.platform.patient_field import PlatformPatientField
from app.services.loan_quote import CRIMINAL_RATE_CAP_BPS
from app.services.loan_servicing import generate_amortization_schedule
from app.services.migration.turnkey import STATUS_MAP


ENTITY_TYPES = ("customers", "loans", "payments", "disbursements")

# platform_patient_fields keys written by this importer (source-tagged).
LEGACY_CUSTOMER_FIELD_KEY = "turnkey_legacy_customer_id"
IMPORT_ADDRESS_FIELD_KEY = "turnkey_import_address"
IMPORT_FIELD_SOURCE = "turnkey_import"

# external_ref namespaces. "turnkey:" matches turnkey_payments.EXTERNAL_REF_PREFIX
# (a file-supplied stable transaction id); "import:" marks a ref DERIVED from
# (acct, date, amount) when the file carries no transaction id.
REF_PREFIX_SUPPLIED = "turnkey:"
REF_PREFIX_DERIVED = "import:"

# Loan statuses accepted by the loans template (PaySpyre canonical). Turnkey
# "STATUS/SUBSTATUS" pairs (e.g. "OPEN/ACTIVE") are also accepted and mapped via
# turnkey.STATUS_MAP. 'pending_disbursement' is deliberately NOT importable — a
# migrated loan was already funded in the legacy system.
IMPORTABLE_LOAN_STATUSES = ("active", "delinquent", "paid_off", "charged_off", "cancelled")

_MAX_TERM_MONTHS = 480  # sanity ceiling (40 years)


# ---------------------------------------------------------------------------
# Column specs (the documented templates)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    required: bool
    description: str
    example: str
    pii: bool = False  # PII columns: values NEVER echoed into errors/reports


ENTITY_COLUMNS: dict[str, list[ColumnSpec]] = {
    "customers": [
        ColumnSpec("legacy_customer_id", True, "Turnkey customer ID (unique per customer)", "581"),
        ColumnSpec("first_name", True, "Legal first name", "Royce", pii=True),
        ColumnSpec("last_name", True, "Legal last name", "Heidenreich", pii=True),
        ColumnSpec("email", True, "Email (find-or-create key when the legacy id is new)", "royce@example.com", pii=True),
        ColumnSpec("phone", False, "Main phone", "+12505551234", pii=True),
        ColumnSpec("date_of_birth", False, "Date of birth, YYYY-MM-DD", "1975-03-14", pii=True),
        ColumnSpec("street", False, "Street address", "2033 Gordon Dr.", pii=True),
        ColumnSpec("apartment", False, "Apartment / unit", "4B", pii=True),
        ColumnSpec("city", False, "City", "Kelowna", pii=True),
        ColumnSpec("province", False, "Province (2-letter or full name)", "BC", pii=True),
        ColumnSpec("postal_code", False, "Postal code", "V1Y 3J2", pii=True),
        # NOTE: no SIN column, by design — see the module docstring.
    ],
    "loans": [
        ColumnSpec("legacy_account_number", True, "Turnkey loan account number (unique per loan)", "BC4906-0001"),
        ColumnSpec("customer_legacy_id", True, "Turnkey customer ID this loan belongs to (import customers first)", "581"),
        ColumnSpec("principal", True, "Original amount financed, dollars", "2500.00"),
        ColumnSpec("annual_rate_percent", True, "Annual interest rate, percent (max 2 decimals)", "5.99"),
        ColumnSpec("start_date", True, "Origination / disbursement date, YYYY-MM-DD", "2024-01-10"),
        ColumnSpec("term_months", True, "Term in months", "24"),
        ColumnSpec("payment_frequency", False, "Payment frequency (only 'monthly' supported in v1; default monthly)", "monthly"),
        ColumnSpec("status", True, "active | delinquent | paid_off | charged_off | cancelled, or a Turnkey STATUS/SUBSTATUS pair like OPEN/ACTIVE", "active"),
        ColumnSpec("principal_balance", False, "CURRENT outstanding principal, dollars (required for active/delinquent; closed loans carry 0)", "1250.00"),
        ColumnSpec("next_due_date", False, "Next scheduled due date, YYYY-MM-DD (with final_payment_date, enables the forward schedule for active loans)", "2026-08-01"),
        ColumnSpec("final_payment_date", False, "Final scheduled payment date, YYYY-MM-DD", "2027-01-01"),
        ColumnSpec("vendor", False, "Vendor name/ID (recorded for audit; vendor linking is a follow-up)", "BC4906"),
    ],
    "payments": [
        ColumnSpec("legacy_account_number", True, "Turnkey loan account number the payment was posted to", "BC4906-0001"),
        ColumnSpec("payment_date", True, "Posting date, YYYY-MM-DD", "2025-06-01"),
        ColumnSpec("amount", True, "Payment amount, dollars", "123.45"),
        ColumnSpec("type", False, "Payment method/channel, e.g. PAD, EFT, Card", "PAD"),
        ColumnSpec("reference", False, "Stable Turnkey transaction id (STRONGLY recommended — required to import two identical payments on the same day)", "TXN-88123"),
    ],
    "disbursements": [
        ColumnSpec("legacy_account_number", True, "Turnkey loan account number that was funded", "BC4906-0001"),
        ColumnSpec("disbursement_date", True, "Funding date, YYYY-MM-DD", "2024-01-10"),
        ColumnSpec("amount", True, "Disbursed amount, dollars (warned if it differs from the loan principal)", "2500.00"),
        ColumnSpec("reference", False, "Stable Turnkey disbursement/transaction id", "DSB-4411"),
    ],
}

_PII_FIELDS: dict[str, frozenset[str]] = {
    entity: frozenset(c.name for c in cols if c.pii) for entity, cols in ENTITY_COLUMNS.items()
}


def template_for(entity_type: str) -> dict:
    """The documented template for an entity: columns, CSV header, example row."""
    cols = ENTITY_COLUMNS[entity_type]
    return {
        "entity_type": entity_type,
        "file_format": "csv",
        "note": ".xlsx support pending dependency approval — upload CSV (UTF-8).",
        "columns": [
            {
                "name": c.name,
                "required": c.required,
                "description": c.description,
                "example": c.example,
            }
            for c in cols
        ],
        "header_csv": ",".join(c.name for c in cols),
        "example_row_csv": ",".join(c.example for c in cols),
    }


# ---------------------------------------------------------------------------
# Strict scalar parsers (pure)
# ---------------------------------------------------------------------------

# Dollars: optional $, either plain digits or correctly-grouped thousands, at
# most 2 decimals. Anything else (parentheses, sign, exponent, "1.234",
# "1,23") is ambiguous -> rejected.
_MONEY_RE = re.compile(r"^\$?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d{1,2})?$")
_RATE_RE = re.compile(r"^(\d{1,3})(\.\d{1,2})?%?$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def parse_money_cents(raw: str) -> int:
    """Parse a dollar string to integer cents. Raises ValueError on anything
    ambiguous — never guesses."""
    s = (raw or "").strip()
    m = _MONEY_RE.match(s)
    if not m:
        raise ValueError(
            "invalid amount — expected dollars like 1234.56 or 1,234.56 "
            "(max 2 decimals, no sign/parentheses)"
        )
    whole = m.group(1).replace(",", "")
    frac = (m.group(2) or ".0")[1:]
    return int(whole) * 100 + int(frac.ljust(2, "0"))


def parse_rate_bps(raw: str) -> int:
    """Parse an annual percentage rate ('5.99') to basis points (599).
    Max 2 decimals (= whole bps); more precision is rejected as ambiguous."""
    s = (raw or "").strip()
    m = _RATE_RE.match(s)
    if not m:
        raise ValueError("invalid rate — expected percent like 5.99 (max 2 decimals)")
    bps = int(Decimal(m.group(1) + (m.group(2) or "")) * 100)
    if bps > 100_00:  # >100% annual: certainly a unit mistake
        raise ValueError("rate exceeds 100% — check units (percent expected)")
    return bps


def parse_iso_date(raw: str) -> date:
    """Strict YYYY-MM-DD. Slash formats (03/04/2025) are ambiguous -> rejected."""
    s = (raw or "").strip()
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ValueError("invalid date — expected YYYY-MM-DD")


def parse_int(raw: str) -> int:
    s = (raw or "").strip()
    if not re.match(r"^\d+$", s):
        raise ValueError("invalid integer")
    return int(s)


def derive_payment_external_ref(
    legacy_account_number: str,
    payment_date: date,
    amount_cents: int,
    reference: Optional[str] = None,
) -> str:
    """The idempotency key for an imported payment.

    * File supplies a stable transaction id -> ``turnkey:<id>`` (same namespace
      as turnkey_payments, so the two import paths dedupe against each other).
    * No id -> a DERIVED key from (account, date, amount):
      ``import:<acct>:<YYYY-MM-DD>:<cents>``. Two identical (acct, date,
      amount) rows without a reference are therefore ambiguous and rejected at
      validation — supply the reference column to import both.
    """
    if reference:
        return f"{REF_PREFIX_SUPPLIED}{reference.strip()}"
    return f"{REF_PREFIX_DERIVED}{legacy_account_number}:{payment_date.isoformat()}:{amount_cents}"


def derive_disbursement_ref(
    legacy_account_number: str, disbursement_date: date, reference: Optional[str] = None
) -> str:
    if reference:
        return f"{REF_PREFIX_SUPPLIED}{reference.strip()}"
    return f"{REF_PREFIX_DERIVED}disb:{legacy_account_number}:{disbursement_date.isoformat()}"


# ---------------------------------------------------------------------------
# Validation (pure)
# ---------------------------------------------------------------------------


@dataclass
class ImportContext:
    """Lookup sets the validator checks references against (built by the DB
    shell; empty sets make the validator fully pure for tests)."""

    existing_customer_legacy_ids: set[str] = field(default_factory=set)
    existing_customer_emails: set[str] = field(default_factory=set)  # lowercased
    existing_loan_accounts: set[str] = field(default_factory=set)
    existing_payment_refs: set[tuple[str, str]] = field(default_factory=set)  # (acct, external_ref)


@dataclass
class RowIssue:
    row: int            # 1-based DATA row number (header = row 0)
    field: str
    message: str

    def as_dict(self) -> dict:
        return {"row": self.row, "field": self.field, "message": self.message}


@dataclass
class ValidationResult:
    entity_type: str
    row_count: int = 0
    valid_rows: list[dict] = field(default_factory=list)   # normalized payloads (may hold PII)
    errors: list[RowIssue] = field(default_factory=list)
    warnings: list[RowIssue] = field(default_factory=list)
    file_errors: list[str] = field(default_factory=list)
    totals: dict = field(default_factory=dict)

    @property
    def error_rows(self) -> set[int]:
        return {e.row for e in self.errors}

    def report(self) -> dict:
        """The PII-safe preview report persisted on the batch + returned by the
        API. References row numbers + field names, never PII values."""
        return {
            "entity_type": self.entity_type,
            "row_count": self.row_count,
            "valid_count": len(self.valid_rows),
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "file_errors": self.file_errors,
            "errors": [e.as_dict() for e in self.errors],
            "warnings": [w.as_dict() for w in self.warnings],
            "totals": self.totals,
        }


def _read_csv(csv_text: str, entity_type: str, result: ValidationResult) -> list[dict]:
    """Parse CSV text into row dicts; header problems become file_errors."""
    specs = ENTITY_COLUMNS[entity_type]
    try:
        reader = csv.DictReader(io.StringIO(csv_text))
        header = reader.fieldnames
        if not header:
            result.file_errors.append("empty file — no header row")
            return []
        header_set = {h.strip().lower() for h in header if h}
        missing = [c.name for c in specs if c.required and c.name not in header_set]
        if missing:
            result.file_errors.append(
                f"missing required column(s): {', '.join(missing)} — "
                f"download the template for the expected header"
            )
            return []
        known = {c.name for c in specs}
        extra = sorted(h for h in header_set if h not in known)
        if extra:
            result.warnings.append(
                RowIssue(0, ",".join(extra), "unknown column(s) — ignored")
            )
        rows = []
        for raw in reader:
            # normalize keys (case/space-insensitive header match), drop unknowns
            rows.append(
                {
                    (k or "").strip().lower(): (v or "").strip()
                    for k, v in raw.items()
                    if (k or "").strip().lower() in known
                }
            )
        return rows
    except csv.Error as exc:
        result.file_errors.append(f"unreadable CSV: {exc}")
        return []


def _require(
    row: dict, row_no: int, name: str, result: ValidationResult
) -> Optional[str]:
    v = row.get(name, "")
    if not v:
        result.errors.append(RowIssue(row_no, name, "required value is missing"))
        return None
    return v


def _parse_field(
    parser: Callable, raw: str, row_no: int, name: str, entity_type: str, result: ValidationResult
):
    """Run a scalar parser; on failure record a row error. The offending VALUE is
    echoed only for non-PII fields."""
    try:
        return parser(raw)
    except ValueError as exc:
        pii = name in _PII_FIELDS[entity_type]
        detail = str(exc) if pii else f"{exc} (got {raw!r})"
        result.errors.append(RowIssue(row_no, name, detail))
        return None


def _validate_customers(rows: list[dict], ctx: ImportContext, result: ValidationResult) -> None:
    seen_ids: set[str] = set()
    seen_emails: set[str] = set()
    for i, row in enumerate(rows, start=1):
        had_errors = len(result.errors)
        legacy_id = _require(row, i, "legacy_customer_id", result)
        first = _require(row, i, "first_name", result)
        last = _require(row, i, "last_name", result)
        email = _require(row, i, "email", result)

        if email:
            email = email.lower()
            if not _EMAIL_RE.match(email):
                result.errors.append(RowIssue(i, "email", "invalid email format"))
            elif email in seen_emails:
                result.errors.append(
                    RowIssue(i, "email", "duplicate email within file — customers must be unique")
                )
            else:
                seen_emails.add(email)
                if email in ctx.existing_customer_emails:
                    result.warnings.append(
                        RowIssue(i, "email", "matches an existing patient — legacy id will be attached, no new record created")
                    )
        if legacy_id:
            if legacy_id in seen_ids:
                result.errors.append(
                    RowIssue(i, "legacy_customer_id", "duplicate legacy id within file")
                )
            else:
                seen_ids.add(legacy_id)
                if legacy_id in ctx.existing_customer_legacy_ids:
                    result.warnings.append(
                        RowIssue(i, "legacy_customer_id", "already imported — row will be skipped (idempotent)")
                    )

        dob = None
        if row.get("date_of_birth"):
            dob = _parse_field(parse_iso_date, row["date_of_birth"], i, "date_of_birth", "customers", result)
            if dob is not None and not (date(1900, 1, 1) <= dob <= date.today()):
                result.errors.append(RowIssue(i, "date_of_birth", "out of plausible range"))
                dob = None

        if len(result.errors) > had_errors:
            continue
        address = {
            k: row[k]
            for k in ("street", "apartment", "city", "province", "postal_code")
            if row.get(k)
        }
        result.valid_rows.append(
            {
                "_row": i,
                "legacy_customer_id": legacy_id,
                "first_name": first,
                "last_name": last,
                "email": email,
                "phone": row.get("phone") or None,
                "date_of_birth": dob.isoformat() if dob else None,
                "address": address or None,
            }
        )


def _parse_loan_status(raw: str) -> str:
    s = raw.strip()
    if s.lower() in IMPORTABLE_LOAN_STATUSES:
        return s.lower()
    if "/" in s:  # Turnkey "STATUS/SUBSTATUS" pair, e.g. OPEN/ACTIVE
        tk_status, _, tk_sub = (p.strip().upper() for p in s.partition("/"))
        mapped = STATUS_MAP.get((tk_status, tk_sub))
        if mapped and mapped[1]:
            return mapped[0]
        raise ValueError("unmapped Turnkey status pair (not importable)")
    raise ValueError(
        f"unknown status — expected one of {', '.join(IMPORTABLE_LOAN_STATUSES)} "
        f"or a Turnkey STATUS/SUBSTATUS pair"
    )


def _validate_loans(rows: list[dict], ctx: ImportContext, result: ValidationResult) -> None:
    seen_accts: set[str] = set()
    total_principal = total_outstanding = 0
    for i, row in enumerate(rows, start=1):
        had_errors = len(result.errors)
        acct = _require(row, i, "legacy_account_number", result)
        customer_id = _require(row, i, "customer_legacy_id", result)
        principal_raw = _require(row, i, "principal", result)
        rate_raw = _require(row, i, "annual_rate_percent", result)
        start_raw = _require(row, i, "start_date", result)
        term_raw = _require(row, i, "term_months", result)
        status_raw = _require(row, i, "status", result)

        if acct:
            if acct in seen_accts:
                result.errors.append(RowIssue(i, "legacy_account_number", "duplicate account number within file"))
            else:
                seen_accts.add(acct)
                if acct in ctx.existing_loan_accounts:
                    result.warnings.append(
                        RowIssue(i, "legacy_account_number", "already imported — row will be skipped (idempotent)")
                    )
        if customer_id and customer_id not in ctx.existing_customer_legacy_ids:
            result.errors.append(
                RowIssue(i, "customer_legacy_id", "unknown customer legacy id — import the customers file first")
            )

        principal = _parse_field(parse_money_cents, principal_raw, i, "principal", "loans", result) if principal_raw else None
        if principal is not None and principal <= 0:
            result.errors.append(RowIssue(i, "principal", "must be positive"))
            principal = None
        rate = _parse_field(parse_rate_bps, rate_raw, i, "annual_rate_percent", "loans", result) if rate_raw else None
        if rate is not None and rate >= CRIMINAL_RATE_CAP_BPS:
            result.warnings.append(
                RowIssue(i, "annual_rate_percent", "rate at/above the criminal-rate cap — verify against the source book")
            )
        start = _parse_field(parse_iso_date, start_raw, i, "start_date", "loans", result) if start_raw else None
        term = _parse_field(parse_int, term_raw, i, "term_months", "loans", result) if term_raw else None
        if term is not None and not (1 <= term <= _MAX_TERM_MONTHS):
            result.errors.append(RowIssue(i, "term_months", f"must be 1..{_MAX_TERM_MONTHS}"))
            term = None
        status = _parse_field(_parse_loan_status, status_raw, i, "status", "loans", result) if status_raw else None

        frequency = (row.get("payment_frequency") or "monthly").lower()
        if frequency != "monthly":
            result.errors.append(
                RowIssue(i, "payment_frequency", f"unsupported frequency (only 'monthly' in v1; got {frequency!r})")
            )

        balance = None
        open_statuses = ("active", "delinquent")
        if status in open_statuses:
            if not row.get("principal_balance"):
                result.errors.append(
                    RowIssue(i, "principal_balance", "required for active/delinquent loans (current outstanding)")
                )
            else:
                balance = _parse_field(parse_money_cents, row["principal_balance"], i, "principal_balance", "loans", result)
                if balance is not None and principal is not None and balance > principal:
                    result.warnings.append(
                        RowIssue(i, "principal_balance", "outstanding exceeds original principal — verify")
                    )
        else:
            if row.get("principal_balance"):
                nz = _parse_field(parse_money_cents, row["principal_balance"], i, "principal_balance", "loans", result)
                if nz:
                    result.warnings.append(
                        RowIssue(i, "principal_balance", "closed loan — balance forced to 0")
                    )
            balance = 0

        next_due = final_pmt = None
        if row.get("next_due_date"):
            next_due = _parse_field(parse_iso_date, row["next_due_date"], i, "next_due_date", "loans", result)
        if row.get("final_payment_date"):
            final_pmt = _parse_field(parse_iso_date, row["final_payment_date"], i, "final_payment_date", "loans", result)
        if status in open_statuses and (next_due is None or final_pmt is None):
            result.warnings.append(
                RowIssue(i, "next_due_date", "active loan without next_due_date + final_payment_date — no forward schedule will be generated")
            )
        if next_due and final_pmt and final_pmt < next_due:
            result.errors.append(RowIssue(i, "final_payment_date", "before next_due_date"))

        if len(result.errors) > had_errors:
            continue
        total_principal += principal or 0
        total_outstanding += balance or 0
        result.valid_rows.append(
            {
                "_row": i,
                "legacy_account_number": acct,
                "customer_legacy_id": customer_id,
                "principal_cents": principal,
                "annual_rate_bps": rate,
                "start_date": start.isoformat(),
                "term_months": term,
                "status": status,
                "principal_balance_cents": balance,
                "next_due_date": next_due.isoformat() if next_due else None,
                "final_payment_date": final_pmt.isoformat() if final_pmt else None,
                "vendor": row.get("vendor") or None,
            }
        )
    result.totals = {
        "principal_cents": total_principal,
        "outstanding_cents": total_outstanding,
    }


def _validate_payments(rows: list[dict], ctx: ImportContext, result: ValidationResult) -> None:
    seen_refs: set[str] = set()
    total = 0
    for i, row in enumerate(rows, start=1):
        had_errors = len(result.errors)
        acct = _require(row, i, "legacy_account_number", result)
        date_raw = _require(row, i, "payment_date", result)
        amount_raw = _require(row, i, "amount", result)

        if acct and acct not in ctx.existing_loan_accounts:
            result.errors.append(
                RowIssue(i, "legacy_account_number", "unknown loan account — import the loans file first")
            )
        pay_date = _parse_field(parse_iso_date, date_raw, i, "payment_date", "payments", result) if date_raw else None
        amount = _parse_field(parse_money_cents, amount_raw, i, "amount", "payments", result) if amount_raw else None
        if amount is not None and amount <= 0:
            result.errors.append(RowIssue(i, "amount", "must be positive"))
            amount = None

        external_ref = None
        if acct and pay_date and amount is not None:
            external_ref = derive_payment_external_ref(acct, pay_date, amount, row.get("reference") or None)
            if external_ref in seen_refs:
                which = "reference" if row.get("reference") else "amount"
                result.errors.append(
                    RowIssue(
                        i,
                        which,
                        "duplicate payment key within file — two identical (account, date, amount) "
                        "payments need distinct 'reference' values to import both",
                    )
                )
            else:
                seen_refs.add(external_ref)
                if (acct, external_ref) in ctx.existing_payment_refs:
                    result.warnings.append(
                        RowIssue(i, "reference", "already imported — row will be skipped (idempotent)")
                    )

        if len(result.errors) > had_errors:
            continue
        total += amount
        result.valid_rows.append(
            {
                "_row": i,
                "legacy_account_number": acct,
                "payment_date": pay_date.isoformat(),
                "amount_cents": amount,
                "method": (row.get("type") or "").strip() or None,
                "external_ref": external_ref,
            }
        )
    result.totals = {"amount_cents": total}


def _validate_disbursements(rows: list[dict], ctx: ImportContext, result: ValidationResult) -> None:
    seen_accts: set[str] = set()
    total = 0
    for i, row in enumerate(rows, start=1):
        had_errors = len(result.errors)
        acct = _require(row, i, "legacy_account_number", result)
        date_raw = _require(row, i, "disbursement_date", result)
        amount_raw = _require(row, i, "amount", result)

        if acct:
            if acct in seen_accts:
                result.errors.append(
                    RowIssue(i, "legacy_account_number", "duplicate account within file — one disbursement per loan")
                )
            else:
                seen_accts.add(acct)
                if acct not in ctx.existing_loan_accounts:
                    result.errors.append(
                        RowIssue(i, "legacy_account_number", "unknown loan account — import the loans file first")
                    )
        disb_date = _parse_field(parse_iso_date, date_raw, i, "disbursement_date", "disbursements", result) if date_raw else None
        amount = _parse_field(parse_money_cents, amount_raw, i, "amount", "disbursements", result) if amount_raw else None
        if amount is not None and amount <= 0:
            result.errors.append(RowIssue(i, "amount", "must be positive"))
            amount = None

        if len(result.errors) > had_errors:
            continue
        total += amount
        result.valid_rows.append(
            {
                "_row": i,
                "legacy_account_number": acct,
                "disbursement_date": disb_date.isoformat(),
                "amount_cents": amount,
                "reference": (row.get("reference") or "").strip() or None,
            }
        )
    result.totals = {"amount_cents": total}


_VALIDATORS = {
    "customers": _validate_customers,
    "loans": _validate_loans,
    "payments": _validate_payments,
    "disbursements": _validate_disbursements,
}


def validate_csv(entity_type: str, csv_text: str, ctx: Optional[ImportContext] = None) -> ValidationResult:
    """Validate a CSV upload for ``entity_type``. PURE — writes nothing.

    Returns normalized row payloads for every clean row plus a PII-safe report
    of per-row errors/warnings. A row with any error is EXCLUDED from
    ``valid_rows`` (confirm never applies it).
    """
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"unknown entity_type {entity_type!r}")
    ctx = ctx or ImportContext()
    result = ValidationResult(entity_type=entity_type)
    rows = _read_csv(csv_text, entity_type, result)
    result.row_count = len(rows)
    if not result.file_errors:
        if not rows:
            result.file_errors.append("no data rows found")
        else:
            _VALIDATORS[entity_type](rows, ctx, result)
    return result


# ---------------------------------------------------------------------------
# DB shell: context builder + idempotent apply
# ---------------------------------------------------------------------------


def build_context(db: Session) -> ImportContext:
    """Collect the lookup sets validation checks references against."""
    legacy_ids = {
        str(v)
        for (v,) in db.query(PlatformPatientField.field_value)
        .filter(
            PlatformPatientField.field_key == LEGACY_CUSTOMER_FIELD_KEY,
            PlatformPatientField.is_current.is_(True),
        )
        .all()
    }
    emails = {
        e.lower()
        for (e,) in db.query(PlatformPatient.email)
        .filter(PlatformPatient.email.isnot(None))
        .all()
    }
    loan_accts = {
        acct
        for (acct,) in db.query(PlatformLoan.legacy_account_number)
        .filter(PlatformLoan.legacy_account_number.isnot(None))
        .all()
    }
    payment_refs = {
        (acct, ref)
        for (acct, ref) in (
            db.query(PlatformLoan.legacy_account_number, PlatformLoanPayment.external_ref)
            .join(PlatformLoanPayment, PlatformLoanPayment.loan_id == PlatformLoan.id)
            .filter(
                PlatformLoan.legacy_account_number.isnot(None),
                PlatformLoanPayment.external_ref.isnot(None),
            )
            .all()
        )
    }
    return ImportContext(
        existing_customer_legacy_ids=legacy_ids,
        existing_customer_emails=emails,
        existing_loan_accounts=loan_accts,
        existing_payment_refs=payment_refs,
    )


@dataclass
class ApplyResult:
    """Outcome of the confirm step. ``failures`` reference row numbers + a
    non-PII reason only."""

    entity_type: str
    applied: int = 0
    skipped_existing: int = 0
    failed: int = 0
    failures: list[dict] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def report(self) -> dict:
        return {
            "entity_type": self.entity_type,
            "applied": self.applied,
            "skipped_existing": self.skipped_existing,
            "failed": self.failed,
            "failures": self.failures,
            "details": self.details,
        }


def _fail(res: ApplyResult, row_no: int, message: str) -> None:
    res.failed += 1
    res.failures.append({"row": row_no, "message": message})


def _legacy_customer_map(db: Session) -> dict[str, Any]:
    """legacy customer id -> patient_id, from current source-tagged fields."""
    return {
        str(v): pid
        for (v, pid) in db.query(
            PlatformPatientField.field_value, PlatformPatientField.patient_id
        )
        .filter(
            PlatformPatientField.field_key == LEGACY_CUSTOMER_FIELD_KEY,
            PlatformPatientField.is_current.is_(True),
        )
        .all()
    }


def _apply_customers(db: Session, rows: list[dict], res: ApplyResult) -> None:
    """Find-or-create patients keyed on legacy id, then email. Never overwrites
    existing values (the live record wins over the legacy export)."""
    legacy_map = _legacy_customer_map(db)
    created = linked = 0
    for row in rows:
        row_no = row.get("_row", 0)
        try:
            with db.begin_nested():
                legacy_id = row["legacy_customer_id"]
                if legacy_id in legacy_map:
                    res.skipped_existing += 1
                    continue
                email = row["email"]
                patient = (
                    db.query(PlatformPatient)
                    .filter(func.lower(PlatformPatient.email) == email)
                    .first()
                )
                if patient is None:
                    patient = PlatformPatient(
                        legal_first_name=row["first_name"],
                        legal_last_name=row["last_name"],
                        email=email,
                        phone_e164=row.get("phone"),
                        dob=date.fromisoformat(row["date_of_birth"]) if row.get("date_of_birth") else None,
                    )
                    db.add(patient)
                    db.flush()
                    created += 1
                else:
                    # Existing patient: attach the legacy id; fill gaps only.
                    if patient.phone_e164 is None and row.get("phone"):
                        patient.phone_e164 = row["phone"]
                    if patient.dob is None and row.get("date_of_birth"):
                        patient.dob = date.fromisoformat(row["date_of_birth"])
                    linked += 1
                db.add(
                    PlatformPatientField(
                        patient_id=patient.id,
                        field_key=LEGACY_CUSTOMER_FIELD_KEY,
                        field_value=legacy_id,
                        source=IMPORT_FIELD_SOURCE,
                    )
                )
                if row.get("address"):
                    db.add(
                        PlatformPatientField(
                            patient_id=patient.id,
                            field_key=IMPORT_ADDRESS_FIELD_KEY,
                            field_value=row["address"],
                            source=IMPORT_FIELD_SOURCE,
                        )
                    )
                legacy_map[legacy_id] = patient.id
                res.applied += 1
        except Exception as exc:  # per-row isolation: record, keep going
            _fail(res, row_no, f"{type(exc).__name__} while creating customer")
    res.details = {"created": created, "linked_existing": linked}


def _apply_loans(db: Session, rows: list[dict], res: ApplyResult) -> None:
    """The migration-035 path: source='turnkey_migration', legacy account as the
    idempotency key, no PaySpyre application, agreement signed / disbursement
    completed (already funded in the legacy system) — the same shape
    ``turnkey_persist.persist_loans`` writes — plus the patient link."""
    existing = {
        acct
        for (acct,) in db.query(PlatformLoan.legacy_account_number)
        .filter(PlatformLoan.legacy_account_number.isnot(None))
        .all()
    }
    legacy_map = _legacy_customer_map(db)
    schedules = 0
    for row in rows:
        row_no = row.get("_row", 0)
        try:
            with db.begin_nested():
                acct = row["legacy_account_number"]
                if acct in existing:
                    res.skipped_existing += 1
                    continue
                patient_id = legacy_map.get(row["customer_legacy_id"])
                if patient_id is None:
                    # Validated against a snapshot; re-check at apply time.
                    _fail(res, row_no, "unknown customer legacy id at apply time")
                    continue
                start = date.fromisoformat(row["start_date"])
                loan = PlatformLoan(
                    application_id=None,
                    patient_id=patient_id,
                    source="turnkey_migration",
                    legacy_account_number=acct,
                    principal_cents=row["principal_cents"],
                    annual_rate_bps=row["annual_rate_bps"],
                    term_months=row["term_months"],
                    status=row["status"],
                    principal_balance_cents=row["principal_balance_cents"],
                    disbursed_at=datetime(start.year, start.month, start.day, tzinfo=timezone.utc),
                    agreement_status="signed",
                    disbursement_status="completed",
                    currency="CAD",
                )
                # Forward schedule (snapshot-and-service-forward): re-amortize the
                # CURRENT balance over the remaining term on Turnkey's actual/360
                # convention — same approach as turnkey._forward_schedule.
                if (
                    row["status"] in ("active", "delinquent")
                    and row["principal_balance_cents"] > 0
                    and row.get("next_due_date")
                    and row.get("final_payment_date")
                ):
                    next_due = date.fromisoformat(row["next_due_date"])
                    final_pmt = date.fromisoformat(row["final_payment_date"])
                    remaining = (
                        (final_pmt.year - next_due.year) * 12
                        + (final_pmt.month - next_due.month)
                        + 1
                    )
                    if remaining > 0:
                        for r in generate_amortization_schedule(
                            row["principal_balance_cents"],
                            row["annual_rate_bps"],
                            remaining,
                            next_due,
                            day_count="actual/360",
                        ):
                            loan.schedule.append(
                                PlatformLoanScheduleItem(
                                    installment_number=r.installment_number,
                                    due_date=r.due_date,
                                    principal_cents=r.principal_cents,
                                    interest_cents=r.interest_cents,
                                    total_cents=r.total_cents,
                                    status="scheduled",
                                    paid_cents=0,
                                )
                            )
                        schedules += 1
                db.add(loan)
                existing.add(acct)
                res.applied += 1
        except Exception as exc:
            _fail(res, row_no, f"{type(exc).__name__} while creating loan {row.get('legacy_account_number')!r}")
    res.details = {"forward_schedules": schedules}


def _apply_payments(db: Session, rows: list[dict], res: ApplyResult) -> None:
    """LEDGER-ONLY inserts (turnkey_payments' correctness rule): never routed
    through record_payment — migrated balances are already current snapshots and
    re-applying history would double-count. Deduped on (loan, external_ref),
    backed by migration 033's partial unique index."""
    loan_by_acct = {
        acct: loan_id
        for (acct, loan_id) in db.query(
            PlatformLoan.legacy_account_number, PlatformLoan.id
        )
        .filter(PlatformLoan.legacy_account_number.isnot(None))
        .all()
    }
    seen = {
        (loan_id, ref)
        for (loan_id, ref) in db.query(
            PlatformLoanPayment.loan_id, PlatformLoanPayment.external_ref
        )
        .filter(PlatformLoanPayment.external_ref.isnot(None))
        .all()
    }
    for row in rows:
        row_no = row.get("_row", 0)
        try:
            with db.begin_nested():
                loan_id = loan_by_acct.get(row["legacy_account_number"])
                if loan_id is None:
                    _fail(res, row_no, "unknown loan account at apply time")
                    continue
                key = (loan_id, row["external_ref"])
                if key in seen:
                    res.skipped_existing += 1
                    continue
                d = date.fromisoformat(row["payment_date"])
                method = "turnkey_migration"
                if row.get("method"):
                    method = f"turnkey_migration:{row['method']}"
                db.add(
                    PlatformLoanPayment(
                        loan_id=loan_id,
                        amount_cents=row["amount_cents"],
                        received_at=datetime(d.year, d.month, d.day, tzinfo=timezone.utc),
                        method=method,
                        external_ref=row["external_ref"],
                    )
                )
                seen.add(key)
                res.applied += 1
        except Exception as exc:
            _fail(res, row_no, f"{type(exc).__name__} while inserting payment")


def _apply_disbursements(db: Session, rows: list[dict], res: ApplyResult) -> None:
    """Mark the funding leg of migrated loans completed-HISTORICAL: backfill
    disbursed_at / disbursement_ref. No money moves; loan import already sets
    disbursement_status='completed', so fully-populated loans are skipped."""
    amount_mismatches = 0
    for row in rows:
        row_no = row.get("_row", 0)
        try:
            with db.begin_nested():
                loan = (
                    db.query(PlatformLoan)
                    .filter(PlatformLoan.legacy_account_number == row["legacy_account_number"])
                    .first()
                )
                if loan is None:
                    _fail(res, row_no, "unknown loan account at apply time")
                    continue
                if loan.source != "turnkey_migration":
                    _fail(res, row_no, "loan is not a migrated loan — refusing to touch a native disbursement")
                    continue
                if row["amount_cents"] != loan.principal_cents:
                    amount_mismatches += 1
                d = date.fromisoformat(row["disbursement_date"])
                ref = derive_disbursement_ref(row["legacy_account_number"], d, row.get("reference"))
                changed = False
                if loan.disbursed_at is None:
                    loan.disbursed_at = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
                    changed = True
                if loan.disbursement_ref is None:
                    loan.disbursement_ref = ref
                    changed = True
                if loan.disbursement_status != "completed":
                    loan.disbursement_status = "completed"
                    changed = True
                if changed:
                    res.applied += 1
                else:
                    res.skipped_existing += 1
        except Exception as exc:
            _fail(res, row_no, f"{type(exc).__name__} while marking disbursement")
    res.details = {"amount_mismatches_vs_principal": amount_mismatches}


_APPLIERS = {
    "customers": _apply_customers,
    "loans": _apply_loans,
    "payments": _apply_payments,
    "disbursements": _apply_disbursements,
}


def apply_rows(db: Session, entity_type: str, rows: list[dict]) -> ApplyResult:
    """Apply validated, normalized rows idempotently. Per-row savepoints: a
    failing row is rolled back + recorded, the rest of the batch proceeds.
    Does NOT commit — the caller owns the transaction (so the batch bookkeeping
    + audit event land atomically with the applied rows)."""
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"unknown entity_type {entity_type!r}")
    res = ApplyResult(entity_type=entity_type)
    _APPLIERS[entity_type](db, rows, res)
    return res
