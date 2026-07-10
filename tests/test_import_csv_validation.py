"""Cutover CSV import (WS-D) — pure validation specs. DB-free: everything here
exercises ``validate_csv`` / the scalar parsers with an in-memory ImportContext."""
from datetime import date

import pytest

from app.services.migration.csv_import import (
    ENTITY_COLUMNS,
    ENTITY_TYPES,
    ImportContext,
    parse_iso_date,
    parse_money_cents,
    parse_rate_bps,
    template_for,
    validate_csv,
)


def _csv(header: str, *rows: str) -> str:
    return "\n".join([header, *rows]) + "\n"


def _errors_by_field(result):
    return {(e.row, e.field): e.message for e in result.errors}


# ---------------------------------------------------------------------------
# Money / rate / date parsers — strict, ambiguity-rejecting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,cents",
    [
        ("2500.00", 250000),
        ("2500", 250000),
        ("2,500.5", 250050),
        ("$1,234.56", 123456),
        ("$ 1,234.56", 123456),
        ("0.01", 1),
        ("0", 0),
        ("1,234,567.89", 123456789),
    ],
)
def test_parse_money_accepts_unambiguous_dollars(raw, cents):
    assert parse_money_cents(raw) == cents


@pytest.mark.parametrize(
    "raw",
    [
        "",            # empty
        "1.234",       # 3 decimals — ambiguous (European thousands?)
        "1,23",        # malformed group
        "12,34,56",    # malformed groups
        "-5",          # sign
        "(1.00)",      # accounting negative
        "1e3",         # exponent
        "1.2.3",
        "CAD 5",
        "five",
    ],
)
def test_parse_money_rejects_ambiguous(raw):
    with pytest.raises(ValueError):
        parse_money_cents(raw)


@pytest.mark.parametrize("raw,bps", [("5.99", 599), ("0", 0), ("12.99", 1299), ("29.99%", 2999), ("7", 700)])
def test_parse_rate_to_bps(raw, bps):
    assert parse_rate_bps(raw) == bps


@pytest.mark.parametrize("raw", ["", "5.999", "0.0599.", "-1", "101", "abc"])
def test_parse_rate_rejects_ambiguous_or_out_of_range(raw):
    with pytest.raises(ValueError):
        parse_rate_bps(raw)


def test_parse_date_strict_iso_only():
    assert parse_iso_date("2024-01-10") == date(2024, 1, 10)
    for bad in ("03/04/2025", "2024-13-01", "10-01-2024", ""):
        with pytest.raises(ValueError):
            parse_iso_date(bad)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def test_every_entity_has_a_template_with_header_and_example():
    for entity in ENTITY_TYPES:
        t = template_for(entity)
        names = [c["name"] for c in t["columns"]]
        assert t["header_csv"] == ",".join(names)
        assert len(t["example_row_csv"].split(",")) == len(names)
        assert any(c["required"] for c in t["columns"])


def test_customers_template_has_no_sin_column():
    # Full SINs must never transit a CSV round-trip (encrypted-path only).
    assert not any("sin" in c.name.lower() for c in ENTITY_COLUMNS["customers"])


# ---------------------------------------------------------------------------
# Customers validation
# ---------------------------------------------------------------------------

CUST_HEADER = "legacy_customer_id,first_name,last_name,email,phone,date_of_birth,street,city,province,postal_code"


def test_customers_happy_path_normalizes():
    r = validate_csv(
        "customers",
        _csv(CUST_HEADER, "581,Royce,Heidenreich,Royce@Example.com,+12505551234,1975-03-14,2033 Gordon Dr.,Kelowna,BC,V1Y 3J2"),
    )
    assert r.file_errors == [] and r.errors == []
    row = r.valid_rows[0]
    assert row["email"] == "royce@example.com"          # lowercased key
    assert row["date_of_birth"] == "1975-03-14"
    assert row["address"]["city"] == "Kelowna"
    assert row["_row"] == 1


def test_customers_missing_required_column_is_a_file_error():
    r = validate_csv("customers", _csv("first_name,last_name,email", "A,B,a@b.co"))
    assert any("legacy_customer_id" in fe for fe in r.file_errors)
    assert r.valid_rows == []


def test_customers_duplicate_legacy_id_and_email_in_file():
    r = validate_csv(
        "customers",
        _csv(
            CUST_HEADER,
            "581,A,One,a@x.com,,,,,,",
            "581,B,Two,b@x.com,,,,,,",
            "582,C,Three,a@x.com,,,,,,",
        ),
    )
    errs = _errors_by_field(r)
    assert (2, "legacy_customer_id") in errs
    assert (3, "email") in errs
    assert len(r.valid_rows) == 1  # only row 1 clean


def test_customers_existing_legacy_id_warns_idempotent_skip():
    ctx = ImportContext(existing_customer_legacy_ids={"581"})
    r = validate_csv("customers", _csv(CUST_HEADER, "581,A,One,a@x.com,,,,,,"), ctx)
    assert r.errors == []
    assert any(w.field == "legacy_customer_id" and "skipped" in w.message for w in r.warnings)
    assert len(r.valid_rows) == 1  # still applied (apply skips it idempotently)


