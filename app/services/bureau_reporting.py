"""Equifax month-end batch generation (WS-I) — TEST MODE ONLY.

Dave (09__WP_Tools, must-have): "we send credit bureau updates on our loans and
their statuses to Equifax... we need to be able to have it in the format that
Equifax requires... we want basically first day of every month", plus a manual
custom-update option.

WHAT THIS IS: the month-end BATCH GENERATOR. It selects the accounts flagged
``bureau_reportable`` by the month-end delinquency snapshot (Pot-60 and
deeper, WS-H) and renders a Metro2-STYLE fixed-width flat file (header / one
base segment per account / trailer), persisting it to
``platform_bureau_batches`` — idempotent per month.

WHAT THIS IS NOT: nothing is transmitted. The Equifax subscriber agreement is
pending (Dave item), and with it the CERTIFIED Metro2 layout + member number.
The layout below is a faithful Metro2-shaped scaffold (426-char base segments
are descoped to a documented compact layout) so the selection logic, month-end
cron shape, storage, and re-run semantics are real; swapping in the certified
field layout + SFTP transmission is the follow-up once the agreement lands.

Pure core (``generate_batch_content``) is DB-free; the runner mirrors the
bucket-snapshot job's idempotent-per-month semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional, Sequence

from sqlalchemy.orm import Session

from app.models.platform.bureau_batch import PlatformBureauBatch
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.loan import PlatformLoan, PlatformLoanDelinquencySnapshot
from app.models.platform.patient import PlatformPatient
from app.services.delinquency_buckets import default_snapshot_month, month_end_of

# Metro2 account-status codes for the delinquency ladder (Field 17A). The
# bucket→code mapping follows the standard Metro2 aging codes; ``written_off``
# reports as charged off. Insolvency reports the aging status — the consumer
# information indicator (bankruptcy/proposal) is part of the certified-layout
# follow-up.
BUCKET_TO_METRO2_STATUS = {
    "pot_60": "78",   # 60-89 days past due
    "pot_90": "80",   # 90-119 days past due
    "default": "84",  # 120+ days past due (ladder collapsed — see module note)
    "insolvency": "84",
    "written_off": "97",  # charged off
}

REPORTER_NAME = "PAYSPYRE FINANCIAL"
PORTFOLIO_TYPE = "I"  # installment
ACCOUNT_TYPE = "12"   # medical debt / installment sales contract family (scaffold)


@dataclass(frozen=True)
class ReportableAccount:
    """One Pot-60+ account for the month's batch (DB-free value object)."""

    account_number: str
    consumer_name: str
    date_opened: Optional[date]
    original_amount_cents: int
    balance_cents: int
    days_past_due: int
    bucket: str


def _pad(value: str, width: int) -> str:
    return (value or "")[:width].ljust(width)


def _num(cents: int, width: int) -> str:
    """Whole-dollar amount, zero-padded (Metro2 reports whole dollars)."""
    dollars = max(0, cents) // 100
    return str(min(dollars, 10**width - 1)).rjust(width, "0")


def _date8(d: Optional[date]) -> str:
    return d.strftime("%m%d%Y") if d else "0" * 8


def generate_batch_content(
    batch_month: date, accounts: Sequence[ReportableAccount]
) -> str:
    """Render the Metro2-style flat file (PURE — deterministic, DB-free).

    Layout (compact scaffold; fixed-width, one record per line):

      HEADER : 'HEADER' + activity date (month-end, MMDDYYYY) + reporter name
      BASE   : 'B' + account number(30) + status(2) + portfolio type(1) +
               account type(2) + date opened(8) + original amount(9) +
               current balance(9) + DPD(3) + consumer name(40)
      TRAILER: 'TRAILER' + record count(9) + total balance(9)
    """
    month_end = month_end_of(batch_month)
    lines = [
        "HEADER"
        + _date8(month_end)
        + _pad(REPORTER_NAME, 40)
        + _pad("EQUIFAX TEST MODE - DO NOT TRANSMIT", 40)
    ]
    total_balance = 0
    for acct in accounts:
        status = BUCKET_TO_METRO2_STATUS.get(acct.bucket)
        if status is None:
            # Never let a non-reportable bucket leak into the file.
            continue
        total_balance += acct.balance_cents
        lines.append(
            "B"
            + _pad(acct.account_number, 30)
            + status
            + PORTFOLIO_TYPE
            + ACCOUNT_TYPE
            + _date8(acct.date_opened)
            + _num(acct.original_amount_cents, 9)
            + _num(acct.balance_cents, 9)
            + str(min(max(acct.days_past_due, 0), 999)).rjust(3, "0")
            + _pad(acct.consumer_name.upper(), 40)
        )
    lines.append(
        "TRAILER" + str(len(lines) - 1).rjust(9, "0") + _num(total_balance, 9)
    )
    return "\n".join(lines) + "\n"


