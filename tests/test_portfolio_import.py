"""Portfolio import — the generic loan-book importer.

Pins the four promises the import makes:

1. It reads a source workbook's SHAPE from a declarative profile, so a different
   source system is a different profile rather than different code.
2. It imports each transaction's fee / interest / principal allocation EXACTLY
   as recorded — signs and all — and never re-derives one.
3. A balance that does not reconcile is REPORTED, never silently adjusted.
4. Placeholder contact details are only invented when a run explicitly asks,
   in the shape that run states.

The workbook under test is ``tests/fixtures/portfolio_book.py`` — entirely
synthetic. No real portfolio data lives in this repository.
"""
from __future__ import annotations

import pytest

from tests.fixtures.portfolio_book import build_workbook
from app.models.loan import Vendor
from app.models.platform.loan import PlatformLoan, PlatformLoanPayment
from app.models.platform.patient import PlatformPatient
from app.models.platform.patient_field import PlatformPatientField
from app.models.platform.provider import PlatformProvider
from app.services.migration import constants
from app.services.migration import portfolio_import as importer
from app.services.migration.borrower_completion import (
    NO_PLACEHOLDERS,
    PlaceholderPolicy,
    generate_contact,
)
from app.services.migration.portfolio_profile import (
    PortfolioProfile,
    get_profile,
)
from app.services.migration.portfolio_reconcile import reconcile_source
from app.services.migration.portfolio_workbook import (
    InMemoryWorkbook,
    ProfileMismatch,
    read_workbook,
    split_person_name,
    to_bps,
    to_cents,
    to_date,
)

PROFILE = get_profile()

#: The one-time testing shape: an unresolvable TLD and an unroutable area code.
KOM_555 = PlaceholderPolicy(
    enabled=True, email_domain="payspyre-import.kom", phone_area_code="555"
)


@pytest.fixture
def book():
    return read_workbook(build_workbook(), PROFILE)


# ---------------------------------------------------------------------------
# Reading a source workbook through a profile
# ---------------------------------------------------------------------------


def test_reads_the_source_shape_declared_by_the_profile(book):
    """Accounts, transactions, vendors and the provider roster all resolve from
    the profile's column bindings — no positional assumptions in the caller."""
    assert [a.account_number for a in book.accounts] == [
        "5001", "5002", "5003", "5004", "5005"
    ]
    assert len(book.transactions) == 12
    assert {v.code for v in book.vendors} == {"BC1000", "AB2000"}
    rosters = {v.code: v.providers for v in book.vendors}
    assert rosters["BC1000"] == ["Dr. Ada Lovelace", "Dr. Grace Hopper", "AR-General"]
    assert rosters["AB2000"] == ["Dr. Alan Turing"]


def test_totals_rows_are_not_mistaken_for_data(book):
    """A real export appends a TOTALS row carrying a row count where an
    identifier belongs. It must not become a 6th account / 13th transaction."""
    assert len(book.accounts) == 5
    assert len(book.transactions) == 12
    assert book.skipped_rows == 2  # one totals row on each sheet


def test_units_are_declared_not_guessed():
    """Dollars-with-float-noise become exact cents; a decimal-fraction rate
    becomes basis points; a non-date sentinel becomes None rather than raising."""
    assert to_cents(4159.5300000000002) == 415953
    assert to_cents(1138.0000000000002) == 113800
    assert to_cents("$1,234.56") == 123456
    assert to_cents("(45.00)") == -4500
    assert to_cents(1234, money_unit="cents") == 1234
    assert to_bps(0.0999) == 999
    assert to_bps(9.99, rate_unit="percent") == 999
    assert to_date("Closed") is None


def test_name_format_is_a_profile_declaration():
    assert split_person_name("Ramsey, Nora", "last_comma_first") == ("Nora", "Ramsey")
    assert split_person_name("Nora Ramsey", "first_last") == ("Nora", "Ramsey")
    assert split_person_name("", "last_comma_first") == (None, None)


def test_a_profile_that_does_not_fit_the_file_is_refused_not_guessed():
    """A required column the header does not contain is a hard mismatch — the
    importer never falls back to positional guessing."""
    wb = InMemoryWorkbook(
        {"Accounts": [[], [], ["Vendor", "Something Else"], ["BC1000", "x"]]}
    )
    with pytest.raises(ProfileMismatch) as exc:
        read_workbook(wb, PROFILE)
    assert "account_number" in str(exc.value)