def test_customers_bad_email_and_dob():
    r = validate_csv(
        "customers",
        _csv(CUST_HEADER, "581,A,One,not-an-email,,1850-01-01,,,,"),
    )
    fields = {e.field for e in r.errors}
    assert fields == {"email", "date_of_birth"}
    assert r.valid_rows == []


def test_customers_error_messages_never_echo_pii_values():
    r = validate_csv(
        "customers",
        _csv(CUST_HEADER, "581,Secret,Name,secret.person@private.example,,31/12/1980,,,,"),
    )
    blob = str(r.report())
    assert "secret.person" not in blob.lower()
    assert "Secret" not in blob
    assert "31/12/1980" not in blob  # DOB value never echoed
    assert any(e.field == "date_of_birth" for e in r.errors)  # ...but the error is reported


# ---------------------------------------------------------------------------
# Loans validation
# ---------------------------------------------------------------------------

LOAN_HEADER = (
    "legacy_account_number,customer_legacy_id,principal,annual_rate_percent,start_date,"
    "term_months,payment_frequency,status,principal_balance,next_due_date,final_payment_date,vendor"
)
_CTX = ImportContext(existing_customer_legacy_ids={"581"})


def _loan_row(**over):
    base = {
        "acct": "BC4906-0001", "cust": "581", "principal": "2500.00", "rate": "5.99",
        "start": "2024-01-10", "term": "24", "freq": "monthly", "status": "active",
        "balance": "1250.00", "next_due": "2026-08-01", "final": "2027-01-01", "vendor": "BC4906",
    }
    base.update(over)
    return ",".join(base[k] for k in ("acct", "cust", "principal", "rate", "start", "term", "freq", "status", "balance", "next_due", "final", "vendor"))


def test_loans_happy_path_normalizes_to_cents_and_bps():
    r = validate_csv("loans", _csv(LOAN_HEADER, _loan_row()), _CTX)
    assert r.file_errors == [] and r.errors == []
    row = r.valid_rows[0]
    assert row["principal_cents"] == 250000
    assert row["annual_rate_bps"] == 599
    assert row["principal_balance_cents"] == 125000
    assert row["status"] == "active"
    assert r.totals == {"principal_cents": 250000, "outstanding_cents": 125000}


def test_loans_unknown_customer_ref_is_an_error():
    r = validate_csv("loans", _csv(LOAN_HEADER, _loan_row(cust="999")), _CTX)
    assert any(e.field == "customer_legacy_id" for e in r.errors)
    assert r.valid_rows == []


def test_loans_duplicate_account_in_file():
    r = validate_csv("loans", _csv(LOAN_HEADER, _loan_row(), _loan_row()), _CTX)
    assert any(e.field == "legacy_account_number" and e.row == 2 for e in r.errors)
    assert len(r.valid_rows) == 1


def test_loans_turnkey_status_pair_maps_and_unmapped_rejects():
    ok = validate_csv("loans", _csv(LOAN_HEADER, _loan_row(status="OPEN/ACTIVE")), _CTX)
    assert ok.valid_rows[0]["status"] == "active"
    closed = validate_csv(
        "loans", _csv(LOAN_HEADER, _loan_row(status="CLOSED/PAID", balance="", next_due="", final="")), _CTX
    )
    assert closed.valid_rows[0]["status"] == "paid_off"
    assert closed.valid_rows[0]["principal_balance_cents"] == 0
    bad = validate_csv("loans", _csv(LOAN_HEADER, _loan_row(status="VOIDED/VOIDED")), _CTX)
    assert any(e.field == "status" for e in bad.errors)


def test_loans_active_requires_balance_and_closed_forces_zero():
    r = validate_csv("loans", _csv(LOAN_HEADER, _loan_row(balance="")), _CTX)
    assert any(e.field == "principal_balance" for e in r.errors)

    r2 = validate_csv("loans", _csv(LOAN_HEADER, _loan_row(status="paid_off", balance="100.00")), _CTX)
    assert r2.errors == []
    assert r2.valid_rows[0]["principal_balance_cents"] == 0
    assert any(w.field == "principal_balance" for w in r2.warnings)


def test_loans_nonmonthly_frequency_rejected_v1():
    r = validate_csv("loans", _csv(LOAN_HEADER, _loan_row(freq="biweekly")), _CTX)
    assert any(e.field == "payment_frequency" for e in r.errors)


def test_loans_ambiguous_money_is_rejected_with_value_echo_ok():
    # principal is NOT a PII field — the offending value may be echoed for triage.
    r = validate_csv("loans", _csv(LOAN_HEADER, _loan_row(principal="1.234")), _CTX)
    err = next(e for e in r.errors if e.field == "principal")
    assert "1.234" in err.message


