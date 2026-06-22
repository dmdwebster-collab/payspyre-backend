"""Importing Turnkey payment history into the PaySpyre ledger.

Covers the pure mapping layer (amount-unit/date/validation handling, warnings instead of
crashes) and the LEDGER-ONLY persist step (correct rows created; balance + schedule of
migrated loans left UNTOUCHED; idempotent re-import; unmatched accounts reported).

NOTE: these tests are NOT executed here (shared test DB). They are written to run on the
Postgres test DB via the ``db_session`` fixture in tests/conftest.py.
"""
from datetime import date, datetime, timezone

from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanPayment,
    PlatformLoanScheduleItem,
)
from app.services.loan_servicing import generate_amortization_schedule
from app.services.migration.turnkey import MappedLoan
from app.services.migration.turnkey_persist import persist_loans
from app.services.migration.turnkey_payments import (
    EXTERNAL_REF_PREFIX,
    MappedPayment,
    map_payment,
    map_payments,
    persist_payments,
)


# --- sample export rows (dict-keyed, matching the by-name Col constants) ---
def _row(acct="900100", amount="123.45", dt="2024-03-15", method="PAD", txn="TK-1"):
    return {
        "Acct#": acct,
        "Payment Date": dt,
        "Amount": amount,
        "Method": method,
        "Transaction Id": txn,
    }


def _seed_migrated_loan(db, acct="900100"):
    """Seed one migrated loan (source='turnkey_migration', application_id NULL, legacy
    account set, with a forward schedule) so we can attach payments to it."""
    sched = generate_amortization_schedule(
        250_000, 599, 12, date(2026, 7, 1), day_count="actual/360"
    )
    persist_loans(
        db,
        [
            MappedLoan(
                acct=acct,
                status="active",
                principal_cents=500_000,
                annual_rate_bps=599,
                term_months=36,
                principal_balance_cents=250_000,
                disbursed_at=date(2024, 1, 10),
                forward_schedule=sched,
            )
        ],
    )
    return db.query(PlatformLoan).filter_by(legacy_account_number=acct).one()


# ----------------------------- mapping layer -----------------------------
def test_map_payment_dollars_to_cents_and_utc_date():
    mp, warning = map_payment(_row(amount="123.45", dt="2024-03-15", txn="TK-9"))
    assert warning is None
    assert isinstance(mp, MappedPayment)
    assert mp.amount_cents == 12_345
    assert mp.received_at == datetime(2024, 3, 15, tzinfo=timezone.utc)
    assert mp.external_ref == f"{EXTERNAL_REF_PREFIX}TK-9"
    assert mp.method == "turnkey_migration:PAD"


def test_map_payment_amount_with_currency_symbols():
    mp, warning = map_payment(_row(amount="$1,250.00"))
    assert warning is None
    assert mp.amount_cents == 125_000


def test_map_payment_native_datetime_cell():
    mp, _ = map_payment(_row(dt=datetime(2023, 1, 2, 9, 30)))
    assert mp.received_at == datetime(2023, 1, 2, 9, 30, tzinfo=timezone.utc)


def test_map_payment_collects_warnings_not_crash():
    # missing account, non-positive amount, bad date, missing txn id -> all warnings
    bad_rows = [
        _row(acct=""),
        _row(amount="-5.00"),
        _row(amount="not-a-number"),
        _row(dt="garbage"),
        _row(txn=""),
    ]
    res = map_payments(bad_rows)
    assert res.payments == []
    assert len(res.invalid) == 5
    assert res.total_rows == 5


def test_map_payments_mixed_valid_and_invalid():
    res = map_payments([_row(txn="TK-A"), _row(acct="", txn="TK-B"), _row(txn="TK-C")])
    assert len(res.payments) == 2
    assert len(res.invalid) == 1