def test_a_new_source_system_is_a_profile_not_a_code_change():
    """The whole mapping round-trips through JSON, so an operator can describe a
    source this repo has never seen and import it without a deploy."""
    as_json = PROFILE.to_dict()
    rebuilt = PortfolioProfile.from_dict(as_json)
    assert rebuilt.validate() == []
    assert rebuilt.to_dict() == as_json
    assert rebuilt.map_status("OPEN", "ACTIVE") == "active"
    assert rebuilt.rule_for("PMT-AUTOPAY").is_cash is True

    # Re-point the SAME profile at a differently-headed file: only data changes.
    renamed = PortfolioProfile.from_dict(
        {**as_json,
         "accounts": {**as_json["accounts"],
                      "columns": {**as_json["accounts"]["columns"],
                                  "account_number": "Loan Reference"}}}
    )
    assert renamed.accounts.columns["account_number"] == "Loan Reference"


def test_unmapped_status_and_type_are_reported_never_assumed():
    """A status or transaction type the profile does not know is surfaced, not
    coerced into the nearest-looking value."""
    assert PROFILE.map_status("OPEN", "SOMETHING_NEW") is None
    assert PROFILE.rule_for("CRYPTO-SETTLEMENT") is None


# ---------------------------------------------------------------------------
# Fidelity — allocations are transcribed, not re-derived
# ---------------------------------------------------------------------------


def test_transaction_allocations_are_read_exactly_as_recorded(book):
    nsf = next(t for t in book.transactions if t.source_type == "NSF/RETURN")
    # A return is NEGATIVE in the source, and stays negative here.
    assert nsf.payment_cents == -20736
    assert nsf.interest_paid_cents == -3712
    assert nsf.principal_paid_cents == -17024
    assert nsf.fees_charged_cents == 4500

    autopay = next(
        t for t in book.transactions
        if t.source_type == "PMT-AUTOPAY" and t.account_number == "5001"
    )
    assert autopay.payment_cents == 20736
    assert autopay.interest_paid_cents == 3713
    assert autopay.principal_paid_cents == 17023
    # Verbatim: the parts are what the source said, not a recomputed split.
    assert autopay.interest_paid_cents + autopay.principal_paid_cents == autopay.payment_cents


def test_ledger_rows_carry_the_source_allocation_verbatim(db_session):
    result = importer.apply_import(
        db_session,
        read_workbook(build_workbook(), PROFILE),
        importer.ImportOptions(placeholders=KOM_555),
        commit=False,
    )
    assert result.ledger_rows_created == 12 - 0  # every mapped row, incl. the voided loan's none
    loan = _loan(db_session, "5001")
    rows = sorted(loan.transactions, key=lambda t: t.seq)
    by_comment = {r.comment.split(" | ")[0]: r for r in rows}

    autopay = [r for r in rows if r.comment.startswith("PMT-AUTOPAY")][0]
    assert (autopay.principal_cents, autopay.interest_cents, autopay.fees_cents) == (
        17023, 3713, 0
    )
    # The return keeps its negative allocation; the non-negative amount column
    # carries magnitude only, with the direction living in txn_type.
    nsf = by_comment["NSF/RETURN"]
    assert nsf.txn_type == "reversal"
    assert nsf.amount_cents == 20736
    assert (nsf.principal_cents, nsf.interest_cents) == (-17024, -3712)
    assert nsf.reverses_transaction_id is not None
    db_session.rollback()


def test_a_return_is_linked_to_the_payment_it_reverses(db_session):
    importer.apply_import(
        db_session, read_workbook(build_workbook(), PROFILE),
        importer.ImportOptions(placeholders=KOM_555), commit=False,
    )
    loan = _loan(db_session, "5001")
    rows = {t.seq: t for t in loan.transactions}
    nsf = next(t for t in loan.transactions if t.txn_type == "reversal")
    reversed_row = rows[[s for s, t in rows.items() if t.id == nsf.reverses_transaction_id][0]]
    assert reversed_row.txn_type == "payment"
    assert reversed_row.amount_cents == nsf.amount_cents
    db_session.rollback()