def test_loans_active_without_schedule_dates_warns():
    r = validate_csv("loans", _csv(LOAN_HEADER, _loan_row(next_due="", final="")), _CTX)
    assert r.errors == []
    assert any("forward schedule" in w.message for w in r.warnings)


def test_loans_criminal_rate_warns_but_imports():
    r = validate_csv("loans", _csv(LOAN_HEADER, _loan_row(rate="59.99")), _CTX)
    assert r.errors == []
    assert any(w.field == "annual_rate_percent" for w in r.warnings)


# ---------------------------------------------------------------------------
# Payments validation
# ---------------------------------------------------------------------------

PAY_HEADER = "legacy_account_number,payment_date,amount,type,reference"
_PCTX = ImportContext(existing_loan_accounts={"BC4906-0001"})


def test_payments_happy_path_derives_ref():
    r = validate_csv("payments", _csv(PAY_HEADER, "BC4906-0001,2025-06-01,123.45,PAD,"), _PCTX)
    assert r.errors == []
    row = r.valid_rows[0]
    assert row["external_ref"] == "import:BC4906-0001:2025-06-01:12345"
    assert row["method"] == "PAD"
    assert r.totals == {"amount_cents": 12345}


def test_payments_supplied_reference_namespaced_turnkey():
    r = validate_csv("payments", _csv(PAY_HEADER, "BC4906-0001,2025-06-01,123.45,PAD,TXN-88123"), _PCTX)
    assert r.valid_rows[0]["external_ref"] == "turnkey:TXN-88123"


def test_payments_identical_rows_without_reference_are_ambiguous():
    r = validate_csv(
        "payments",
        _csv(PAY_HEADER, "BC4906-0001,2025-06-01,123.45,PAD,", "BC4906-0001,2025-06-01,123.45,PAD,"),
        _PCTX,
    )
    assert any(e.row == 2 and "reference" in e.message for e in r.errors)
    assert len(r.valid_rows) == 1


def test_payments_identical_rows_with_distinct_references_both_import():
    r = validate_csv(
        "payments",
        _csv(PAY_HEADER, "BC4906-0001,2025-06-01,123.45,PAD,T1", "BC4906-0001,2025-06-01,123.45,PAD,T2"),
        _PCTX,
    )
    assert r.errors == []
    assert len(r.valid_rows) == 2


def test_payments_unknown_account_and_nonpositive_amount():
    r = validate_csv(
        "payments",
        _csv(PAY_HEADER, "NOPE,2025-06-01,123.45,,", "BC4906-0001,2025-06-01,0,,"),
        _PCTX,
    )
    errs = _errors_by_field(r)
    assert (1, "legacy_account_number") in errs
    assert (2, "amount") in errs
    assert r.valid_rows == []


def test_payments_already_imported_warns_idempotent():
    ctx = ImportContext(
        existing_loan_accounts={"BC4906-0001"},
        existing_payment_refs={("BC4906-0001", "turnkey:T1")},
    )
    r = validate_csv("payments", _csv(PAY_HEADER, "BC4906-0001,2025-06-01,123.45,PAD,T1"), ctx)
    assert r.errors == []
    assert any("already imported" in w.message for w in r.warnings)


# ---------------------------------------------------------------------------
# Disbursements validation
# ---------------------------------------------------------------------------

DISB_HEADER = "legacy_account_number,disbursement_date,amount,reference"


def test_disbursements_happy_and_duplicate_account():
    r = validate_csv(
        "disbursements",
        _csv(DISB_HEADER, "BC4906-0001,2024-01-10,2500.00,DSB-1", "BC4906-0001,2024-01-11,2500.00,DSB-2"),
        _PCTX,
    )
    assert any(e.row == 2 and e.field == "legacy_account_number" for e in r.errors)
    assert len(r.valid_rows) == 1
    assert r.valid_rows[0]["amount_cents"] == 250000


def test_disbursements_unknown_account_is_an_error():
    r = validate_csv("disbursements", _csv(DISB_HEADER, "NOPE,2024-01-10,2500.00,"), _PCTX)
    assert any(e.field == "legacy_account_number" for e in r.errors)


# ---------------------------------------------------------------------------
# File-level behavior
# ---------------------------------------------------------------------------


def test_empty_file_and_header_only_are_file_errors():
    for text in ("", "\n", PAY_HEADER + "\n"):
        r = validate_csv("payments", text, _PCTX)
        assert r.file_errors, text
        assert r.valid_rows == []


def test_unknown_entity_type_raises():
    with pytest.raises(ValueError):
        validate_csv("vendors", "a,b\n1,2\n")


def test_unknown_extra_columns_warn_and_are_ignored():
    r = validate_csv(
        "payments",
        _csv(PAY_HEADER + ",mystery_col", "BC4906-0001,2025-06-01,123.45,PAD,,huh"),
        _PCTX,
    )
    assert any(w.row == 0 and "unknown column" in w.message for w in r.warnings)
    assert len(r.valid_rows) == 1
    assert "mystery_col" not in r.valid_rows[0]
