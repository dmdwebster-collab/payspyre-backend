"""The guarded demo-data purge.

The most destructive operation in the codebase, so what is pinned here is the
FENCE, not the deleting:

* it refuses in production, with no override;
* it refuses without the exact confirmation phrase;
* it refuses if a retained login does not exist, rather than emptying the
  database and locking everyone out;
* it deletes the demonstration domain and KEEPS the named operator accounts;
* the dry run counts and changes nothing;
* configuration (products, roles, permissions) is never demo data.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.core.config import settings
from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanPayment,
    PlatformLoanTransaction,
)
from app.models.platform.patient import PlatformPatient
from app.models.user import User
from app.services import demo_purge

RETAINED = ("dave@payspyrebeta.com", "admin@payspyrebeta.com")


@pytest.fixture
def seeded(db_session):
    """Two retained operator logins, one throwaway login, and a demo borrower
    with a loan, a payment receipt and a ledger row."""
    for email in RETAINED:
        db_session.add(
            User(email=email, first_name="Op", last_name="Erator", password_hash="x")
        )
    db_session.add(
        User(email="demo.tester@example.com", first_name="Demo", last_name="Tester",
             password_hash="x")
    )
    patient = PlatformPatient(legal_first_name="Demo", legal_last_name="Borrower")
    db_session.add(patient)
    db_session.flush()

    loan = PlatformLoan(
        application_id=None,
        patient_id=patient.id,
        source="portfolio_import",
        legacy_account_number="DEMO-1",
        principal_cents=100000,
        annual_rate_bps=999,
        term_months=12,
        status="active",
        principal_balance_cents=90000,
    )
    db_session.add(loan)
    db_session.flush()
    db_session.add(
        PlatformLoanPayment(
            loan_id=loan.id, amount_cents=10000,
            received_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            method="portfolio_import", external_ref="portfolio:DEMO-1",
        )
    )
    payment = PlatformLoanTransaction(
        loan_id=loan.id, seq=1, reference="none-x-1", txn_type="payment",
        amount_cents=10000, principal_cents=10000, interest_cents=0,
        fees_cents=0, effective_date=date(2025, 1, 1),
        processing_date=date(2025, 1, 1), created_by="portfolio_import",
    )
    db_session.add(payment)
    db_session.flush()
    # A REVERSAL, so the ledger under test carries the self-reference an imported
    # book is full of (every NSF/return links to the payment it undoes).
    db_session.add(
        PlatformLoanTransaction(
            loan_id=loan.id, seq=2, reference="none-x-2", txn_type="reversal",
            amount_cents=10000, principal_cents=-10000, interest_cents=0,
            fees_cents=0, effective_date=date(2025, 2, 1),
            processing_date=date(2025, 2, 1), created_by="portfolio_import",
            reverses_transaction_id=payment.id,
        )
    )
    db_session.commit()
    return db_session


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_it_refuses_in_production_with_no_override(seeded, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(demo_purge.PurgeRefused) as exc:
        demo_purge.purge(
            seeded, confirmation=demo_purge.CONFIRMATION_TOKEN, retain_emails=RETAINED
        )
    assert "production" in str(exc.value)
    # Even counting is refused there — the numbers must never read as a rehearsal.
    with pytest.raises(demo_purge.PurgeRefused):
        demo_purge.dry_run(seeded)
    seeded.rollback()
    assert seeded.query(PlatformPatient).count() == 1


def test_it_refuses_without_the_exact_confirmation_phrase(seeded):
    for bad in (None, "", "yes", "true", "purge-demo-data", "PURGE_DEMO_DATA"):
        with pytest.raises(demo_purge.PurgeRefused) as exc:
            demo_purge.purge(seeded, confirmation=bad, retain_emails=RETAINED)
        assert "confirmation" in str(exc.value)
    seeded.rollback()
    assert seeded.query(PlatformLoan).count() == 1


def test_it_refuses_when_a_retained_login_does_not_exist(seeded):
    """Emptying the database and leaving no way back in is worse than not
    purging at all."""
    with pytest.raises(demo_purge.PurgeRefused) as exc:
        demo_purge.purge(
            seeded,
            confirmation=demo_purge.CONFIRMATION_TOKEN,
            retain_emails=["nobody@nowhere.invalid"],
        )
    assert "do not exist" in str(exc.value)
    seeded.rollback()
    assert seeded.query(User).count() == 3


def test_it_refuses_an_empty_retain_list(seeded):
    with pytest.raises(demo_purge.PurgeRefused) as exc:
        demo_purge.purge(
            seeded, confirmation=demo_purge.CONFIRMATION_TOKEN, retain_emails=[]
        )
    assert "every login" in str(exc.value)
    seeded.rollback()


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_the_dry_run_counts_and_deletes_nothing(seeded):
    report = demo_purge.dry_run(seeded, retain_emails=RETAINED)
    assert report.dry_run is True
    assert report.tables["platform_patients"] == 1
    assert report.tables["platform_loans"] == 1
    assert report.tables["platform_loan_payments"] == 1
    assert report.tables["platform_loan_transactions"] == 2  # payment + its reversal
    # Only the ONE non-retained login is counted.
    assert report.tables["users"] == 1
    assert report.total_rows >= 5

    seeded.rollback()
    assert seeded.query(PlatformPatient).count() == 1
    assert seeded.query(PlatformLoan).count() == 1
    assert seeded.query(User).count() == 3


def test_the_dry_run_counts_match_what_the_purge_then_deletes(seeded):
    planned = demo_purge.dry_run(seeded, retain_emails=RETAINED)
    actual = demo_purge.purge(
        seeded,
        confirmation=demo_purge.CONFIRMATION_TOKEN,
        retain_emails=RETAINED,
        commit=True,
    )
    for table in ("platform_patients", "platform_loans", "platform_loan_payments",
                  "platform_loan_transactions", "users"):
        assert actual.tables[table] == planned.tables[table], table


# ---------------------------------------------------------------------------
# The purge itself
# ---------------------------------------------------------------------------


def test_it_deletes_the_demo_domain_and_keeps_the_named_accounts(seeded):
    report = demo_purge.purge(
        seeded,
        confirmation=demo_purge.CONFIRMATION_TOKEN,
        retain_emails=RETAINED,
        commit=True,
    )
    assert report.dry_run is False
    assert seeded.query(PlatformPatient).count() == 0
    assert seeded.query(PlatformLoan).count() == 0
    assert seeded.query(PlatformLoanPayment).count() == 0

    remaining = {u.email for u in seeded.query(User).all()}
    assert remaining == set(RETAINED)


def test_it_removes_ledger_rows_despite_the_immutability_trigger(seeded):
    """``platform_loan_transactions`` is WORM — a trigger rejects DELETE. A purge
    is the one legitimate reason to remove those rows, and it must actually
    succeed rather than fail halfway."""
    assert seeded.query(PlatformLoanTransaction).count() == 2
    demo_purge.purge(
        seeded, confirmation=demo_purge.CONFIRMATION_TOKEN,
        retain_emails=RETAINED, commit=True,
    )
    assert seeded.query(PlatformLoanTransaction).count() == 0


def test_a_reversal_link_is_never_pre_cleared_to_break_the_self_reference(seeded):
    """Regression: the purge used to NULL ``reverses_transaction_id`` first, which
    violates ck_platform_loan_txn_reversal_ref — a reversal MUST name the row it
    undoes. A whole-table DELETE resolves the self-reference on its own, because
    Postgres checks the FK at end of statement when the referenced rows are gone
    too. Any imported book is full of these links (one per NSF/return)."""
    linked = (
        seeded.query(PlatformLoanTransaction)
        .filter(PlatformLoanTransaction.txn_type == "reversal")
        .one()
    )
    assert linked.reverses_transaction_id is not None

    report = demo_purge.purge(
        seeded, confirmation=demo_purge.CONFIRMATION_TOKEN,
        retain_emails=RETAINED, commit=True,
    )
    assert seeded.query(PlatformLoanTransaction).count() == 0
    # Both rows are counted as deleted — no statement in the plan is a no-op
    # bookkeeping UPDATE that would understate the report.
    assert report.tables["platform_loan_transactions"] == 2


def test_the_immutability_trigger_is_restored_afterwards(seeded):
    """The trigger is disabled inside the transaction only. Once the purge is
    done the ledger must be WORM again."""
    from sqlalchemy.exc import DatabaseError

    demo_purge.purge(
        seeded, confirmation=demo_purge.CONFIRMATION_TOKEN,
        retain_emails=RETAINED, commit=True,
    )
    loan = PlatformLoan(
        application_id=None, source="portfolio_import", legacy_account_number="AFTER-1",
        principal_cents=1000, annual_rate_bps=0, term_months=1, status="active",
        principal_balance_cents=1000,
    )
    seeded.add(loan)
    seeded.flush()
    txn = PlatformLoanTransaction(
        loan_id=loan.id, seq=1, reference="none-y-1", txn_type="payment",
        amount_cents=100, effective_date=date(2025, 1, 1),
        processing_date=date(2025, 1, 1), created_by="test",
    )
    seeded.add(txn)
    seeded.commit()

    with pytest.raises(DatabaseError):
        seeded.execute(
            PlatformLoanTransaction.__table__.delete().where(
                PlatformLoanTransaction.id == txn.id
            )
        )
    seeded.rollback()


def test_the_report_names_exactly_what_went(seeded):
    report = demo_purge.purge(
        seeded, confirmation=demo_purge.CONFIRMATION_TOKEN,
        retain_emails=RETAINED, commit=True,
    ).as_dict()
    assert report["dry_run"] is False
    assert report["retained_emails"] == sorted(e.lower() for e in RETAINED)
    assert report["tables"]["platform_loans"] == 1
    assert report["tables"]["users"] == 1
    # Tables that had nothing are reported as such rather than omitted, so the
    # operator can see the whole scope that was considered.
    assert "platform_credit_applications" in report["tables_empty"]


def test_vendors_survive_unless_explicitly_included(db_session):
    from app.models.loan import Vendor

    for email in RETAINED:
        db_session.add(
            User(email=email, first_name="Op", last_name="Erator", password_hash="x")
        )
    db_session.add(
        Vendor(business_name="Demo Clinic", business_type="corporation",
               contact_name="A", email="a@b.invalid", phone="1", address_line1="1",
               city="Kelowna", province="BC", postal_code="V1Y1A1")
    )
    db_session.commit()

    demo_purge.purge(
        db_session, confirmation=demo_purge.CONFIRMATION_TOKEN,
        retain_emails=RETAINED, commit=True,
    )
    assert db_session.query(Vendor).count() == 1

    demo_purge.purge(
        db_session, confirmation=demo_purge.CONFIRMATION_TOKEN,
        retain_emails=RETAINED, include_vendors=True, commit=True,
    )
    assert db_session.query(Vendor).count() == 0


def test_configuration_is_not_demonstration_data(seeded):
    """Roles and permissions are seeded by migrations and required by the
    platform. A purge must not touch them."""
    from app.models.user import Permission, Role

    roles_before = seeded.query(Role).count()
    perms_before = seeded.query(Permission).count()
    demo_purge.purge(
        seeded, confirmation=demo_purge.CONFIRMATION_TOKEN,
        retain_emails=RETAINED, commit=True,
    )
    assert seeded.query(Role).count() == roles_before
    assert seeded.query(Permission).count() == perms_before


def test_every_statement_is_a_literal_with_bound_parameters():
    """No SQL in this module is built by interpolating caller input — the retain
    list is always a bound parameter (bandit B608 stays N/A)."""
    for _, stmt in demo_purge._USER_DELETES:
        sql = str(stmt)
        assert ":retain" in sql
        assert "@" not in sql  # no e-mail address ever baked into the statement
    for _, stmt in demo_purge._DOMAIN_DELETES:
        assert "%" not in str(stmt) and "{" not in str(stmt)