def test_cash_rows_also_get_a_payment_receipt_but_non_cash_rows_do_not(db_session):
    importer.apply_import(
        db_session, read_workbook(build_workbook(), PROFILE),
        importer.ImportOptions(placeholders=KOM_555), commit=False,
    )
    loan = _loan(db_session, "5003")
    receipts = (
        db_session.query(PlatformLoanPayment)
        .filter(PlatformLoanPayment.loan_id == loan.id)
        .all()
    )
    # 5003 has an origination (not cash), a fee ADJUSTMENT (not cash) and one autopay.
    assert len(receipts) == 1
    assert receipts[0].amount_cents == 9026
    assert receipts[0].external_ref.startswith(constants.REF_PREFIX_SUPPLIED)
    db_session.rollback()


def test_the_stated_balance_is_never_recomputed_from_history(db_session):
    """Account 5005's transactions leave $250 outstanding while the account row
    claims zero. The loan carries the STATED figure — re-applying history to a
    snapshot balance is exactly the double-count this importer must not do."""
    importer.apply_import(
        db_session, read_workbook(build_workbook(), PROFILE),
        importer.ImportOptions(placeholders=KOM_555), commit=False,
    )
    assert _loan(db_session, "5005").principal_balance_cents == 0
    db_session.rollback()


# ---------------------------------------------------------------------------
# Reconciliation — report, never adjust
# ---------------------------------------------------------------------------


def test_a_balance_mismatch_is_reported_not_silently_fixed(book):
    report = reconcile_source(book.accounts, book.transactions)
    assert not report.ok
    assert report.accounts_with_discrepancies == ["5005"]
    measures = {d.measure for d in report.discrepancies}
    assert "principal_balance" in measures
    mismatch = next(d for d in report.discrepancies if d.measure == "principal_balance")
    assert mismatch.stated_cents == 0
    assert mismatch.derived_cents == 25000
    assert mismatch.delta_cents == 25000


def test_every_other_account_reconciles_exactly(book):
    report = reconcile_source(book.accounts, book.transactions)
    # 5 accounts; 5004 is VOIDED and has no transactions, 5005 is the exception.
    assert report.accounts_checked == 5
    assert report.accounts_reconciled == 3
    assert report.accounts_without_transactions == ["5004"]


def test_post_import_reconciliation_ties_what_landed_to_what_was_stated(db_session):
    result = importer.apply_import(
        db_session, read_workbook(build_workbook(), PROFILE),
        importer.ImportOptions(placeholders=KOM_555), commit=False,
    )
    persisted = result.persisted_reconciliation
    # The voided account is not imported and is listed as such, not counted good.
    assert persisted["accounts_not_imported"] == ["5004"]
    # 5005's stated balance disagrees with its own history — still reported.
    assert persisted["accounts_with_discrepancies"] == 0
    assert result.source_reconciliation["accounts_with_discrepancies"] == 1
    db_session.rollback()


def test_reconciliation_tolerance_is_a_cent_not_a_licence(book):
    tight = reconcile_source(book.accounts, book.transactions, tolerance_cents=0)
    assert not tight.ok
    # A $250 gap is not a rounding artefact at any sane tolerance.
    loose = reconcile_source(book.accounts, book.transactions, tolerance_cents=100)
    assert not loose.ok


# ---------------------------------------------------------------------------
# Vendors, providers, borrowers
# ---------------------------------------------------------------------------


def test_providers_are_seeded_from_the_vendor_record_and_linked_to_loans(db_session):
    result = importer.apply_import(
        db_session, read_workbook(build_workbook(), PROFILE),
        importer.ImportOptions(placeholders=KOM_555), commit=False,
    )
    assert result.providers_created == 4  # 3 at BC1000 + 1 at AB2000

    bc = db_session.query(Vendor).filter(Vendor.external_code == "BC1000").one()
    roster = {
        p.name for p in db_session.query(PlatformProvider)
        .filter(PlatformProvider.vendor_id == bc.id).all()
    }
    assert roster == {"Dr. Ada Lovelace", "Dr. Grace Hopper", "AR-General"}

    loan = _loan(db_session, "5001")
    assert loan.provider_id is not None
    assert loan.vendor_id == bc.id
    provider = (
        db_session.query(PlatformProvider)
        .filter(PlatformProvider.id == loan.provider_id).one()
    )
    assert provider.name == "Dr. Ada Lovelace"
    assert provider.vendor_id == bc.id
    db_session.rollback()


