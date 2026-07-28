"""Unit tests for the pending-application loan-agreement PREVIEW (QC step).

DELIBERATELY DB-FREE (the suite shares a remote DB and must not be run wholesale
by agents): everything above ``generate_agreement_preview`` is a pure function
over plain attribute objects, so the whole feature is tested with
``SimpleNamespace`` fakes — same idiom as ``tests/test_document_engine.py``.

The DB wrapper, the endpoint's role gate and the "never persists" guarantee are
asserted structurally (route graph + source inspection) rather than by standing
up Postgres.

Run JUST this file:

    source .venv/bin/activate && \
        python -m pytest tests/test_application_agreement_preview.py -p no:warnings -q
"""
import inspect
import io
import zipfile
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import application_agreement_preview as preview_mod
from app.services.application_agreement_preview import (
    AGREEMENT_MERGE_FIELDS,
    BUILTIN_QC_SKELETON_HTML,
    NOT_AVAILABLE_FMT,
    NOT_CHARGED_VALUE,
    PREVIEW_DISCLAIMER,
    build_agreement_context,
    build_preview,
    build_schedule_rows,
    resolve_terms,
)

ALL_FIELDS = {name for group in AGREEMENT_MERGE_FIELDS.values() for name in group}
NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

PRICING = {
    "schema_version": 1,
    "interest": {"annual_rate_bps": 990, "min_rate_bps": 0, "max_rate_bps": 3000},
    "default_term_months": 12,
    "fees": [
        {
            "fee_type": "origination",
            "calc": "fixed_cents",
            "amount": 2500,
            "charge_timing": "at_origination",
        },
        {
            "fee_type": "administration",
            "calc": "fixed_cents",
            "amount": 100,
            "charge_timing": "per_payment",
        },
        {
            "fee_type": "nsf",
            "calc": "fixed_cents",
            "amount": 4500,
            "charge_timing": "on_event",
            "add_on": True,
        },
    ],
}