# ----------------------------- persist (ledger-only) -----------------------------
def test_persist_creates_ledger_rows_with_correct_fields(db_session):
    loan = _seed_migrated_loan(db_session, "900100")
    res_map = map_payments([
        _row(acct="900100", amount="100.00", dt="2024-02-01", txn="TK-1"),
        _row(acct="900100", amount="100.00", dt="2024-03-01", txn="TK-2"),
    ])
    res = persist_payments(db_session, res_map.payments, invalid_count=len(res_map.invalid))
    assert res.imported == 2
    assert res.skipped_duplicate == 0
    assert res.unmatched_account == 0
    assert res.invalid == 0

    payments = (
        db_session.query(PlatformLoanPayment)
        .filter_by(loan_id=loan.id)
        .order_by(PlatformLoanPayment.received_at)
        .all()
    )
    assert [p.amount_cents for p in payments] == [10_000, 10_000]
    assert payments[0].received_at == datetime(2024, 2, 1, tzinfo=timezone.utc)
    assert payments[0].external_ref == f"{EXTERNAL_REF_PREFIX}TK-1"
    assert payments[1].external_ref == f"{EXTERNAL_REF_PREFIX}TK-2"
    assert all(p.method == "turnkey_migration:PAD" for p in payments)


def test_persist_does_not_touch_balance_or_schedule(db_session):
    """The whole point: historical payments are ledger-only. Importing them must NOT
    change the migrated loan's principal_balance_cents or its amortization schedule."""
    loan = _seed_migrated_loan(db_session, "900200")
    balance_before = loan.principal_balance_cents
    schedule_before = [
        (s.installment_number, s.total_cents, s.paid_cents, s.status)
        for s in db_session.query(PlatformLoanScheduleItem)
        .filter_by(loan_id=loan.id)
        .order_by(PlatformLoanScheduleItem.installment_number)
        .all()
    ]
    assert balance_before == 250_000
    assert len(schedule_before) == 12

    res_map = map_payments([
        _row(acct="900200", amount="500.00", dt="2024-01-01", txn="TK-10"),
        _row(acct="900200", amount="500.00", dt="2024-02-01", txn="TK-11"),
    ])
    persist_payments(db_session, res_map.payments)

    db_session.refresh(loan)
    assert loan.principal_balance_cents == balance_before  # UNCHANGED
    schedule_after = [
        (s.installment_number, s.total_cents, s.paid_cents, s.status)
        for s in db_session.query(PlatformLoanScheduleItem)
        .filter_by(loan_id=loan.id)
        .order_by(PlatformLoanScheduleItem.installment_number)
        .all()
    ]
    assert schedule_after == schedule_before  # UNCHANGED — no installment marked paid


def test_persist_is_idempotent_on_external_ref(db_session):
    _seed_migrated_loan(db_session, "900300")
    rows = [_row(acct="900300", amount="100.00", dt="2024-02-01", txn="TK-DUP")]

    first = persist_payments(db_session, map_payments(rows).payments)
    assert first.imported == 1

    second = persist_payments(db_session, map_payments(rows).payments)
    assert second.imported == 0
    assert second.skipped_duplicate == 1

    loan = db_session.query(PlatformLoan).filter_by(legacy_account_number="900300").one()
    assert (
        db_session.query(PlatformLoanPayment).filter_by(loan_id=loan.id).count() == 1
    )


def test_persist_reports_unmatched_account_without_crashing(db_session):
    _seed_migrated_loan(db_session, "900400")
    rows = [
        _row(acct="900400", amount="100.00", txn="TK-M1"),     # matches
        _row(acct="DOES-NOT-EXIST", amount="50.00", txn="TK-M2"),  # no loan
    ]
    res = persist_payments(db_session, map_payments(rows).payments)
    assert res.imported == 1
    assert res.unmatched_account == 1
    assert res.unmatched_accts == ["DOES-NOT-EXIST"]


def test_persist_passes_through_invalid_count(db_session):
    _seed_migrated_loan(db_session, "900500")
    res_map = map_payments([
        _row(acct="900500", txn="TK-OK"),
        _row(acct="", txn="TK-BAD"),  # invalid -> warning
    ])
    res = persist_payments(db_session, res_map.payments, invalid_count=len(res_map.invalid))
    assert res.imported == 1
    assert res.invalid == 1