def test_a_borrower_with_two_loans_at_one_vendor_is_one_borrower(db_session):
    """Nora Ramsey holds 5001 (BC1000) and 5003 (AB2000) — different vendors, so
    two borrower records. Identity is (vendor, name) because a loan-book export
    carries no customer id; the rule is stated, not hidden."""
    result = importer.apply_import(
        db_session, read_workbook(build_workbook(), PROFILE),
        importer.ImportOptions(placeholders=KOM_555), commit=False,
    )
    assert result.borrowers_created == 5
    patients = db_session.query(PlatformPatient).all()
    assert sum(1 for p in patients if p.legal_last_name == "Ramsey") == 2
    db_session.rollback()


def test_vendors_are_created_from_the_vendor_sheet_and_matched_on_their_code(db_session):
    importer.apply_import(
        db_session, read_workbook(build_workbook(), PROFILE),
        importer.ImportOptions(placeholders=KOM_555), commit=False,
    )
    vendors = {v.external_code: v for v in db_session.query(Vendor).all()}
    assert set(vendors) == {"BC1000", "AB2000"}
    assert vendors["BC1000"].business_name == "Northside Dental"
    assert vendors["BC1000"].city == "Kelowna"
    assert vendors["BC1000"].province == "BC"
    db_session.rollback()


def test_a_voided_account_is_skipped_and_said_so(db_session):
    result = importer.apply_import(
        db_session, read_workbook(build_workbook(), PROFILE),
        importer.ImportOptions(placeholders=KOM_555), commit=False,
    )
    assert result.loans_skipped_status == ["5004"]
    assert _loan(db_session, "5004") is None
    db_session.rollback()


def test_reimporting_the_same_book_creates_nothing_twice(db_session):
    options = importer.ImportOptions(placeholders=KOM_555)
    first = importer.apply_import(
        db_session, read_workbook(build_workbook(), PROFILE), options, commit=False
    )
    db_session.flush()
    second = importer.apply_import(
        db_session, read_workbook(build_workbook(), PROFILE), options, commit=False
    )
    assert first.loans_created == 4
    assert second.loans_created == 0
    assert second.loans_skipped_existing == 4
    assert second.ledger_rows_created == 0
    assert second.transactions_skipped_duplicate > 0
    assert second.borrowers_matched == 5
    assert second.providers_created == 0
    db_session.rollback()


def test_loan_terms_carry_the_source_cadence_and_provenance(db_session):
    importer.apply_import(
        db_session, read_workbook(build_workbook(), PROFILE),
        importer.ImportOptions(placeholders=KOM_555), commit=False,
    )
    active = _loan(db_session, "5001")
    assert active.source == constants.PORTFOLIO_SOURCE
    assert active.application_id is None
    assert active.status == "active"
    assert active.payment_frequency == "monthly"
    assert active.annual_rate_bps == 999
    assert active.principal_cents == 450000

    biweekly = _loan(db_session, "5002")
    assert biweekly.payment_frequency == "bi_weekly"
    assert biweekly.status == "paid_off"
    assert biweekly.closed_at is not None
    db_session.rollback()


# ---------------------------------------------------------------------------
# Borrower completion — opt-in, and shaped by the run
# ---------------------------------------------------------------------------


def test_nothing_is_invented_unless_the_run_asks(db_session):
    result = importer.apply_import(
        db_session, read_workbook(build_workbook(), PROFILE),
        importer.ImportOptions(placeholders=NO_PLACEHOLDERS), commit=False,
    )
    assert result.borrowers_completed == 0
    assert result.placeholder_fields == {}
    assert all(p.email is None for p in db_session.query(PlatformPatient).all())
    assert all(p.phone_e164 is None for p in db_session.query(PlatformPatient).all())
    db_session.rollback()


def test_the_unroutable_shape_applies_only_when_the_flag_is_set(db_session):
    result = importer.apply_import(
        db_session, read_workbook(build_workbook(), PROFILE),
        importer.ImportOptions(placeholders=KOM_555), commit=False,
    )
    assert result.borrowers_completed == 5
    patients = db_session.query(PlatformPatient).all()
    assert patients
    for p in patients:
        # EVERY synthetic e-mail is undeliverable and every phone unroutable.
        assert p.email.endswith("@payspyre-import.kom")
        assert p.phone_e164.startswith("+1555")
    db_session.rollback()