def _application(**over):
    base = dict(
        id=uuid4(),
        status="under_review",
        patient_id=uuid4(),
        credit_product_id=uuid4(),
        vendor_id=uuid4(),
        requested_amount_cents=240_000,
        requested_term_months=None,
        requested_annual_rate_bps=None,
        preferred_payment_frequency="monthly",
        preferred_first_due_date=None,
        decision=None,
        first_name="Alex",
        middle_name=None,
        last_name="Nguyen",
        date_of_birth=date(1990, 3, 14),
        main_phone="+12505551234",
        email="alex@example.com",
        residence_street="123 Bernard Ave",
        residence_unit="4B",
        residence_city="Kelowna",
        residence_province="BC",
        residence_postal_code="V1Y 6N2",
        loan_start_date=date(2026, 8, 1),
        first_due_date=date(2026, 9, 1),
        agreement_signed_at=None,
        co_applicant_of_application_id=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _patient(**over):
    base = dict(
        legal_first_name="Alex",
        legal_last_name="Nguyen",
        dob=date(1990, 3, 14),
        email="alex@example.com",
        phone_e164="+12505551234",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _product(pricing=PRICING):
    return SimpleNamespace(
        name="Dental Full Arch", code="DFA", vertical="dental", pricing_config=pricing
    )


def _vendor(**over):
    base = dict(
        business_name="Kelowna Dental Centre Ltd.",
        dba_name="Kelowna Dental Centre",
        address_line1="2033 Gordon Dr",
        address_line2="#100",
        city="Kelowna",
        province="BC",
        postal_code="V1Y 3J2",
        industry_category_id=uuid4(),
    )
    base.update(over)
    return SimpleNamespace(**base)


def _company():
    from app.services import company_info

    return company_info.get_defaults()


def _bank_account(**over):
    base = dict(
        institution_number="003",
        transit_number="00012",
        account_mask="••••1000",
        account_number_encrypted="gAAAAA-super-secret-ciphertext",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _offer(**over):
    base = dict(
        amount_cents=240_000,
        annual_rate_bps=990,
        term_months=12,
        payment_frequency="monthly",
        start_date=date(2026, 8, 1),
        first_due_date=date(2026, 9, 1),
        status="accepted",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _complete_kwargs(**over):
    """Every input a fully-populated file would have — including a booked loan
    and a signed agreement, the only two sources that do not exist pre-loan."""
    base = dict(
        patient=_patient(),
        product=_product(),
        vendor=_vendor(),
        industry_category=SimpleNamespace(name="Dental Services"),
        co_borrower=_application(first_name="Sam", last_name="Nguyen"),
        co_borrower_patient=_patient(legal_first_name="Sam"),
        bank_account=_bank_account(),
        company=_company(),
        loan=SimpleNamespace(id=uuid4()),
        accepted_offer=_offer(),
        now=NOW,
    )
    base.update(over)
    return base


def _preview(application=None, **over):
    return build_preview(application or _application(), **_complete_kwargs(**over))


# ---------------------------------------------------------------------------
# The merge-field inventory
# ---------------------------------------------------------------------------


class TestMergeFieldInventory:
    def test_dictionary_and_context_stay_in_lockstep(self):
        """Every documented field is produced, and nothing undocumented is."""
        terms, _ = resolve_terms(_application(), _product(), _offer(), today=NOW.date())
        ctx, _, _ = build_agreement_context(
            _application(),
            terms=terms,
            **{
                k: v
                for k, v in _complete_kwargs().items()
                if k not in ("accepted_offer", "now")
            },
        )
        assert ALL_FIELDS == set(ctx)

    def test_builtin_skeleton_references_every_documented_field(self):
        """The fallback data sheet doubles as the QC checklist."""
        for name in ALL_FIELDS:
            assert "{{%s}}" % name in BUILTIN_QC_SKELETON_HTML, name

    def test_every_field_has_a_documented_source(self):
        for group in AGREEMENT_MERGE_FIELDS.values():
            for name, source in group.items():
                assert source.strip(), name


# ---------------------------------------------------------------------------
# A complete application resolves everything
# ---------------------------------------------------------------------------


class TestCompleteApplication:
    def test_every_field_resolves_with_no_gaps(self):
        """Fully-populated file — booked and signed, so even the two fields with
        no PRE-loan source resolve. Nothing is left unaccounted for."""
        result = build_preview(
            _application(agreement_signed_at=datetime(2026, 8, 1, tzinfo=timezone.utc)),
            **_complete_kwargs(),
        )
        assert result.missing_fields == ()
        assert "[NOT AVAILABLE" not in result.html
        assert ALL_FIELDS <= set(result.merge_data)
        assert all(result.merge_data[f].strip() for f in ALL_FIELDS)

    def test_headline_figures(self):
        result = _preview()
        ctx = result.merge_data
        assert ctx["LoanAmount"] == "$2,400.00"
        assert ctx["InterestRate"] == "9.90%"
        assert ctx["LoanTerm"] == "12"
        assert ctx["RepaymentPeriod"] == "Monthly"
        assert ctx["NumberOfInstallments"] == "12"
        assert ctx["FirstInstallmentDate"] == "2026-09-01"
        assert ctx["LastPaymentDate"] == "2027-08-01"
        assert ctx["Vendor"] == "Kelowna Dental Centre"
        assert ctx["VendorIndustryCategory"] == "Dental Services"
        assert ctx["FullName"] == "Alex Nguyen"
        assert ctx["BorrowerAddress"] == "123 Bernard Ave Unit 4B, Kelowna, BC, V1Y 6N2"
        assert ctx["CompanyName"] == "PaySpyre Financial Inc."

    def test_totals_tie_out(self):
        """Total of payments == principal + interest + fees, exactly."""
        t = _preview().terms
        assert (
            t.total_of_payments_cents
            == t.principal_cents + t.total_interest_cents + t.total_fees_cents
        )
        assert t.finance_charge_cents == t.total_interest_cents + t.total_fees_cents
        # $1/payment x 12 + $25 origination.
        assert t.total_fees_cents == 100 * 12 + 2500

    def test_first_installment_carries_the_origination_fee(self):
        t = _preview().terms
        rows = t.schedule
        assert rows[0]["fees_cents"] == 100 + 2500
        assert rows[1]["fees_cents"] == 100
        # The regular installment excludes the one-off origination bump.
        assert t.regular_installment_cents == rows[0]["total_cents"] - 2500

    def test_schedule_balance_amortizes_to_zero(self):
        rows = _preview().terms.schedule
        assert rows[-1]["remaining_principal_cents"] == 0
        balances = [r["remaining_principal_cents"] for r in rows]
        assert balances == sorted(balances, reverse=True)

    def test_apr_exceeds_the_contract_rate_when_fees_are_charged(self):
        t = _preview().terms
        assert t.apr_bps > t.annual_rate_bps


# ---------------------------------------------------------------------------
# The QC contract: gaps are LOUD, never blank
# ---------------------------------------------------------------------------


class TestMissingFieldsAreExplicit:
    def _sparse(self):
        application = _application(
            first_name=None,
            last_name=None,
            date_of_birth=None,
            main_phone=None,
            email=None,
            residence_street=None,
            residence_city=None,
            residence_province=None,
            residence_postal_code=None,
        )
        return build_preview(
            application,
            **_complete_kwargs(
                patient=SimpleNamespace(
                    legal_first_name=None,
                    legal_last_name=None,
                    dob=None,
                    email=None,
                    phone_e164=None,
                ),
                loan=None,
                bank_account=None,
                industry_category=None,
            ),
        )

    def test_unresolved_fields_render_a_loud_marker_not_a_blank(self):
        result = self._sparse()
        for name in ("FullName", "BorrowerDateOfBirth", "ContactEmail", "LoanId"):
            assert result.merge_data[name] == NOT_AVAILABLE_FMT.format(field=name)
            assert NOT_AVAILABLE_FMT.format(field=name) in result.html

    def test_every_marker_is_reported_in_missing_fields(self):
        result = self._sparse()
        reported = {n.field for n in result.missing_fields}
        markered = {
            k
            for k, v in result.merge_data.items()
            if v == NOT_AVAILABLE_FMT.format(field=k)
        }
        assert reported == markered
        assert reported  # the sparse file really is missing things

    def test_missing_notes_explain_source_and_reason(self):
        by_field = {n.field: n for n in self._sparse().missing_fields}
        assert "no loan exists yet" in by_field["LoanId"].reason.lower()
        assert "activation" in by_field["LoanId"].reason.lower()
        assert "no default bank account" in by_field["BorrowerBankNumber"].reason.lower()
        assert "industry category" in by_field["VendorIndustryCategory"].reason.lower()
        # The source hint always points at where the value should come from.
        assert "patient" in by_field["FullName"].source.lower()

    def test_pending_application_flags_exactly_the_two_pre_loan_gaps(self):
        """A complete but UNBOOKED, UNSIGNED file: LoanId + ContractDate only."""
        result = _preview(loan=None)
        assert {n.field for n in result.missing_fields} == {"LoanId", "ContractDate"}
        assert "signs" in {n.field: n for n in result.missing_fields}[
            "ContractDate"
        ].reason.lower()

    def test_not_applicable_is_separated_from_missing(self):
        """No co-borrower and an uncharged fee are answers, not gaps."""
        result = _preview(co_borrower=None, co_borrower_patient=None)
        missing = {n.field for n in result.missing_fields}
        na = {n.field for n in result.not_applicable_fields}
        assert "CoApplicantFullName" in na
        assert "CoApplicantFullName" not in missing
        assert result.merge_data["CoApplicantFullName"] == "N/A"
        # Late fee is disabled by Canada policy -> "Not charged", not a gap.
        assert "LateFeeFull" in na
        assert result.merge_data["LateFeeFull"] == NOT_CHARGED_VALUE
        assert "LateFeeFull" not in missing

    def test_co_borrower_resolves_when_one_is_linked(self):
        result = _preview()
        na = {n.field for n in result.not_applicable_fields}
        assert "CoApplicantFullName" not in na
        assert result.merge_data["CoApplicantFullName"] == "Sam Nguyen"

    def test_configured_fees_render_their_amounts(self):
        ctx = _preview().merge_data
        assert ctx["OriginationFeeFull"] == "$25.00"
        assert ctx["AdministrationFeeFull"] == "$1.00"
        assert ctx["NSFFull"] == "$45.00"
        assert ctx["RepaymentFeeRate"] == NOT_CHARGED_VALUE

    def test_optional_unit_number_is_not_screamed_about(self):
        result = build_preview(
            _application(residence_unit=None), **_complete_kwargs()
        )
        assert "BorrowerAddress_Appartment" not in {
            n.field for n in result.missing_fields
        }
        assert result.merge_data["BorrowerAddress_Appartment"] == ""


# ---------------------------------------------------------------------------
# The preview tracks the CURRENT terms
# ---------------------------------------------------------------------------


class TestPreviewFollowsTheTerms:
    def test_changing_the_amount_changes_the_preview(self):
        before = _preview(accepted_offer=_offer(amount_cents=240_000))
        after = _preview(accepted_offer=_offer(amount_cents=500_000))
        assert before.merge_data["LoanAmount"] == "$2,400.00"
        assert after.merge_data["LoanAmount"] == "$5,000.00"
        assert before.html != after.html
        assert (
            after.terms.total_of_payments_cents > before.terms.total_of_payments_cents
        )

    def test_changing_the_term_changes_the_schedule(self):
        before = _preview(accepted_offer=_offer(term_months=12))
        after = _preview(accepted_offer=_offer(term_months=24))
        assert before.terms.installment_count == 12
        assert after.terms.installment_count == 24
        assert after.merge_data["LastPaymentDate"] != before.merge_data["LastPaymentDate"]

    def test_changing_the_rate_changes_the_interest(self):
        cheap = _preview(accepted_offer=_offer(annual_rate_bps=500))
        dear = _preview(accepted_offer=_offer(annual_rate_bps=2500))
        assert dear.terms.total_interest_cents > cheap.terms.total_interest_cents
        assert dear.merge_data["InterestRate"] == "25.00%"

    def test_accepted_offer_beats_decision_and_request(self):
        application = _application(
            requested_amount_cents=100_000,
            decision={"amount_cents": 200_000, "term_months": 6, "apr_bps": 1500},
        )
        with_offer = build_preview(
            application, **_complete_kwargs(accepted_offer=_offer(amount_cents=240_000))
        )
        assert with_offer.terms.terms_source == "accepted_offer"
        assert with_offer.terms.principal_cents == 240_000

    def test_decision_beats_the_requested_amount(self):
        application = _application(
            requested_amount_cents=100_000,
            decision={"amount_cents": 200_000, "term_months": 6, "apr_bps": 1500},
        )
        result = build_preview(application, **_complete_kwargs(accepted_offer=None))
        assert result.terms.terms_source == "decision"
        assert result.terms.principal_cents == 200_000
        assert result.terms.term_months == 6
        assert result.terms.annual_rate_bps == 1500

    def test_falls_back_to_the_requested_amount_and_product_config(self):
        application = _application(decision=None)
        result = build_preview(application, **_complete_kwargs(accepted_offer=None))
        assert result.terms.principal_cents == 240_000
        assert result.terms.annual_rate_bps == 990  # product interest config
        assert result.terms.term_months == 12  # product default_term_months


# ---------------------------------------------------------------------------
# Preview labelling + non-persistence
# ---------------------------------------------------------------------------


class TestPreviewLabelling:
    def test_disclaimer_is_rendered_into_the_html(self):
        result = _preview()
        assert PREVIEW_DISCLAIMER in result.html.replace("&#x27;", "'")
        assert "NOT AN EXECUTED DOCUMENT" in result.html
        assert result.html.startswith(preview_mod.PREVIEW_BANNER_HTML)

    def test_title_is_prefixed(self):
        assert _preview().title.startswith("PREVIEW — ")

    def test_result_names_the_application_it_rendered(self):
        application = _application()
        result = build_preview(application, **_complete_kwargs())
        assert result.application_id == application.id
        assert result.application_status == "under_review"

    def test_service_never_writes_anything(self):
        """Structural guarantee: the preview module performs no DB mutation."""
        source = inspect.getsource(preview_mod)
        for forbidden in ("db.add(", "db.commit(", "db.flush(", "db.delete("):
            assert forbidden not in source, forbidden
        assert "PlatformLoanDocument" not in source

    def test_regenerating_reflects_new_state_rather_than_a_cache(self):
        application = _application()
        first = build_preview(application, **_complete_kwargs())
        application.first_name = "Alexandra"
        second = build_preview(application, **_complete_kwargs())
        assert first.merge_data["FullName"] == "Alex Nguyen"
        assert second.merge_data["FullName"] == "Alexandra Nguyen"


# ---------------------------------------------------------------------------
# Template sourcing
# ---------------------------------------------------------------------------


class TestTemplateSourcing:
    def test_without_a_template_the_builtin_skeleton_is_used_and_flagged(self):
        result = _preview()
        assert result.template_source == "builtin_skeleton"
        assert result.template_id is None
        assert any("No active loan_agreement template" in w for w in result.warnings)

    def test_a_db_template_is_used_and_identified(self):
        template = SimpleNamespace(
            id=uuid4(),
            version=3,
            title="PaySpyre Loan Agreement",
            body_html="<p>Borrower: {{FullName}} — Principal {{LoanAmount}}</p>",
        )
        result = _preview(template=template)
        assert result.template_source == "db_template"
        assert result.template_version == 3
        assert result.title == "PREVIEW — PaySpyre Loan Agreement"
        assert "Alex Nguyen" in result.html
        assert "$2,400.00" in result.html

    def test_unknown_placeholders_are_reported_not_silently_dropped(self):
        template = SimpleNamespace(
            id=uuid4(), version=1, title="T", body_html="<p>{{NopeNotAField}}</p>"
        )
        result = _preview(template=template)
        assert "NopeNotAField" in result.unknown_fields
        assert any("cannot fill" in w for w in result.warnings)

    def test_the_shipped_default_template_renders_from_a_pending_application(self):
        """Migration 040 seeds a ``loan_agreement`` template written in the
        LOAN-level vocabulary. The preview must render THAT template too, or the
        QC step is blank out of the box."""
        shipped = SimpleNamespace(
            id=uuid4(),
            version=1,
            title="Loan Agreement — Default Template",
            body_html=(
                "<p>{{CompanyName}} and {{BorrowerFullName}}</p>"
                "<ul><li>{{LoanId}}</li><li>{{PrincipalAmount}}</li>"
                "<li>{{AnnualInterestRate}}</li><li>{{TermMonths}}</li>"
                "<li>{{InstallmentCount}}</li><li>{{FirstDueDate}}</li>"
                "<li>{{MaturityDate}}</li><li>{{TotalOfPayments}}</li>"
                "<li>{{ProductName}}</li>"
                "<li>{{VendorName}}, {{VendorCity}}, {{VendorProvince}}</li></ul>"
                "{{Table:AmortizationSchedule}}{{Table:FeeSchedule}}"
            ),
        )
        result = _preview(template=shipped)
        assert result.unknown_fields == ()
        assert "Alex Nguyen" in result.html
        assert "$2,400.00" in result.html  # PrincipalAmount
        assert "9.90%" in result.html  # AnnualInterestRate
        assert "Dental Full Arch" in result.html  # ProductName
        assert "2026-09-01" in result.html  # FirstDueDate
        assert "Origination" in result.html  # the fee table rendered

    def test_loan_level_aliases_agree_with_the_agreement_fields(self):
        ctx = _preview().merge_data
        assert ctx["PrincipalAmount"] == ctx["LoanAmount"]
        assert ctx["AnnualInterestRate"] == ctx["InterestRate"]
        assert ctx["TermMonths"] == ctx["LoanTerm"]
        assert ctx["BorrowerFullName"] == ctx["FullName"]
        assert ctx["FirstDueDate"] == ctx["FirstInstallmentDate"]
        assert ctx["MaturityDate"] == ctx["LastPaymentDate"]
        assert ctx["TotalOfPayments"] == ctx["TotalAmountToPay"]

    def test_alias_gaps_are_flagged_only_when_the_template_uses_them(self):
        """DisbursedDate is empty by design pre-loan — flag it only if used."""
        unused = SimpleNamespace(
            id=uuid4(), version=1, title="T", body_html="<p>{{ProductName}}</p>"
        )
        used = SimpleNamespace(
            id=uuid4(), version=1, title="T", body_html="<p>{{DisbursedDate}}</p>"
        )
        assert "DisbursedDate" not in {
            n.field for n in _preview(template=unused, loan=None).missing_fields
        }
        flagged = _preview(template=used, loan=None)
        assert "DisbursedDate" in {n.field for n in flagged.missing_fields}
        assert "[NOT AVAILABLE: DisbursedDate]" in flagged.html

    def test_schedule_table_and_rows_tokens_both_render(self):
        rows = SimpleNamespace(
            id=uuid4(), version=1, title="T",
            body_html="<table><tr><th>#</th></tr>{{Rows:Schedule}}</table>",
        )
        whole = SimpleNamespace(
            id=uuid4(), version=1, title="T", body_html="{{Table:Schedule}}"
        )
        rows_html = _preview(template=rows).html
        whole_html = _preview(template=whole).html
        assert rows_html.count("<tr>") == 12 + 1  # 12 installments + the header row
        assert "Remaining Principal Balance" in whole_html
        assert "2026-09-01" in whole_html
        assert "<thead>" not in rows_html  # Rows: emits only <tr>s


# ---------------------------------------------------------------------------
# Security: the preview must not leak the full bank account number
# ---------------------------------------------------------------------------


class TestPadPrivacy:
    def test_only_the_masked_account_is_rendered(self):
        result = _preview()
        assert result.merge_data["BorrowerBankAccount"] == "••••1000"
        assert "super-secret-ciphertext" not in result.html
        assert result.merge_data["BorrowerBankNumber"] == "003"
        assert result.merge_data["BorrowerBankRoutingNumber"] == "00012"

    def test_leading_zeros_survive(self):
        """Institution 003 must never render as 3."""
        assert _preview().merge_data["BorrowerBankNumber"] == "003"


# ---------------------------------------------------------------------------
# Warnings — the other half of the QC signal
# ---------------------------------------------------------------------------


class TestWarnings:
    def test_defaulted_first_due_date_is_warned_about(self):
        application = _application(first_due_date=None, preferred_first_due_date=None)
        result = build_preview(
            application, **_complete_kwargs(accepted_offer=_offer(first_due_date=None))
        )
        assert any("first due date" in w.lower() for w in result.warnings)

    def test_non_monthly_frequency_is_warned_about(self):
        result = _preview(accepted_offer=_offer(payment_frequency="bi_weekly"))
        assert any("monthly" in w.lower() for w in result.warnings)

    def test_criminal_rate_is_warned_about(self):
        result = _preview(accepted_offer=_offer(annual_rate_bps=3500))
        assert any("s.347" in w for w in result.warnings)

    def test_missing_rate_and_term_fall_back_with_a_warning(self):
        application = _application(decision=None, requested_term_months=None)
        result = build_preview(
            application,
            **_complete_kwargs(
                accepted_offer=None, product=_product({"schema_version": 1})
            ),
        )
        assert any("Interest rate is not set" in w for w in result.warnings)
        assert any("Term is not set" in w for w in result.warnings)

    def test_non_positive_principal_degrades_instead_of_crashing(self):
        application = _application(requested_amount_cents=0, decision=None)
        result = build_preview(application, **_complete_kwargs(accepted_offer=None))
        assert any("no positive principal" in w for w in result.warnings)
        assert result.terms.schedule == ()
        assert result.merge_data["LoanAmount"] == NOT_AVAILABLE_FMT.format(
            field="LoanAmount"
        )


# ---------------------------------------------------------------------------
# Pure schedule builder
# ---------------------------------------------------------------------------


class TestBuildScheduleRows:
    def _rows(self):
        return [
            SimpleNamespace(
                installment_number=2,
                due_date=date(2026, 10, 1),
                principal_cents=1000,
                interest_cents=10,
                total_cents=1010,
            ),
            SimpleNamespace(
                installment_number=1,
                due_date=date(2026, 9, 1),
                principal_cents=1000,
                interest_cents=20,
                total_cents=1020,
            ),
        ]

    def test_sorted_and_fee_adjusted(self):
        out = build_schedule_rows(
            self._rows(),
            principal_cents=2000,
            per_payment_fee_cents=100,
            origination_fee_cents=500,
        )
        assert [r["installment_number"] for r in out] == [1, 2]
        assert out[0]["fees_cents"] == 600
        assert out[0]["total_cents"] == 1020 + 600
        assert out[1]["fees_cents"] == 100
        assert [r["remaining_principal_cents"] for r in out] == [1000, 0]

    def test_no_fees_configured(self):
        out = build_schedule_rows(self._rows(), principal_cents=2000)
        assert all(r["fees_cents"] == 0 for r in out)
        assert out[0]["total_cents"] == 1020


# ---------------------------------------------------------------------------
# Endpoint: contract + auth/role gate
# ---------------------------------------------------------------------------


def _preview_route():
    from app.api.v1.endpoints import admin_application_documents

    for route in admin_application_documents.router.routes:
        if route.path.endswith("/documents/agreement-preview"):
            return route
    raise AssertionError("agreement-preview route is not registered")


def _dependency_calls(dependant):
    for dep in dependant.dependencies:
        if dep.call is not None:
            yield dep.call
        yield from _dependency_calls(dep)


class TestEndpoint:
    def test_route_is_a_read_only_get(self):
        route = _preview_route()
        assert route.methods == {"GET"}

    def test_route_is_role_gated_to_admin_and_staff(self):
        """The router-level gate really applies to THIS route's dependant."""
        checkers = [
            fn
            for fn in _dependency_calls(_preview_route().dependant)
            if getattr(fn, "__name__", "") == "role_checker"
        ]
        assert checkers, "no require_roles gate on the agreement-preview route"
        allowed = set()
        for fn in checkers:
            for cell in fn.__closure__ or ():
                if isinstance(cell.cell_contents, tuple):
                    allowed.update(cell.cell_contents)
        assert allowed == {"admin", "staff"}

    def test_role_gate_rejects_a_non_staff_user(self):
        from fastapi import HTTPException

        checker = next(
            fn
            for fn in _dependency_calls(_preview_route().dependant)
            if getattr(fn, "__name__", "") == "role_checker"
        )
        borrower = SimpleNamespace(
            roles=[SimpleNamespace(role=SimpleNamespace(name="clinic"))]
        )
        with pytest.raises(HTTPException) as exc:
            checker(current_user=borrower)
        assert exc.value.status_code == 403

        staff = SimpleNamespace(
            roles=[SimpleNamespace(role=SimpleNamespace(name="staff"))]
        )
        assert checker(current_user=staff) is staff

    def test_response_model_pins_is_preview_true(self):
        from app.api.v1.endpoints.admin_application_documents import (
            AgreementPreviewResponse,
        )

        fields = AgreementPreviewResponse.model_fields
        assert fields["is_preview"].default is True
        for required in (
            "missing_fields",
            "not_applicable_fields",
            "terms",
            "disclaimer",
            "html",
        ):
            assert required in fields

    def test_endpoint_delegates_and_raises_no_status_conflict(self):
        """A read-only QC preview is useful at every stage — no status gate."""
        from app.api.v1.endpoints import admin_application_documents

        source = inspect.getsource(admin_application_documents.agreement_preview)
        assert "generate_agreement_preview" in source
        # No status-based refusal (the e-sign routes have those; this one must not).
        assert "HTTP_409_CONFLICT" not in source
        assert "raise HTTPException" not in source


# ---------------------------------------------------------------------------
# The out-of-band .docx loader (proprietary text stays out of git)
# ---------------------------------------------------------------------------


def _synthetic_docx(paragraphs, table_rows=None):
    """Build a minimal .docx in memory. NOT the owner's document — a fixture."""
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs
    )
    if table_rows:
        rows = "".join(
            "<w:tr>"
            + "".join(
                f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row
            )
            + "</w:tr>"
            for row in table_rows
        )
        body += f"<w:tbl>{rows}</w:tbl>"
    xml = f"<w:document {ns}><w:body>{body}</w:body></w:document>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    buf.seek(0)
    return buf


class TestDocxLoader:
    def _convert(self, docx):
        import importlib.util
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "seed_loan_agreement_template.py"
        )
        spec = importlib.util.spec_from_file_location("seed_agreement", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as fh:
            fh.write(docx.read())
            tmp = fh.name
        return module.convert_docx_to_template_html(tmp)

    def test_word_merge_fields_become_engine_placeholders(self):
        html = self._convert(
            _synthetic_docx(["Name of Borrower: «FullName»", "Amount: «LoanAmount»"])
        )
        assert "{{FullName}}" in html
        assert "{{LoanAmount}}" in html
        assert "«" not in html

    def test_repeat_block_becomes_the_rows_token(self):
        html = self._convert(
            _synthetic_docx(
                [],
                table_rows=[
                    ["Installment Number", "Due Date"],
                    ["«TableStart:Schedule»«InstallmentNumber»", "«DueDate»"],
                    ["Totals", "«TotalAmountToPay»"],
                ],
            )
        )
        assert "{{Rows:Schedule}}" in html
        assert "InstallmentNumber" not in html  # the repeat row is replaced wholesale
        assert "Installment Number" in html  # header row survives
        assert "{{TotalAmountToPay}}" in html  # totals row survives

    def test_literal_text_is_escaped(self):
        html = self._convert(_synthetic_docx(["Terms &amp; conditions &lt;here&gt;"]))
        assert "<here>" not in html

    def test_converted_template_renders_end_to_end(self):
        html = self._convert(
            _synthetic_docx(["Borrower «FullName» owes «TotalAmountToPay»"])
        )
        result = _preview(
            template=SimpleNamespace(id=uuid4(), version=1, title="T", body_html=html)
        )
        assert "Alex Nguyen" in result.html
        assert result.unknown_fields == ()

    def test_loader_takes_its_content_from_a_local_path_only(self):
        """The script is a LOADER: the agreement text lives on the operator's
        machine and in the DB, never in this (public) repository."""
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "seed_loan_agreement_template.py"
        ).read_text()
        assert "--docx" in source
        assert "convert_docx_to_template_html" in source
        # No hard-coded document path and no embedded body_html literal.
        assert "/Users/" not in source
        assert "Downloads" not in source
        assert 'body_html="' not in source
        assert "body_html=body_html" in source
