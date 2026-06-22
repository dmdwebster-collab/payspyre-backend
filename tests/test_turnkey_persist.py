"""Persisting migrated Turnkey loans into PaySpyre (DB-write step + schema rules)."""
from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.platform.loan import PlatformLoan
from app.services.loan_servicing import generate_amortization_schedule
from app.services.migration.turnkey import MappedLoan
from app.services.migration.turnkey_persist import persist_loans


def _active(acct: str = "900001") -> MappedLoan:
    sched = generate_amortization_schedule(250_000, 599, 12, date(2026, 7, 1), day_count="actual/360")
    return MappedLoan(
        acct=acct, status="active", principal_cents=500_000, annual_rate_bps=599,
        term_months=36, principal_balance_cents=250_000, disbursed_at=date(2024, 1, 10),
        forward_schedule=sched,
    )


def _paid(acct: str = "900002") -> MappedLoan:
    return MappedLoan(
        acct=acct, status="paid_off", principal_cents=300_000, annual_rate_bps=599,
        term_months=24, principal_balance_cents=0, disbursed_at=date(2022, 1, 1),
    )


def test_persist_active_loan_with_forward_schedule(db_session):
    res = persist_loans(db_session, [_active("900001")])
    assert res.created == 1 and res.skipped_existing == 0

    loan = db_session.query(PlatformLoan).filter_by(legacy_account_number="900001").one()
    assert loan.application_id is None
    assert loan.source == "turnkey_migration"
    assert loan.status == "active"
    assert loan.principal_cents == 500_000
    assert loan.principal_balance_cents == 250_000
    assert loan.agreement_status == "signed"
    assert loan.disbursement_status == "completed"
    assert len(loan.schedule) == 12
    # the persisted schedule ties out to the current outstanding balance
    assert sum(s.principal_cents for s in loan.schedule) == 250_000


def test_persist_is_idempotent_on_legacy_account(db_session):
    persist_loans(db_session, [_active("900003")])
    res2 = persist_loans(db_session, [_active("900003")])
    assert res2.created == 0 and res2.skipped_existing == 1
    assert db_session.query(PlatformLoan).filter_by(legacy_account_number="900003").count() == 1


def test_persist_closed_loan_is_record_only(db_session):
    persist_loans(db_session, [_paid("900004")])
    loan = db_session.query(PlatformLoan).filter_by(legacy_account_number="900004").one()
    assert loan.status == "paid_off"
    assert loan.principal_balance_cents == 0
    assert loan.schedule == []          # historical record, no forward schedule
    assert loan.principal_cents == 300_000  # original amount preserved


def test_check_constraint_rejects_originated_loan_without_application(db_session):
    # A non-migration loan (source defaults to 'application') with a NULL application_id
    # must be rejected by the DB CHECK — only migrated loans may omit the application.
    bad = PlatformLoan(
        application_id=None, principal_cents=1000, annual_rate_bps=599, term_months=12,
        status="active", principal_balance_cents=1000,
    )
    db_session.add(bad)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_migrated_loan_may_omit_application(db_session):
    # The mirror case: a migrated loan with NULL application_id is allowed.
    persist_loans(db_session, [_paid("900005")])
    assert db_session.query(PlatformLoan).filter_by(legacy_account_number="900005").one().application_id is None
