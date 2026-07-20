"""Unit tests for the WS-B document / merge-field engine.

DELIBERATELY DB-free (fan-out protocol: the suite shares a remote DB and must
not be run wholesale by agents): the renderer, context builders, table
builders and template-resolution precedence are PURE functions over plain
attribute objects — tested directly with SimpleNamespace fakes, same idiom as
tests/test_delinquency_buckets.py.

Run JUST this file:

    source .venv/bin/activate && \
        python -m pytest tests/test_document_engine.py -p no:warnings -q
"""
import importlib.util
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.document_engine import (
    MERGE_FIELDS,
    TABLE_FIELDS,
    build_scalar_context,
    build_tables,
    pick_template,
    render_template,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _schedule_item(n, due, principal, interest, total):
    return SimpleNamespace(
        installment_number=n,
        due_date=due,
        principal_cents=principal,
        interest_cents=interest,
        total_cents=total,
    )


def _loan(**over):
    schedule = over.pop(
        "schedule",
        [
            _schedule_item(2, date(2026, 9, 1), 19_800, 1_100, 20_900),
            _schedule_item(1, date(2026, 8, 1), 19_700, 1_200, 20_900),
        ],
    )
    base = dict(
        id=uuid4(),
        status="active",
        principal_cents=240_000,
        annual_rate_bps=990,
        term_months=12,
        currency="CAD",
        disbursed_at=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        schedule=schedule,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _patient():
    return SimpleNamespace(
        legal_first_name="Alex",
        legal_last_name="Nguyen",
        email="alex@example.com",
        phone_e164="+12505551234",
        dob=date(1990, 3, 14),
    )


def _vendor():
    return SimpleNamespace(
        business_name="Kelowna Dental Centre",
        dba_name="KDC",
        email="office@kdc.example",
        phone="250-555-0000",
        address_line1="123 Main St",
        city="Kelowna",
        province="BC",
        postal_code="V1W 5A5",
    )


def _product(pricing=None):
    return SimpleNamespace(
        name="Dental Full Arch",
        code="dental_full_arch_v1",
        vertical="dental",
        pricing_config=pricing,
    )


PRICING = {
    "schema_version": 1,
    "fees": [
        {
            "fee_type": "origination",
            "calc": "fixed_cents",
            "amount": 2500,
            "charge_timing": "at_origination",
        },
        {
            "fee_type": "nsf",
            "calc": "fixed_cents",
            "amount": 4500,
            "charge_timing": "on_event",
            "add_on": True,
        },
        {
            "fee_type": "administration",
            "calc": "fixed_cents",
            "amount": 100,
            "charge_timing": "per_payment",
            "enabled": False,  # disabled -> must NOT render
        },
    ],
}


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class TestRenderTemplate:
    def test_scalar_substitution(self):
        result = render_template("<p>Hi {{BorrowerFirstName}}!</p>", {"BorrowerFirstName": "Alex"})
        assert result.html == "<p>Hi Alex!</p>"
        assert result.unknown_fields == ()

    def test_whitespace_tolerated_inside_braces(self):
        result = render_template("{{  LoanId  }}", {"LoanId": "L-1"})
        assert result.html == "L-1"

    def test_values_are_html_escaped(self):
        result = render_template(
            "{{VendorName}}", {"VendorName": '<script>alert("x")</script>'}
        )
        assert "<script>" not in result.html
        assert "&lt;script&gt;" in result.html

    def test_unknown_scalar_renders_empty_and_is_reported(self):
        result = render_template("a{{NopeField}}b", {})
        assert result.html == "ab"
        assert result.unknown_fields == ("NopeField",)

    def test_unknown_fields_deduplicated(self):
        result = render_template("{{Nope}}{{Nope}}{{Other}}", {})
        assert result.unknown_fields == ("Nope", "Other")

    def test_table_substitution(self):
        rows = [
            {"#": 1, "Due date": "2026-08-01", "Principal": "$197.00",
             "Interest": "$12.00", "Total": "$209.00"},
        ]
        result = render_template(
            "{{Table:AmortizationSchedule}}", {}, {"AmortizationSchedule": rows}
        )
        assert "<table" in result.html
        assert "<th>Due date</th>" in result.html
        assert "<td>$209.00</td>" in result.html
        assert result.unknown_fields == ()

    def test_table_cells_are_escaped(self):
        rows = [{"#": "<b>1</b>", "Due date": "", "Principal": "", "Interest": "", "Total": ""}]
        result = render_template(
            "{{Table:AmortizationSchedule}}", {}, {"AmortizationSchedule": rows}
        )
        assert "<b>1</b>" not in result.html
        assert "&lt;b&gt;1&lt;/b&gt;" in result.html

    def test_unknown_table_renders_empty_and_is_reported(self):
        result = render_template("x{{Table:NoSuchTable}}y", {}, {})
        assert result.html == "xy"
        assert result.unknown_fields == ("Table:NoSuchTable",)

    def test_known_table_without_data_is_reported_not_crashed(self):
        result = render_template("{{Table:FeeSchedule}}", {}, {})
        assert result.html == ""
        assert result.unknown_fields == ("Table:FeeSchedule",)


# ---------------------------------------------------------------------------
# Scalar context
# ---------------------------------------------------------------------------


class TestBuildScalarContext:
    def test_dictionary_and_context_stay_in_lockstep(self):
        """Every documented merge field is produced, and vice versa."""
        documented = {name for group in MERGE_FIELDS.values() for name in group}
        produced = set(build_scalar_context().keys())
        assert documented == produced

    def test_full_context_formatting(self):
        ctx = build_scalar_context(
            loan=_loan(), patient=_patient(), product=_product(), vendor=_vendor()
        )
        assert ctx["BorrowerFullName"] == "Alex Nguyen"
        assert ctx["BorrowerDateOfBirth"] == "1990-03-14"
        assert ctx["PrincipalAmount"] == "$2,400.00"
        assert ctx["AnnualInterestRate"] == "9.90%"
        assert ctx["TermMonths"] == "12"
        assert ctx["InstallmentCount"] == "2"
        # Schedule is sorted by installment number, not input order.
        assert ctx["FirstDueDate"] == "2026-08-01"
        assert ctx["MaturityDate"] == "2026-09-01"
        assert ctx["TotalOfPayments"] == "$418.00"
        assert ctx["TotalInterest"] == "$23.00"
        assert ctx["DisbursedDate"] == "2026-07-15"
        assert ctx["ProductName"] == "Dental Full Arch"
        assert ctx["VendorName"] == "Kelowna Dental Centre"
        assert ctx["VendorProvince"] == "BC"
        assert ctx["CompanyName"] == "PaySpyre Financial Inc."

    def test_missing_entities_render_blank_not_crash(self):
        ctx = build_scalar_context()  # no loan/patient/product/vendor at all
        assert ctx["BorrowerFullName"] == ""
        assert ctx["PrincipalAmount"] == ""
        assert ctx["FirstDueDate"] == ""
        # Company facts still resolve.
        assert ctx["CompanyName"] != ""

    def test_extra_context_wins(self):
        ctx = build_scalar_context(extra={"StatementOpeningBalance": "$10.00"})
        assert ctx["StatementOpeningBalance"] == "$10.00"

    def test_generated_date_uses_injected_now(self):
        ctx = build_scalar_context(now=datetime(2026, 1, 2, tzinfo=timezone.utc))
        assert ctx["GeneratedDate"] == "2026-01-02"


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class TestBuildTables:
    def test_amortization_rows_sorted_and_formatted(self):
        tables = build_tables(loan=_loan())
        rows = tables["AmortizationSchedule"]
        assert [r["#"] for r in rows] == [1, 2]
        assert rows[0]["Due date"] == "2026-08-01"
        assert rows[0]["Principal"] == "$197.00"
        assert rows[0]["Interest"] == "$12.00"
        assert rows[0]["Total"] == "$209.00"

    def test_fee_rows_from_pricing_config(self):
        tables = build_tables(loan=_loan(), product=_product(PRICING))
        fees = tables["FeeSchedule"]
        by_name = {r["Fee"]: r for r in fees}
        assert "Origination" in by_name
        assert by_name["Origination"]["Amount"] == "$25.00"
        assert by_name["Origination"]["When charged"] == "At origination"
        assert by_name["Origination"]["Add-on"] == "No"
        assert by_name["Nsf"]["Amount"] == "$45.00"
        assert by_name["Nsf"]["Add-on"] == "Yes"
        # Disabled fee must not render.
        assert "Administration" not in by_name

    def test_malformed_pricing_yields_empty_fee_table(self):
        tables = build_tables(
            loan=_loan(), product=_product({"schema_version": 999, "nope": True})
        )
        assert tables["FeeSchedule"] == []

    def test_no_entities(self):
        tables = build_tables()
        assert tables == {"AmortizationSchedule": [], "FeeSchedule": []}

    def test_table_names_match_documented_dictionary(self):
        assert set(build_tables().keys()) == set(TABLE_FIELDS.keys())


# ---------------------------------------------------------------------------
# Template resolution precedence (vendor > product > global; version desc)
# ---------------------------------------------------------------------------


def _tpl(scope="global", version=1, product_id=None, vendor_id=None):
    return SimpleNamespace(
        scope=scope, version=version, product_id=product_id, vendor_id=vendor_id
    )


class TestPickTemplate:
    PRODUCT = uuid4()
    VENDOR = uuid4()

    def test_global_only(self):
        g1, g3 = _tpl(version=1), _tpl(version=3)
        assert pick_template([g1, g3]) is g3

    def test_product_beats_global(self):
        g = _tpl(version=9)
        p = _tpl(scope="product", version=1, product_id=self.PRODUCT)
        assert pick_template([g, p], product_id=self.PRODUCT) is p

    def test_vendor_beats_product_and_global(self):
        g = _tpl(version=9)
        p = _tpl(scope="product", version=9, product_id=self.PRODUCT)
        v = _tpl(scope="vendor", version=1, vendor_id=self.VENDOR)
        assert (
            pick_template([g, p, v], product_id=self.PRODUCT, vendor_id=self.VENDOR)
            is v
        )

    def test_non_matching_scoped_templates_never_apply(self):
        g = _tpl(version=1)
        other_vendor = _tpl(scope="vendor", version=5, vendor_id=uuid4())
        other_product = _tpl(scope="product", version=5, product_id=uuid4())
        assert (
            pick_template(
                [g, other_vendor, other_product],
                product_id=self.PRODUCT,
                vendor_id=self.VENDOR,
            )
            is g
        )

    def test_highest_version_wins_within_scope(self):
        v1 = _tpl(scope="vendor", version=1, vendor_id=self.VENDOR)
        v2 = _tpl(scope="vendor", version=2, vendor_id=self.VENDOR)
        assert pick_template([v1, v2], vendor_id=self.VENDOR) is v2

    def test_no_candidates(self):
        assert pick_template([]) is None
        assert pick_template([_tpl(scope="vendor", vendor_id=uuid4())]) is None


# ---------------------------------------------------------------------------
# Migration seed templates: every placeholder they use must be a known field
# ---------------------------------------------------------------------------


def _load_migration_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "056_document_templates.py"
    )
    spec = importlib.util.spec_from_file_location("migration_056", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seeded_templates_use_only_known_merge_fields(monkeypatch):
    """Render every v1 seed template body against a full sample context — no
    placeholder may be unknown (a typo in a seed would ship blanks forever)."""
    module = _load_migration_module()

    captured = {}

    def _capture(table, rows):
        captured["rows"] = rows

    monkeypatch.setattr(module.op, "bulk_insert", _capture, raising=False)
    module._seed_default_templates()

    rows = captured["rows"]
    assert {r["kind"] for r in rows} == {
        "loan_agreement",
        "pad_agreement",
        "amortization_schedule",
        "fee_schedule",
        "terms_and_conditions",
        "privacy_policy",
        "account_statement",
    }

    context = build_scalar_context(
        loan=_loan(), patient=_patient(), product=_product(PRICING), vendor=_vendor()
    )
    tables = build_tables(loan=_loan(), product=_product(PRICING))
    for row in rows:
        result = render_template(row["body_html"], context, tables)
        assert result.unknown_fields == (), (
            f"seed template {row['kind']} has unknown fields: {result.unknown_fields}"
        )
        assert result.html.strip()


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