def test_the_placeholder_shape_is_a_parameter_not_a_product_default():
    """The product default invents NOTHING; the ``.kom``/555 shape belongs to the
    run that asks for it, and is echoed on the report for audit."""
    assert NO_PLACEHOLDERS.enabled is False
    assert PlaceholderPolicy().enabled is False
    assert PlaceholderPolicy().email_domain.endswith(".invalid")

    described = KOM_555.describe()
    assert described["enabled"] is True
    assert described["email_tld"] == "kom"
    assert described["phone_area_code"] == "555"

    other = PlaceholderPolicy(
        enabled=True, email_domain="somewhere.test", phone_area_code="604"
    )
    c = generate_contact(key="k", first_name="A", last_name="B", policy=other)
    assert c.email.endswith("@somewhere.test")
    assert c.phone_e164.startswith("+1604")


def test_generated_contacts_are_deterministic_so_a_reimport_is_stable():
    a = generate_contact(key="BC1000:ramsey, nora", first_name="Nora",
                         last_name="Ramsey", policy=KOM_555)
    b = generate_contact(key="BC1000:ramsey, nora", first_name="Nora",
                         last_name="Ramsey", policy=KOM_555)
    assert (a.email, a.phone_e164, a.street, a.postal_code) == (
        b.email, b.phone_e164, b.street, b.postal_code
    )
    other = generate_contact(key="BC1000:other, sam", first_name="Sam",
                             last_name="Other", policy=KOM_555)
    assert other.email != a.email


def test_a_source_value_is_never_overwritten_by_a_placeholder():
    c = generate_contact(
        key="k", first_name="A", last_name="B", policy=KOM_555,
        existing_email="real@example.com", existing_phone="+12505551234",
    )
    assert c.email is None
    assert c.phone_e164 is None
    assert "email" not in c.generated_fields


def test_a_placeholder_policy_that_cannot_be_safe_is_refused():
    bad = PlaceholderPolicy(enabled=True, phone_area_code="55")
    assert any("area_code" in p for p in bad.validate())
    assert PlaceholderPolicy(enabled=True, email_domain="").validate()
    assert PlaceholderPolicy(enabled=False, phone_area_code="").validate() == []


def test_generated_addresses_are_placed_from_the_vendor_code(db_session):
    importer.apply_import(
        db_session, read_workbook(build_workbook(), PROFILE),
        importer.ImportOptions(placeholders=KOM_555), commit=False,
    )
    fields = (
        db_session.query(PlatformPatientField)
        .filter(PlatformPatientField.field_key == constants.IMPORT_ADDRESS_FIELD_KEY)
        .all()
    )
    assert len(fields) == 5
    provinces = {f.field_value["province"] for f in fields}
    assert provinces == {"BC", "AB"}  # AB2000's borrower lands in Alberta
    db_session.rollback()


# ---------------------------------------------------------------------------
# Retained legacy identifiers
# ---------------------------------------------------------------------------


def test_legacy_identifiers_stay_readable_after_the_rename():
    """Live rows carry the pre-rename spellings. New writes are neutral; reads
    accept both, so an already-migrated book still dedupes."""
    assert constants.PORTFOLIO_SOURCE == "portfolio_import"
    assert "turnkey_migration" in constants.IMPORTED_LOAN_SOURCES
    assert constants.REF_PREFIX_SUPPLIED == "portfolio:"
    assert "turnkey:" in constants.SUPPLIED_REF_PREFIXES
    assert constants.supplied_ref_variants("T7") == ("portfolio:T7", "turnkey:T7")
    assert "turnkey_legacy_customer_id" in constants.LEGACY_CUSTOMER_FIELD_KEYS
    assert constants.LEGACY_CUSTOMER_FIELD_KEY == "portfolio_legacy_customer_id"


def test_a_book_imported_under_the_old_source_value_is_not_reimported(db_session):
    """A loan already carrying the legacy provenance must be recognised as
    imported — otherwise the first run after the rename would duplicate it."""
    db_session.add(
        PlatformLoan(
            application_id=None,
            source=constants.LEGACY_PORTFOLIO_SOURCE,
            legacy_account_number="5001",
            principal_cents=450000,
            annual_rate_bps=999,
            term_months=24,
            status="active",
            principal_balance_cents=415953,
        )
    )
    db_session.flush()
    result = importer.apply_import(
        db_session, read_workbook(build_workbook(), PROFILE),
        importer.ImportOptions(placeholders=KOM_555), commit=False,
    )
    assert result.loans_skipped_existing == 1
    assert result.loans_created == 3
    db_session.rollback()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _loan(db, acct: str):
    return (
        db.query(PlatformLoan)
        .filter(PlatformLoan.legacy_account_number == acct)
        .first()
    )
