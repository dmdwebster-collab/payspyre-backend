"""Persist mapped Turnkey loans into PaySpyre (the DB-write step).

Kept separate from ``turnkey.py`` (which is pure/DB-free): this turns the mapped
records into PlatformLoan rows (+ a forward schedule for active loans). Migrated loans
have ``application_id = NULL`` and ``source = 'turnkey_migration'`` (migration 035), and
are deduped on ``legacy_account_number`` so re-running is IDEMPOTENT.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.platform.loan import PlatformLoan, PlatformLoanScheduleItem
from app.services.migration.turnkey import MappedLoan


@dataclass
class PersistResult:
    created: int = 0
    skipped_existing: int = 0
    created_accts: list[str] = field(default_factory=list)


def _to_dt(d: Optional[date]) -> Optional[datetime]:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) if d else None


def persist_loans(db: Session, mapped: list[MappedLoan], *, commit: bool = True) -> PersistResult:
    """Create PlatformLoan rows from mapped Turnkey loans. Idempotent: a loan whose
    ``legacy_account_number`` already exists is skipped, so a partial/retried run is
    safe. Active loans also get their snapshot forward schedule; closed loans are a
    historical record (status + original principal, zero outstanding, no schedule).
    """
    result = PersistResult()
    existing = {
        acct
        for (acct,) in db.query(PlatformLoan.legacy_account_number)
        .filter(PlatformLoan.legacy_account_number.isnot(None))
        .all()
    }
    for m in mapped:
        if m.acct in existing:
            result.skipped_existing += 1
            continue
        loan = PlatformLoan(
            application_id=None,
            source="turnkey_migration",
            legacy_account_number=m.acct,
            principal_cents=m.principal_cents,
            annual_rate_bps=m.annual_rate_bps,
            term_months=m.term_months,
            status=m.status,
            principal_balance_cents=m.principal_balance_cents,
            disbursed_at=_to_dt(m.disbursed_at),
            # Migrated loans were already agreed + funded in the legacy system.
            agreement_status="signed",
            disbursement_status="completed",
            currency=m.currency,
        )
        for r in m.forward_schedule:
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
        db.add(loan)
        existing.add(m.acct)
        result.created += 1
        result.created_accts.append(m.acct)
    if commit:
        db.commit()
    return result