def _consumer_name(patient) -> str:
    if patient is None:
        return "UNKNOWN"
    parts = [
        getattr(patient, "legal_last_name", None),
        getattr(patient, "legal_first_name", None),
    ]
    return (", ".join(x for x in parts if x)) or "UNKNOWN"


def collect_reportable_accounts(
    db: Session, batch_month: date
) -> list[ReportableAccount]:
    """The month's Pot-60+ accounts, straight off the month-end delinquency
    snapshots (``bureau_reportable`` flag, WS-H) — deterministic re-runs."""
    rows = (
        db.query(PlatformLoanDelinquencySnapshot, PlatformLoan, PlatformPatient)
        .join(PlatformLoan, PlatformLoanDelinquencySnapshot.loan_id == PlatformLoan.id)
        .outerjoin(
            PlatformCreditApplication,
            PlatformLoan.application_id == PlatformCreditApplication.id,
        )
        .outerjoin(
            PlatformPatient,
            PlatformCreditApplication.patient_id == PlatformPatient.id,
        )
        .filter(
            PlatformLoanDelinquencySnapshot.snapshot_month == batch_month,
            PlatformLoanDelinquencySnapshot.bureau_reportable.is_(True),
        )
        .order_by(PlatformLoan.created_at.asc())
        .all()
    )
    accounts: list[ReportableAccount] = []
    for snap, loan, patient in rows:
        disbursed = loan.disbursed_at
        accounts.append(
            ReportableAccount(
                # The legacy Turnkey number when migrated, else the loan UUID.
                account_number=loan.legacy_account_number or str(loan.id),
                consumer_name=_consumer_name(patient),
                date_opened=(
                    disbursed.date() if hasattr(disbursed, "date") else disbursed
                ),
                original_amount_cents=loan.principal_cents,
                balance_cents=snap.outstanding_principal_cents,
                days_past_due=snap.days_past_due,
                bucket=snap.bucket,
            )
        )
    return accounts


@dataclass(frozen=True)
class BatchResult:
    batch_id: str
    batch_month: date
    account_count: int
    total_balance_cents: int
    file_name: str
    generation_count: int


def run_month_end_batch(
    db: Session,
    batch_month: Optional[date] = None,
    *,
    generated_by: str = "bureau_batch_job",
) -> BatchResult:
    """Generate (or re-generate) the month's batch. IDEMPOTENT PER MONTH:
    re-runs UPDATE the existing row and bump ``generation_count``. Commits.

    ``batch_month`` defaults to the most recently completed month-end (same
    rule as the bucket-snapshot job — run first-of-month, report last month).
    TEST MODE: the file is stored on the row; nothing is transmitted.
    """
    if batch_month is None:
        batch_month = default_snapshot_month(date.today())
    batch_month = batch_month.replace(day=1)

    accounts = collect_reportable_accounts(db, batch_month)
    content = generate_batch_content(batch_month, accounts)
    total_balance = sum(a.balance_cents for a in accounts)
    file_name = f"equifax_batch_{batch_month.strftime('%Y_%m')}.txt"

    existing = (
        db.query(PlatformBureauBatch)
        .filter(PlatformBureauBatch.batch_month == batch_month)
        .first()
    )
    if existing is not None:
        existing.content = content
        existing.file_name = file_name
        existing.account_count = len(accounts)
        existing.total_balance_cents = total_balance
        existing.generation_count = (existing.generation_count or 1) + 1
        existing.generated_by = generated_by
        batch = existing
    else:
        batch = PlatformBureauBatch(
            batch_month=batch_month,
            mode="test",
            file_name=file_name,
            content=content,
            account_count=len(accounts),
            total_balance_cents=total_balance,
            generated_by=generated_by,
        )
        db.add(batch)
    db.commit()
    db.refresh(batch)
    return BatchResult(
        batch_id=str(batch.id),
        batch_month=batch_month,
        account_count=batch.account_count,
        total_balance_cents=batch.total_balance_cents,
        file_name=batch.file_name,
        generation_count=batch.generation_count,
    )
