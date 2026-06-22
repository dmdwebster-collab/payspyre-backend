"""Turnkey -> PaySpyre importer mapping (synthetic rows; no real data)."""
from datetime import date, datetime

from app.services.migration.turnkey import (
    Col,
    build_report,
    map_loan,
    parse_account_row,
    parse_accounts,
)


def _row(over: dict | None = None) -> tuple:
    """Build a synthetic Accounts row (44+ cols) with sensible active-loan defaults."""
    row = [None] * 45
    base = {
        Col.VENDOR: "BC0001",
        Col.PROVIDER: "Dr. Test",
        Col.ACCT: "100200",
        Col.STATUS: "OPEN",
        Col.SUBSTATUS: "ACTIVE",
        Col.DAYS_PAST_DUE: 0,
        Col.AMOUNT_FINANCED: 5000,
        Col.TERM: 36,
        Col.RATE: 0.0599,
        Col.REGULAR_PAYMENT: 152.05,
        Col.COST_OF_BORROWING: 473.8,
        Col.ORIGINATION: datetime(2024, 1, 10),
        Col.FIRST_PMT: datetime(2024, 2, 10),
        Col.FINAL_PMT: datetime(2027, 1, 10),
        Col.NEXT_DUE: datetime(2026, 7, 10),
        Col.INTEREST_PAID: 300.0,
        Col.PRINCIPAL_PAID: 2500.0,
        Col.PRINCIPAL_BALANCE: 2500.0,
    }
    base.update(over or {})
    for c, v in base.items():
        row[c] = v
    return tuple(row)


def test_parse_active_loan():
    tk = parse_account_row(_row())
    assert tk is not None
    assert tk.acct == "100200"
    assert tk.amount_financed_cents == 500_000
    assert tk.annual_rate_bps == 599
    assert tk.term_months == 36
    assert tk.principal_balance_cents == 250_000
    assert tk.origination_date == date(2024, 1, 10)


def test_header_and_blank_rows_skipped():
    assert parse_account_row(_row({Col.ACCT: "Acct#"})) is None
    assert parse_account_row(_row({Col.ACCT: None})) is None
    assert parse_account_row(tuple([None] * 45)) is None


def test_status_mapping():
    assert map_loan(parse_account_row(_row())).status == "active"
    assert map_loan(parse_account_row(_row({Col.STATUS: "CLOSED", Col.SUBSTATUS: "PAID"}))).status == "paid_off"
    assert map_loan(parse_account_row(_row({Col.STATUS: "CLOSED", Col.SUBSTATUS: "WRITE OFF"}))).status == "charged_off"
    # voided -> not imported
    assert map_loan(parse_account_row(_row({Col.STATUS: "VOIDED", Col.SUBSTATUS: "VOIDED"}))) is None
    # unknown status -> not imported
    assert map_loan(parse_account_row(_row({Col.STATUS: "WEIRD", Col.SUBSTATUS: "???"}))) is None


def test_closed_loan_carries_no_outstanding_balance():
    m = map_loan(parse_account_row(_row({Col.STATUS: "CLOSED", Col.SUBSTATUS: "PAID",
                                            Col.PRINCIPAL_BALANCE: 0})))
    assert m.status == "paid_off"
    assert m.principal_balance_cents == 0
    assert m.principal_cents == 500_000  # original amount preserved
    assert m.forward_schedule == []      # no forward schedule for a closed loan


def test_active_loan_gets_actual_360_forward_schedule():
    m = map_loan(parse_account_row(_row()))
    assert m.status == "active"
    # Remaining: next due 2026-07-10 .. final 2027-01-10 inclusive = 7 installments.
    assert len(m.forward_schedule) == 7
    # The forward schedule amortizes exactly the CURRENT outstanding balance.
    assert sum(r.principal_cents for r in m.forward_schedule) == m.principal_balance_cents
    # ties out
    interest = sum(r.interest_cents for r in m.forward_schedule)
    assert sum(r.total_cents for r in m.forward_schedule) == m.principal_balance_cents + interest


def test_warnings_balance_exceeds_principal_and_bad_amount():
    m = map_loan(parse_account_row(_row({Col.PRINCIPAL_BALANCE: 9999})))  # > 5000 principal
    assert any("exceeds original principal" in w for w in m.warnings)
    m2 = map_loan(parse_account_row(_row({Col.AMOUNT_FINANCED: 0})))
    assert any("non-positive amount financed" in w for w in m2.warnings)


def test_build_report_aggregates_and_flags_unmapped():
    rows = [
        _row({Col.ACCT: "1", Col.STATUS: "OPEN", Col.SUBSTATUS: "ACTIVE"}),
        _row({Col.ACCT: "2", Col.STATUS: "CLOSED", Col.SUBSTATUS: "PAID", Col.PRINCIPAL_BALANCE: 0}),
        _row({Col.ACCT: "3", Col.STATUS: "VOIDED", Col.SUBSTATUS: "VOIDED"}),
        _row({Col.ACCT: "4", Col.STATUS: "MYSTERY", Col.SUBSTATUS: "X"}),
    ]
    mapped, rep = build_report(rows)
    assert rep.total_rows == 4
    assert rep.importable == 2  # active + paid (voided + mystery excluded)
    assert rep.by_paspyre_status == {"active": 1, "paid_off": 1}
    assert "4" in rep.skipped_unmapped       # unmapped status surfaced
    assert "3" not in rep.skipped_unmapped   # voided is an intentional skip, not unmapped


def test_parse_accounts_filters_non_rows():
    rows = [tuple([None] * 45), _row(), _row({Col.ACCT: "Acct#"})]
    assert len(parse_accounts(rows)) == 1
