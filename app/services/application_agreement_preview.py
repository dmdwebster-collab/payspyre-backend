"""Loan-agreement PREVIEW for a PENDING application (quality-control step).

Dave's requirement, verbatim:

    "Within a Pending Application the system should generate a preview of the
    loan agreement to verify accuracy before the document is sent to the
    borrower. Note: this is a preview only of what would be sent, it does not
    represent a final executed document and would change if any of the terms in
    the application change. This will become an important quality-control step
    within the workflow."

WHAT THIS IS
------------
A *read-only, never-persisted* render of the loan agreement against an
application that has **no loan yet**. It sits alongside the Wave-1 pre-loan
e-sign surface (:mod:`app.services.application_agreement`) and is the QC gate
that runs BEFORE ``send-for-signature``.

WHAT THIS IS NOT
----------------
* NOT an executed document. Nothing is written to
  ``platform_loan_documents``; there is no snapshot, no version, no e-sign ref.
  The rendered HTML carries a visible preview banner (:data:`PREVIEW_BANNER_HTML`)
  so a printed/screenshotted copy can never be mistaken for the real thing.
* NOT cached. Every call re-resolves the terms from the application, so the
  preview always reflects the CURRENT terms — Dave: "would change if any of the
  terms in the application change".
* NOT a change to the booking-time documents. ``document_engine`` and the
  loan-level ``generate_booking_documents`` path are untouched; this module only
  *reads* the shared renderer.

THE QUALITY-CONTROL CONTRACT (the point of the whole feature)
-------------------------------------------------------------
A blank cell in an agreement preview is worse than useless — it hides the gap
it is supposed to expose. So this module NEVER silently blanks a field. Every
merge field lands in exactly one of three buckets, and the endpoint returns all
three alongside the HTML:

``resolved``
    A real value from the application graph.
``missing``  → rendered as ``[NOT AVAILABLE: <Field>]``
    The agreement needs it and the application cannot supply it yet (e.g.
    ``ContractDate`` on a file nobody has signed). Loud, visible, un-missable
    in the rendered output. ``LoanId`` used to be the headline example; since
    2026-07-28 it always resolves — see :func:`_loan_id_value`.
``not_applicable`` → rendered as ``N/A`` / ``Not charged``
    The field legitimately does not apply to this file (no co-borrower on a
    solo application; a fee the product does not charge). Reported so the
    reviewer can confirm the judgement, but not screamed about.

TEMPLATE SOURCING (and why the real agreement is NOT in this repo)
-------------------------------------------------------------------
The backend repository is PUBLIC. Dave's real default loan agreement (a .docx he
supplies) is confidential and proprietary, so its text is NOT committed anywhere
in this tree. Instead:

* the real content is loaded OUT-OF-BAND into ``platform_document_templates``
  (kind ``loan_agreement``) by ``scripts/seed_loan_agreement_template.py``,
  which reads the .docx from a local path and converts its Word merge fields
  (``«Field»``) into the engine's ``{{Field}}`` syntax. Nothing it reads is
  written back to git.
* until that row exists, the preview falls back to
  :data:`BUILTIN_QC_SKELETON_HTML` — a PaySpyre-authored *data sheet*, not a
  contract: section headings and field labels only, zero legal clause text. The
  response says which one was used via ``template_source``, so an operator can
  never mistake the skeleton for the agreement.

PURITY
------
Everything above :func:`build_preview` is a pure function over plain attribute
objects (DB-free testable, same idiom as ``document_engine``); the single DB
wrapper at the bottom owns the queries.
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field as _dc_field
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterable, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.platform.credit_application import PlatformCreditApplication
from app.services import document_engine
from app.services.document_engine import _date_str, _money, _percent_bps, _s

logger = get_logger(__name__)

__all__ = [
    "AGREEMENT_MERGE_FIELDS",
    "AGREEMENT_SCHEDULE_COLUMNS",
    "BUILTIN_QC_SKELETON_HTML",
    "FieldNote",
    "NOT_AVAILABLE_FMT",
    "PREVIEW_BANNER_HTML",
    "PREVIEW_DISCLAIMER",
    "PreviewResult",
    "TermsSnapshot",
    "build_agreement_context",
    "build_preview",
    "build_schedule_rows",
    "generate_agreement_preview",
    "proposed_loan",
    "render_agreement_body",
    "resolve_terms",
]


# ---------------------------------------------------------------------------
# Preview labelling — the disclaimer is part of the RENDERED OUTPUT, not just
# the JSON, so a printout of the preview is self-identifying.
# ---------------------------------------------------------------------------

PREVIEW_DISCLAIMER = (
    "PREVIEW ONLY — NOT AN EXECUTED DOCUMENT. This is a preview of what would "
    "be sent to the borrower, generated from the application's current terms "
    "for quality-control review. It is not a binding agreement, it has not been "
    "signed, and it will change if any of the terms on the application change."
)

PREVIEW_BANNER_HTML = (
    '<div class="agreement-preview-banner" role="note" '
    'style="border:2px solid #b00020;background:#fff4f4;color:#b00020;'
    'padding:12px 16px;margin:0 0 16px;font-weight:600;">'
    f"{_html.escape(PREVIEW_DISCLAIMER)}"
    "</div>"
)

#: How an unresolvable-but-required merge field renders. Deliberately loud.
NOT_AVAILABLE_FMT = "[NOT AVAILABLE: {field}]"

#: How a field that legitimately does not apply to this file renders.
NOT_APPLICABLE_VALUE = "N/A"

#: How a fee the product does not charge renders (a fee row must not read as
#: "we forgot the amount" when the honest answer is "we don't charge this").
NOT_CHARGED_VALUE = "Not charged"


# ---------------------------------------------------------------------------
# The merge-field inventory extracted from Dave's agreement
# ---------------------------------------------------------------------------

#: Every merge field the real agreement uses, grouped, with the APPLICATION-SIDE
#: source it is resolved from. Extracted from the owner's .docx (Word merge
#: fields, ``«Field»``); the names are HIS, not ours, so a template imported
#: from the .docx merges without a rename pass.
#:
#: A test asserts this dictionary and :func:`build_agreement_context` stay in
#: lockstep — extend BOTH when adding a field.
AGREEMENT_MERGE_FIELDS: dict[str, dict[str, str]] = {
    "Lender": {
        "CompanyName": "Lender legal name (company info; default PaySpyre Financial Inc.).",
        "CompanyAddress": "Lender head-office address (company info contacts).",
        "SupportEmail": "Lender support email (company info contacts).",
        "CompanyPhone": "Lender phone (company info contacts).",
    },
    "Vendor": {
        "Vendor": "Vendor DBA name, falling back to legal business name (vendors).",
        "VendorAddress": "Vendor full address (vendors).",
        "VendorProvince": "Vendor province — governing law (vendors.province).",
        "VendorIndustryCategory": (
            "Goods & Services description (vendors.industry_category_id -> "
            "platform_industry_categories.name)."
        ),
    },
    "Borrower": {
        "FullName": "Borrower legal name (application first/middle/last, else patient).",
        "BorrowerDateOfBirth": "Borrower DOB (application.date_of_birth, else patient.dob).",
        "BorrowerAddress": "Borrower full address, one line (application residence_* fields).",
        "BorrowerPhone": "Borrower phone (application.main_phone, else patient.phone_e164).",
        "ContactEmail": "Borrower email (application.email, else patient.email).",
        "BorrowerAddress_Street": "PAD payor street (application.residence_street).",
        "BorrowerAddress_Appartment": "PAD payor unit (application.residence_unit).",
        "BorrowerAddress_City": "PAD payor city (application.residence_city).",
        "BorrowerAddress_Province": "PAD payor province (application.residence_province).",
        "BorrowerAddress_ZipCode": "PAD payor postal code (application.residence_postal_code).",
    },
    "CoBorrower": {
        "CoApplicantFullName": "Co-borrower legal name (linked co-borrower application).",
        "CoApplicantDateOfBirth": "Co-borrower DOB (linked co-borrower application).",
        "CoApplicantAddress": "Co-borrower full address (linked co-borrower application).",
        "CoApplicantPhone": "Co-borrower phone (linked co-borrower application).",
        "CoApplicantEmail": "Co-borrower email (linked co-borrower application).",
    },
    "Identity": {
        "LoanId": (
            "Account number = the APPLICATION NUMBER "
            "(application.application_number), which the booked loan inherits as "
            "loan.loan_number — so it prints before activation and still matches "
            "the live loan. Migrated loans use legacy_account_number."
        ),
        "StartDate": "Date of agreement (accepted offer start_date, else application.loan_start_date).",
        "InterestStartDate": "Interest accrual start (application.loan_start_date, else StartDate).",
        "ContractDate": (
            "Signature date (application.agreement_signed_at) — populated as "
            "soon as the borrower signs, which under the activation rework is "
            "BEFORE the loan exists. Empty only while the file is unsigned."
        ),
    },
    "Terms": {
        "LoanAmount": "Amount Financed / Principal (accepted offer, else decision, else requested).",
        "InterestRate": "Annual interest rate (accepted offer, else decision, else product config).",
        "APR": "Canadian regulatory APR incl. non-contingent fees (SOR/2001-104).",
        "LoanTerm": "Loan term in months (accepted offer, else decision, else product config).",
        "RepaymentPeriod": "Payment frequency label, e.g. Monthly (accepted offer, else product).",
        "NumberOfInstallments": "Number of scheduled installments (computed schedule).",
        "RegularInstallmentAmount": "Regular installment incl. per-payment fees (computed schedule).",
        "FirstInstallment": "First installment amount, incl. at-origination fees (computed schedule).",
        "FirstInstallmentDate": "First installment due date (computed schedule).",
        "LastInstallment": "Final installment amount (computed schedule).",
        "LastPaymentDate": "Final installment due date (computed schedule).",
        "TotalAmountToPay": "Total of Payments = principal + interest + fees (computed schedule).",
        "TotalInterest": "Total scheduled interest over the term (computed schedule).",
        "TotalFee": "Total non-contingent fees over the term (product pricing config).",
        "FinanceCharge": "Cost of Borrowing = total interest + total fees.",
    },
    "Fees": {
        "OriginationFeeFull": "Origination fee (product pricing config; 'Not charged' if absent/disabled).",
        "AdministrationFeeFull": "Administration fee (product pricing config).",
        "RepaymentFeeRate": "Repayment fee (product pricing config).",
        "LateFeeFull": "Late fee (product pricing config; disabled by Canada policy).",
        "NSFFull": "Dishonoured-payment fee (product pricing config).",
    },
    "PAD": {
        "BorrowerBankNumber": "Institution number, 3 digits (patient default bank account).",
        "BorrowerBankRoutingNumber": "Transit number, 5 digits (patient default bank account).",
        "BorrowerBankAccount": (
            "Account number — MASKED in a preview by design; the full number is "
            "never rendered into a QC document."
        ),
    },
}

#: The repeating amortization block. In the .docx this is Word's
#: ``«TableStart:Schedule»…«TableEnd:Schedule»`` row repetition; the seeder
#: rewrites that region to the engine token ``{{Table:Schedule}}`` and this
#: module renders the whole table. Column order matches the .docx exactly.
AGREEMENT_SCHEDULE_TABLE = "Schedule"
AGREEMENT_SCHEDULE_COLUMNS = (
    "Installment Number",
    "Due Date",
    "Payment Amount",
    "Interest Paid",
    "Principal Paid",
    "Fees Paid",
    "Remaining Principal Balance",
)

#: Fields that are genuinely unavailable before the loan exists. Kept explicit
#: so the endpoint can explain WHY rather than just flagging a hole.
#:
#: ``LoanId`` is NO LONGER one of them (2026-07-28): the application number is
#: minted with the application, so the Loan ID always resolves. ``ContractDate``
#: stays, but its reason is now accurate — it is empty because the file is
#: UNSIGNED, not because no loan exists; signing populates it pre-activation.
_NO_PRELOAN_SOURCE_REASONS = {
    "ContractDate": (
        "Stamped when the borrower signs — this file is not signed yet. "
        "(Signing populates it before activation; a loan is not required.)"
    ),
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldNote:
    """One merge field that did not resolve to a real value."""

    field: str
    #: Where the value should have come from (the QC hint).
    source: str
    #: Why it is not there.
    reason: str


@dataclass(frozen=True)
class TermsSnapshot:
    """The terms the preview merged from — returned so the reviewer can tie the
    rendered figures back to their inputs without re-deriving them."""

    principal_cents: Optional[int] = None
    annual_rate_bps: Optional[int] = None
    apr_bps: Optional[int] = None
    term_months: Optional[int] = None
    payment_frequency: str = "monthly"
    #: Where principal/rate/term came from: 'accepted_offer' | 'decision' |
    #: 'application_request' | 'product_default'.
    terms_source: str = "unknown"
    first_due_date: Optional[date] = None
    start_date: Optional[date] = None
    installment_count: int = 0
    regular_installment_cents: Optional[int] = None
    total_of_payments_cents: Optional[int] = None
    total_interest_cents: Optional[int] = None
    total_fees_cents: Optional[int] = None
    finance_charge_cents: Optional[int] = None
    per_payment_fee_cents: int = 0
    origination_fee_cents: int = 0
    #: Amortization rows, already fee-adjusted (see :func:`build_schedule_rows`).
    schedule: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "principal_cents": self.principal_cents,
            "annual_rate_bps": self.annual_rate_bps,
            "apr_bps": self.apr_bps,
            "term_months": self.term_months,
            "payment_frequency": self.payment_frequency,
            "terms_source": self.terms_source,
            "first_due_date": self.first_due_date,
            "start_date": self.start_date,
            "installment_count": self.installment_count,
            "regular_installment_cents": self.regular_installment_cents,
            "total_of_payments_cents": self.total_of_payments_cents,
            "total_interest_cents": self.total_interest_cents,
            "total_fees_cents": self.total_fees_cents,
            "finance_charge_cents": self.finance_charge_cents,
            "per_payment_fee_cents": self.per_payment_fee_cents,
            "origination_fee_cents": self.origination_fee_cents,
        }


@dataclass(frozen=True)
class PreviewResult:
    """A rendered preview plus everything the QC step needs to judge it."""

    html: str
    title: str
    #: The application the agreement was actually rendered FOR. Differs from the
    #: requested id when a co-borrower file was passed: the agreement is written
    #: on the primary file, so the preview resolves through to it.
    application_id: Optional[UUID] = None
    application_status: str = ""
    template_source: str = "builtin_skeleton"  # or 'db_template'
    template_id: Optional[UUID] = None
    template_version: Optional[int] = None
    merge_data: dict[str, str] = _dc_field(default_factory=dict)
    missing_fields: tuple[FieldNote, ...] = ()
    not_applicable_fields: tuple[FieldNote, ...] = ()
    #: Placeholders present in the template that this engine does not know how
    #: to fill at all (a template typo, or a field Dave added since).
    unknown_fields: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    terms: TermsSnapshot = _dc_field(default_factory=TermsSnapshot)
    generated_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------

_FREQUENCY_LABELS = {
    "weekly": "Weekly",
    "bi_weekly": "Bi-Weekly",
    "semi_monthly": "Semi-Monthly",
    "monthly": "Monthly",
}


def _join_nonempty(parts: Iterable[Any], sep: str = " ") -> str:
    return sep.join(p for p in (_s(x).strip() for x in parts) if p)


def _loan_id_value(application: Any, loan: Any = None) -> Optional[str]:
    """The Loan ID to print, WITH or WITHOUT a loan row.

    Dave, 2026-07-28: *"the Application Number becomes the Loan ID. This allows
    the Loan ID to be populated on the loan agreement before activation."* Under
    the activation rework no loan exists until activation, so before this the
    agreement the borrower SIGNED rendered ``[NOT AVAILABLE: LoanId]``.

    Resolution order, and why each rung exists:

    1. ``loan.legacy_account_number`` — a migrated the legacy LMS loan is known to the
       vendor and the borrower by its legacy account number; nothing else.
    2. ``loan.loan_number`` — the booked loan's own number, which booking copied
       from the application (migration 081). Identical to rung 3 by construction.
    3. ``application.application_number`` — the pre-activation case, and the
       whole point: the number is minted with the application, so it is printable
       from the moment the file exists.
    4. ``loan.id`` — only for a loan row predating migration 081.

    Because rungs 2 and 3 are the same string, the signed agreement and the loan
    that is later activated from it always name the same identifier.
    """
    for candidate in (
        getattr(loan, "legacy_account_number", None),
        getattr(loan, "loan_number", None),
        getattr(application, "application_number", None),
        getattr(loan, "id", None),
    ):
        text = _s(candidate).strip()
        if text:
            return text
    return None


def _address_line(street, unit, city, province, postal) -> str:
    street_part = _join_nonempty([street, f"Unit {_s(unit).strip()}" if _s(unit).strip() else ""])
    return _join_nonempty([street_part, city, province, postal], sep=", ")


class _Collector:
    """Accumulates the three field buckets while the context is built.

    Keeps :func:`build_agreement_context` readable: every field goes through
    ``put`` (or ``na``), so no field can be added without landing in a bucket.
    """

    def __init__(self) -> None:
        self.ctx: dict[str, str] = {}
        self.missing: list[FieldNote] = []
        self.not_applicable: list[FieldNote] = []

    def put(self, name: str, value: Any, *, reason: Optional[str] = None) -> None:
        """Record ``value``; blank/None becomes a LOUD not-available marker."""
        text = _s(value).strip()
        if text:
            self.ctx[name] = text
            return
        self.ctx[name] = NOT_AVAILABLE_FMT.format(field=name)
        self.missing.append(
            FieldNote(
                field=name,
                source=_source_of(name),
                reason=reason
                or _NO_PRELOAN_SOURCE_REASONS.get(name)
                or "No value on the application, patient profile, product or vendor.",
            )
        )

    def na(self, name: str, reason: str, *, value: str = NOT_APPLICABLE_VALUE) -> None:
        """Record a field that legitimately does not apply to this file."""
        self.ctx[name] = value
        self.not_applicable.append(
            FieldNote(field=name, source=_source_of(name), reason=reason)
        )


def _source_of(name: str) -> str:
    for group in AGREEMENT_MERGE_FIELDS.values():
        if name in group:
            return group[name]
    return "Undocumented merge field."


# ---------------------------------------------------------------------------
# Terms resolution (pure)
# ---------------------------------------------------------------------------


def resolve_terms(
    application: Any,
    product: Any = None,
    accepted_offer: Any = None,
    loan: Any = None,
    *,
    today: Optional[date] = None,
) -> tuple[TermsSnapshot, list[str]]:
    """Derive the terms the agreement would be written on (pure).

    PRECEDENCE — the accepted offer is the borrower's actual deal, so it wins
    over everything; below that this deliberately mirrors
    ``loan_servicing._resolve_pricing`` (decision -> application request ->
    product config) so the preview shows exactly what booking would produce.

    Returns the snapshot plus human-readable warnings (defaulted dates, a
    frequency the monthly schedule generator cannot express, an APR at the
    Criminal Code cap, …) — all QC signal.
    """
    from app.schemas.pricing_config import (
        ChargeTiming,
        FeeCalc,
        PaymentFrequency,
        parse_pricing_config,
        payments_in_term,
    )
    from app.services.loan_quote import (
        CRIMINAL_RATE_CAP_BPS,
        compute_apr_bps,
        exceeds_criminal_rate,
    )
    from app.services.loan_servicing import (
        _DEFAULT_ANNUAL_RATE_BPS,
        _DEFAULT_TERM_MONTHS,
        generate_amortization_schedule,
    )
    from app.services.servicing_status import step_due_date

    today = today or date.today()
    warnings: list[str] = []
    decision = getattr(application, "decision", None) or {}
    cfg = parse_pricing_config(
        getattr(product, "pricing_config", None), context="agreement preview"
    )

    # --- principal / rate / term ------------------------------------------
    if accepted_offer is not None:
        principal_cents = getattr(accepted_offer, "amount_cents", None)
        annual_rate_bps = getattr(accepted_offer, "annual_rate_bps", None)
        term_months = getattr(accepted_offer, "term_months", None)
        frequency = _s(getattr(accepted_offer, "payment_frequency", None)) or "monthly"
        terms_source = "accepted_offer"
    else:
        principal_cents = decision.get("amount_cents")
        annual_rate_bps = decision.get("apr_bps")
        term_months = decision.get("term_months")
        terms_source = "decision" if principal_cents is not None else "application_request"
        if principal_cents is None:
            principal_cents = getattr(application, "requested_amount_cents", None)
        if annual_rate_bps is None:
            annual_rate_bps = getattr(application, "requested_annual_rate_bps", None)
        if term_months is None:
            term_months = getattr(application, "requested_term_months", None)
        frequency = (
            _s(getattr(application, "preferred_payment_frequency", None)) or "monthly"
        )

    if annual_rate_bps is None:
        annual_rate_bps = (
            cfg.interest.annual_rate_bps
            if cfg.interest is not None
            else _DEFAULT_ANNUAL_RATE_BPS
        )
        if terms_source == "application_request":
            terms_source = "product_default"
        warnings.append(
            "Interest rate is not set on an offer or decision — the preview used "
            f"the product/platform default ({annual_rate_bps / 100:.2f}%)."
        )
    if term_months is None:
        if cfg.default_term_months is not None:
            term_months = cfg.default_term_months
        elif cfg.term_options:
            term_months = cfg.term_options[0]
        elif cfg.term_min_months is not None:
            term_months = cfg.term_min_months
        else:
            term_months = _DEFAULT_TERM_MONTHS
        warnings.append(
            "Term is not set on an offer or decision — the preview used the "
            f"product/platform default ({term_months} months)."
        )

    try:
        frequency_enum = PaymentFrequency(frequency)
    except ValueError:
        warnings.append(
            f"Unrecognised payment frequency {frequency!r}; treated as monthly."
        )
        frequency_enum = PaymentFrequency.MONTHLY
        frequency = "monthly"

    # --- dates -------------------------------------------------------------
    start_date = (
        getattr(accepted_offer, "start_date", None)
        or getattr(application, "loan_start_date", None)
    )
    first_due_date = (
        getattr(accepted_offer, "first_due_date", None)
        or getattr(application, "first_due_date", None)
        or getattr(application, "preferred_first_due_date", None)
    )
    if first_due_date is None:
        # Mirrors ``create_loan_from_application``'s default so the preview
        # matches what booking would actually produce — one PERIOD from today at
        # the deal's own frequency, not always one month.
        first_due_date = step_due_date(today, 1, frequency_enum)
        warnings.append(
            "No first due date is set on the application or accepted offer — the "
            f"preview used the booking default of one month from today "
            f"({first_due_date.isoformat()})."
        )

    if principal_cents is None or int(principal_cents) <= 0:
        warnings.append(
            "The application has no positive principal, so no schedule, totals or "
            "APR could be computed."
        )
        return (
            TermsSnapshot(
                principal_cents=principal_cents,
                annual_rate_bps=int(annual_rate_bps),
                term_months=int(term_months),
                payment_frequency=frequency,
                terms_source=terms_source,
                first_due_date=first_due_date,
                start_date=start_date,
            ),
            warnings,
        )

    principal_cents = int(principal_cents)
    annual_rate_bps = int(annual_rate_bps)
    term_months = int(term_months)

    # --- fees --------------------------------------------------------------
    # Split the product's non-contingent fees into the two buckets the
    # agreement's schedule needs: a per-installment charge, and a one-off
    # at-origination charge the .docx bills on the FIRST installment.
    per_payment_fee_cents = 0
    origination_fee_cents = 0
    for fee in cfg.fees:
        if not fee.enabled or fee.charge_timing is ChargeTiming.ON_EVENT:
            continue
        amt = fee.amount_for(frequency_enum)
        value = round(principal_cents * amt / 10_000) if fee.calc is FeeCalc.RATE_BPS else amt
        if fee.charge_timing is ChargeTiming.PER_PAYMENT:
            per_payment_fee_cents += int(value)
        else:
            origination_fee_cents += int(value)

    n_payments = payments_in_term(term_months, frequency_enum)
    total_fees_cents = per_payment_fee_cents * n_payments + origination_fee_cents

    # --- schedule + totals -------------------------------------------------
    # Built at the deal's OWN frequency. Until migration 082 the booking engine
    # could only step in months, so this preview carried a warning that a
    # non-monthly deal's schedule would not match what got booked. Booking is now
    # frequency-aware, the same engine builds both, and the warning is gone
    # because the mismatch is.
    rows = generate_amortization_schedule(
        principal_cents,
        annual_rate_bps,
        term_months,
        first_due_date,
        frequency=frequency_enum,
    )
    schedule = build_schedule_rows(
        rows,
        principal_cents=principal_cents,
        per_payment_fee_cents=per_payment_fee_cents,
        origination_fee_cents=origination_fee_cents,
    )
    total_interest_cents = sum(int(r["interest_cents"]) for r in schedule)
    total_of_payments_cents = principal_cents + total_interest_cents + total_fees_cents
    regular_installment_cents = (
        int(schedule[0]["total_cents"]) - origination_fee_cents if schedule else None
    )

    apr_bps = compute_apr_bps(
        principal_cents, annual_rate_bps, term_months, frequency, total_fees_cents
    )
    if exceeds_criminal_rate(apr_bps):
        warnings.append(
            f"APR {apr_bps / 100:.2f}% reaches the Criminal Code s.347 cap "
            f"({CRIMINAL_RATE_CAP_BPS / 100:.0f}%) — booking this file would be "
            "refused. Fix the pricing before sending anything to the borrower."
        )

    return (
        TermsSnapshot(
            principal_cents=principal_cents,
            annual_rate_bps=annual_rate_bps,
            apr_bps=apr_bps,
            term_months=term_months,
            payment_frequency=frequency,
            terms_source=terms_source,
            first_due_date=first_due_date,
            start_date=start_date,
            installment_count=len(schedule),
            regular_installment_cents=regular_installment_cents,
            total_of_payments_cents=total_of_payments_cents,
            total_interest_cents=total_interest_cents,
            total_fees_cents=total_fees_cents,
            finance_charge_cents=total_interest_cents + total_fees_cents,
            per_payment_fee_cents=per_payment_fee_cents,
            origination_fee_cents=origination_fee_cents,
            schedule=tuple(schedule),
        ),
        warnings,
    )


def build_schedule_rows(
    amortization_rows: Iterable[Any],
    *,
    principal_cents: int,
    per_payment_fee_cents: int = 0,
    origination_fee_cents: int = 0,
) -> list[dict[str, Any]]:
    """Fee-adjust the amortization rows into the agreement's schedule (pure).

    The .docx schedule has a "Fees Paid" column and a "Remaining Principal
    Balance" column that plain amortization rows do not carry, and its
    "Payment Amount" is inclusive of fees. Per the fee-schedule wording, the
    at-origination fee is billed on the FIRST installment.
    """
    out: list[dict[str, Any]] = []
    balance = int(principal_cents)
    rows = sorted(
        amortization_rows, key=lambda r: getattr(r, "installment_number", 0)
    )
    for row in rows:
        n = int(getattr(row, "installment_number", 0))
        principal = int(getattr(row, "principal_cents", 0))
        interest = int(getattr(row, "interest_cents", 0))
        fees = per_payment_fee_cents + (origination_fee_cents if n == 1 else 0)
        balance = max(0, balance - principal)
        out.append(
            {
                "installment_number": n,
                "due_date": getattr(row, "due_date", None),
                "principal_cents": principal,
                "interest_cents": interest,
                "fees_cents": fees,
                "total_cents": int(getattr(row, "total_cents", principal + interest)) + fees,
                "remaining_principal_cents": balance,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Merge context (pure)
# ---------------------------------------------------------------------------


def _fee_display(cfg: Any, fee_type: Any, frequency: Any) -> Optional[str]:
    """Formatted amount for one configured fee, or None when not charged."""
    from app.schemas.pricing_config import FeeCalc

    for fee in getattr(cfg, "fees", None) or []:
        if fee.fee_type is not fee_type or not fee.enabled:
            continue
        amount = fee.amount_for(frequency)
        if fee.calc is FeeCalc.RATE_BPS:
            return f"{_percent_bps(amount)} of principal"
        return _money(amount)
    return None


def build_agreement_context(
    application: Any,
    *,
    terms: TermsSnapshot,
    patient: Any = None,
    product: Any = None,
    vendor: Any = None,
    industry_category: Any = None,
    co_borrower: Any = None,
    co_borrower_patient: Any = None,
    bank_account: Any = None,
    company: Any = None,
    loan: Any = None,
) -> tuple[dict[str, str], list[FieldNote], list[FieldNote]]:
    """Build the full merge context for the agreement (pure).

    Returns ``(context, missing, not_applicable)``. EVERY field named in
    :data:`AGREEMENT_MERGE_FIELDS` is present in ``context`` — never absent,
    never silently blank (see the module docstring's QC contract).
    """
    from app.schemas.pricing_config import FeeType, PaymentFrequency, parse_pricing_config

    c = _Collector()

    # --- lender ------------------------------------------------------------
    c.put("CompanyName", getattr(company, "legal_name", None))
    primary = getattr(company, "primary", None)
    for name, kind in (
        ("CompanyAddress", "address"),
        ("SupportEmail", "email"),
        ("CompanyPhone", "phone"),
    ):
        c.put(
            name,
            primary(kind) if callable(primary) else None,
            reason=f"No primary {kind} contact is configured in company info.",
        )

    # --- vendor ------------------------------------------------------------
    c.put(
        "Vendor",
        _s(getattr(vendor, "dba_name", None)).strip()
        or getattr(vendor, "business_name", None),
        reason="The application has no vendor linked, or the vendor has no name.",
    )
    c.put(
        "VendorAddress",
        _address_line(
            _join_nonempty(
                [getattr(vendor, "address_line1", None), getattr(vendor, "address_line2", None)]
            ),
            None,
            getattr(vendor, "city", None),
            getattr(vendor, "province", None),
            getattr(vendor, "postal_code", None),
        ),
    )
    c.put("VendorProvince", getattr(vendor, "province", None))
    c.put(
        "VendorIndustryCategory",
        getattr(industry_category, "name", None),
        reason=(
            "The vendor has no industry category set — the agreement's "
            "'Goods & Services' description would be blank."
        ),
    )

    # --- borrower ----------------------------------------------------------
    c.put(
        "FullName",
        _join_nonempty(
            [
                getattr(application, "first_name", None)
                or getattr(patient, "legal_first_name", None),
                getattr(application, "middle_name", None),
                getattr(application, "last_name", None)
                or getattr(patient, "legal_last_name", None),
            ]
        ),
    )
    c.put(
        "BorrowerDateOfBirth",
        _date_str(
            getattr(application, "date_of_birth", None) or getattr(patient, "dob", None)
        ),
    )
    c.put(
        "BorrowerPhone",
        getattr(application, "main_phone", None) or getattr(patient, "phone_e164", None),
    )
    c.put(
        "ContactEmail",
        getattr(application, "email", None) or getattr(patient, "email", None),
    )
    c.put(
        "BorrowerAddress",
        _address_line(
            getattr(application, "residence_street", None),
            getattr(application, "residence_unit", None),
            getattr(application, "residence_city", None),
            getattr(application, "residence_province", None),
            getattr(application, "residence_postal_code", None),
        ),
    )
    c.put("BorrowerAddress_Street", getattr(application, "residence_street", None))
    # A unit number is genuinely optional (houses do not have one) — flagging it
    # as MISSING on every ground-level address would be noise.
    if _s(getattr(application, "residence_unit", None)).strip():
        c.put("BorrowerAddress_Appartment", getattr(application, "residence_unit", None))
    else:
        c.na(
            "BorrowerAddress_Appartment",
            "No unit/apartment on the borrower's address (optional field).",
            value="",
        )
    c.put("BorrowerAddress_City", getattr(application, "residence_city", None))
    c.put("BorrowerAddress_Province", getattr(application, "residence_province", None))
    c.put("BorrowerAddress_ZipCode", getattr(application, "residence_postal_code", None))

    # --- co-borrower -------------------------------------------------------
    # No co-borrower is a legitimate shape, not a gap: render "N/A" and report
    # it under not_applicable so the reviewer confirms it was expected.
    if co_borrower is None:
        for name in AGREEMENT_MERGE_FIELDS["CoBorrower"]:
            c.na(name, "This application has no linked co-borrower file.")
    else:
        c.put(
            "CoApplicantFullName",
            _join_nonempty(
                [
                    getattr(co_borrower, "first_name", None)
                    or getattr(co_borrower_patient, "legal_first_name", None),
                    getattr(co_borrower, "middle_name", None),
                    getattr(co_borrower, "last_name", None)
                    or getattr(co_borrower_patient, "legal_last_name", None),
                ]
            ),
        )
        c.put(
            "CoApplicantDateOfBirth",
            _date_str(
                getattr(co_borrower, "date_of_birth", None)
                or getattr(co_borrower_patient, "dob", None)
            ),
        )
        c.put(
            "CoApplicantAddress",
            _address_line(
                getattr(co_borrower, "residence_street", None),
                getattr(co_borrower, "residence_unit", None),
                getattr(co_borrower, "residence_city", None),
                getattr(co_borrower, "residence_province", None),
                getattr(co_borrower, "residence_postal_code", None),
            ),
        )
        c.put(
            "CoApplicantPhone",
            getattr(co_borrower, "main_phone", None)
            or getattr(co_borrower_patient, "phone_e164", None),
        )
        c.put(
            "CoApplicantEmail",
            getattr(co_borrower, "email", None)
            or getattr(co_borrower_patient, "email", None),
        )

    # --- identity / dates --------------------------------------------------
    c.put("LoanId", _loan_id_value(application, loan))
    c.put("StartDate", _date_str(terms.start_date))
    c.put(
        "InterestStartDate",
        _date_str(getattr(application, "loan_start_date", None) or terms.start_date),
    )
    c.put("ContractDate", _date_str(getattr(application, "agreement_signed_at", None)))

    # --- terms -------------------------------------------------------------
    # A zero/absent principal must be a flagged GAP, not a confident "$0.00".
    c.put(
        "LoanAmount",
        _money(terms.principal_cents) if terms.principal_cents else None,
        reason="The application has no positive principal (amount financed).",
    )
    c.put("InterestRate", _percent_bps(terms.annual_rate_bps))
    c.put("APR", _percent_bps(terms.apr_bps))
    c.put("LoanTerm", terms.term_months)
    c.put(
        "RepaymentPeriod",
        _FREQUENCY_LABELS.get(terms.payment_frequency, terms.payment_frequency),
    )
    c.put("NumberOfInstallments", terms.installment_count or None)
    c.put("RegularInstallmentAmount", _money(terms.regular_installment_cents))
    c.put("TotalAmountToPay", _money(terms.total_of_payments_cents))
    c.put("TotalInterest", _money(terms.total_interest_cents))
    c.put("TotalFee", _money(terms.total_fees_cents))
    c.put("FinanceCharge", _money(terms.finance_charge_cents))

    first_row = terms.schedule[0] if terms.schedule else None
    last_row = terms.schedule[-1] if terms.schedule else None
    no_schedule = "No schedule could be computed — the terms above are incomplete."
    c.put(
        "FirstInstallment",
        _money(first_row["total_cents"]) if first_row else None,
        reason=no_schedule,
    )
    c.put(
        "FirstInstallmentDate",
        _date_str(first_row["due_date"]) if first_row else None,
        reason=no_schedule,
    )
    c.put(
        "LastInstallment",
        _money(last_row["total_cents"]) if last_row else None,
        reason=no_schedule,
    )
    c.put(
        "LastPaymentDate",
        _date_str(last_row["due_date"]) if last_row else None,
        reason=no_schedule,
    )

    # --- fees --------------------------------------------------------------
    cfg = parse_pricing_config(
        getattr(product, "pricing_config", None), context="agreement preview fees"
    )
    try:
        frequency_enum = PaymentFrequency(terms.payment_frequency)
    except ValueError:
        frequency_enum = PaymentFrequency.MONTHLY
    for name, fee_type in (
        ("OriginationFeeFull", FeeType.ORIGINATION),
        ("AdministrationFeeFull", FeeType.ADMINISTRATION),
        ("RepaymentFeeRate", FeeType.REPAYMENT),
        ("LateFeeFull", FeeType.LATE),
        ("NSFFull", FeeType.NSF),
    ):
        display = _fee_display(cfg, fee_type, frequency_enum)
        if display:
            c.put(name, display)
        else:
            # An unconfigured/disabled fee is a real answer ("we don't charge
            # this"), not a hole — the late fee is disabled by Canada policy.
            c.na(
                name,
                f"The credit product does not charge a {fee_type.value} fee "
                "(absent or disabled in its pricing config).",
                value=NOT_CHARGED_VALUE,
            )

    # --- PAD ---------------------------------------------------------------
    no_bank = (
        "No default bank account is on file for the borrower — the PAD section "
        "cannot be completed."
    )
    c.put("BorrowerBankNumber", getattr(bank_account, "institution_number", None), reason=no_bank)
    c.put(
        "BorrowerBankRoutingNumber",
        getattr(bank_account, "transit_number", None),
        reason=no_bank,
    )
    # SECURITY: the full account number is Fernet-encrypted at rest and is NEVER
    # decrypted for a preview. The mask is what a QC reviewer needs to confirm
    # the right account was picked; the real number is merged only by the
    # production PAD path.
    c.put("BorrowerBankAccount", getattr(bank_account, "account_mask", None), reason=no_bank)

    return c.ctx, c.missing, c.not_applicable


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------

#: ``{{Table:Schedule}}`` = the whole table incl. its header row (what the
#: built-in skeleton uses). ``{{Rows:Schedule}}`` = only the ``<tr>`` rows, for a
#: template imported from the .docx, whose table already carries Dave's own
#: header and Totals rows and just needs the repeat block filled in.
_TABLE_TOKEN_RE = re.compile(r"\{\{\s*(Table|Rows):([A-Za-z0-9_]+)\s*\}\}")

#: Scalar placeholders a template references — used to scope the QC pass to the
#: fields THIS template actually needs.
_SCALAR_TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


def _schedule_body_rows(rows: Iterable[dict[str, Any]]) -> str:
    out: list[str] = []
    for r in rows:
        cells = [
            _s(r.get("installment_number")),
            _date_str(r.get("due_date")),
            _money(r.get("total_cents")),
            _money(r.get("interest_cents")),
            _money(r.get("principal_cents")),
            _money(r.get("fees_cents")),
            _money(r.get("remaining_principal_cents")),
        ]
        out.append("<tr>" + "".join(f"<td>{_html.escape(v)}</td>" for v in cells) + "</tr>")
    return "".join(out)


def _render_schedule_table(rows: Iterable[dict[str, Any]]) -> str:
    head = "".join(f"<th>{_html.escape(c)}</th>" for c in AGREEMENT_SCHEDULE_COLUMNS)
    return (
        '<table class="agreement-schedule">'
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody>{_schedule_body_rows(rows)}</tbody></table>"
    )


def render_agreement_body(
    body_html: str,
    context: dict[str, str],
    schedule_rows: Iterable[dict[str, Any]],
    tables: Optional[dict[str, list[dict[str, Any]]]] = None,
) -> document_engine.RenderResult:
    """Render the agreement body (pure).

    Two passes so the shared engine stays untouched: this module substitutes
    its own schedule block (a 7-column layout the loan-level ``TABLE_FIELDS``
    does not define), then delegates scalar substitution, the loan-level
    ``{{Table:…}}`` blocks, and unknown-placeholder reporting to
    ``document_engine.render_template``.
    """
    schedule_rows = list(schedule_rows)

    def _sub(match: re.Match[str]) -> str:
        kind, name = match.group(1), match.group(2)
        if name != AGREEMENT_SCHEDULE_TABLE:
            return match.group(0)  # the engine's own tables, or an unknown name
        if kind == "Rows":
            return _schedule_body_rows(schedule_rows)
        return _render_schedule_table(schedule_rows)

    with_tables = _TABLE_TOKEN_RE.sub(_sub, body_html)
    return document_engine.render_template(with_tables, context, tables or {})


# ---------------------------------------------------------------------------
# Built-in fallback: a QC DATA SHEET, deliberately NOT a contract
# ---------------------------------------------------------------------------

#: Used only when no ``loan_agreement`` template row exists. Headings + labels +
#: merge fields; NO legal clause text — the real agreement's wording is Dave's
#: proprietary property and is loaded out-of-band into the template table (see
#: ``scripts/seed_loan_agreement_template.py``). Every documented merge field
#: appears here exactly once, so the fallback is also a complete QC checklist.
BUILTIN_QC_SKELETON_HTML = """
<h1>Loan agreement — terms data sheet</h1>
<p><em>No loan-agreement template is configured, so this generic data sheet is
shown instead. It lists every value that would be merged into the agreement, so
the figures can still be quality-checked. It is <strong>not</strong> the
agreement and contains none of its terms.</em></p>

<h2>Lender</h2>
<ul>
  <li>Legal name: {{CompanyName}}</li>
  <li>Address: {{CompanyAddress}}</li>
  <li>Support email: {{SupportEmail}}</li>
  <li>Phone: {{CompanyPhone}}</li>
</ul>

<h2>Vendor</h2>
<ul>
  <li>Name (DBA): {{Vendor}}</li>
  <li>Address: {{VendorAddress}}</li>
  <li>Province (governing law): {{VendorProvince}}</li>
  <li>Goods &amp; Services: {{VendorIndustryCategory}}</li>
</ul>

<h2>Borrower</h2>
<ul>
  <li>Name: {{FullName}}</li>
  <li>Date of birth: {{BorrowerDateOfBirth}}</li>
  <li>Address: {{BorrowerAddress}}</li>
  <li>Phone: {{BorrowerPhone}}</li>
  <li>Email: {{ContactEmail}}</li>
</ul>

<h2>Co-borrower</h2>
<ul>
  <li>Name: {{CoApplicantFullName}}</li>
  <li>Date of birth: {{CoApplicantDateOfBirth}}</li>
  <li>Address: {{CoApplicantAddress}}</li>
  <li>Phone: {{CoApplicantPhone}}</li>
  <li>Email: {{CoApplicantEmail}}</li>
</ul>

<h2>Account</h2>
<ul>
  <li>Account number: {{LoanId}}</li>
  <li>Date of agreement: {{StartDate}}</li>
  <li>Interest start date: {{InterestStartDate}}</li>
  <li>Signature date: {{ContractDate}}</li>
</ul>

<h2>Terms</h2>
<ul>
  <li>Amount financed (principal): {{LoanAmount}}</li>
  <li>Annual interest rate: {{InterestRate}}</li>
  <li>Annual percentage rate (APR): {{APR}}</li>
  <li>Loan term (months): {{LoanTerm}}</li>
  <li>Payment frequency: {{RepaymentPeriod}}</li>
  <li>Number of installments: {{NumberOfInstallments}}</li>
  <li>Regular installment: {{RegularInstallmentAmount}}</li>
  <li>First installment: {{FirstInstallment}} on {{FirstInstallmentDate}}</li>
  <li>Final installment: {{LastInstallment}} on {{LastPaymentDate}}</li>
  <li>Total of payments: {{TotalAmountToPay}}</li>
  <li>Total interest: {{TotalInterest}}</li>
  <li>Total fees: {{TotalFee}}</li>
  <li>Cost of borrowing: {{FinanceCharge}}</li>
</ul>

<h2>Fees</h2>
<ul>
  <li>Origination fee: {{OriginationFeeFull}}</li>
  <li>Administration fee: {{AdministrationFeeFull}}</li>
  <li>Repayment fee: {{RepaymentFeeRate}}</li>
  <li>Late fee: {{LateFeeFull}}</li>
  <li>Dishonoured payment fee: {{NSFFull}}</li>
</ul>

<h2>Amortization schedule</h2>
{{Table:Schedule}}

<h2>Pre-authorized debit (PAD)</h2>
<ul>
  <li>Payor: {{FullName}}</li>
  <li>Street: {{BorrowerAddress_Street}}</li>
  <li>Apartment / unit: {{BorrowerAddress_Appartment}}</li>
  <li>City: {{BorrowerAddress_City}}</li>
  <li>Province: {{BorrowerAddress_Province}}</li>
  <li>Postal code: {{BorrowerAddress_ZipCode}}</li>
  <li>Institution number: {{BorrowerBankNumber}}</li>
  <li>Transit number: {{BorrowerBankRoutingNumber}}</li>
  <li>Account number (masked): {{BorrowerBankAccount}}</li>
</ul>
""".strip()

BUILTIN_SKELETON_TITLE = "Loan agreement — terms data sheet (no template configured)"


# ---------------------------------------------------------------------------
# Assembly (pure)
# ---------------------------------------------------------------------------


def proposed_loan(application: Any, terms: TermsSnapshot, loan: Any = None) -> Any:
    """A loan-SHAPED view of the proposed terms (pure, never persisted).

    Lets the shared ``document_engine`` context/table builders run against an
    application that has no loan yet — see :func:`build_preview`. Nothing here
    is written anywhere; it exists only for the duration of one render.
    """
    schedule = [
        SimpleNamespace(
            installment_number=r["installment_number"],
            due_date=r["due_date"],
            principal_cents=r["principal_cents"],
            interest_cents=r["interest_cents"],
            total_cents=r["total_cents"],
        )
        for r in terms.schedule
    ]
    return SimpleNamespace(
        id=getattr(loan, "id", None),
        status=getattr(loan, "status", None) or "proposed",
        principal_cents=terms.principal_cents,
        annual_rate_bps=terms.annual_rate_bps,
        term_months=terms.term_months,
        currency=getattr(loan, "currency", None) or "CAD",
        disbursed_at=getattr(loan, "disbursed_at", None),
        schedule=schedule,
    )


def build_preview(
    application: Any,
    *,
    template: Any = None,
    patient: Any = None,
    product: Any = None,
    vendor: Any = None,
    industry_category: Any = None,
    co_borrower: Any = None,
    co_borrower_patient: Any = None,
    bank_account: Any = None,
    company: Any = None,
    loan: Any = None,
    accepted_offer: Any = None,
    now: Optional[datetime] = None,
) -> PreviewResult:
    """Resolve terms, build the context, render — pure, DB-free, never cached."""
    now = now or datetime.now(timezone.utc)
    terms, warnings = resolve_terms(
        application, product=product, accepted_offer=accepted_offer, loan=loan,
        today=now.date(),
    )
    context, missing, not_applicable = build_agreement_context(
        application,
        terms=terms,
        patient=patient,
        product=product,
        vendor=vendor,
        industry_category=industry_category,
        co_borrower=co_borrower,
        co_borrower_patient=co_borrower_patient,
        bank_account=bank_account,
        company=company,
        loan=loan,
    )

    # TWO VOCABULARIES, one render. Dave's .docx uses his own field names
    # (``FullName``, ``LoanAmount``, …); the templates already shipped in
    # ``platform_document_templates`` use ``document_engine``'s loan-level names
    # (``BorrowerFullName``, ``PrincipalAmount``, ``{{Table:AmortizationSchedule}}``,
    # …). A QC preview has to render WHATEVER template is configured, so the
    # loan-level context is built too — off a loan-SHAPED view of the proposed
    # terms — and the agreement's own names take precedence where they collide.
    proposed = proposed_loan(application, terms, loan)
    alias_context = document_engine.build_scalar_context(
        loan=proposed, patient=patient, product=product, vendor=vendor, now=now
    )
    tables = document_engine.build_tables(loan=proposed, product=product)
    context = {**alias_context, **context}

    if template is not None:
        body = getattr(template, "body_html", "") or ""
        title = getattr(template, "title", None) or "Loan agreement"
        source = "db_template"
    else:
        body = BUILTIN_QC_SKELETON_HTML
        title = BUILTIN_SKELETON_TITLE
        source = "builtin_skeleton"
        warnings.append(
            "No active loan_agreement template is configured, so a generic terms "
            "data sheet was rendered instead of the real agreement. Load the "
            "agreement template before using this preview as a sign-off."
        )

    # QC pass over the loan-level alias fields: only those the template actually
    # references are judged, so pre-loan-empty-by-design fields it never uses
    # (DisbursedDate, Statement*, …) do not flood the gap list.
    referenced = set(_SCALAR_TOKEN_RE.findall(body))
    for name in sorted(referenced & set(alias_context)):
        if not context[name].strip():
            context[name] = NOT_AVAILABLE_FMT.format(field=name)
            missing.append(
                FieldNote(
                    field=name,
                    source=f"document_engine loan-level merge field ({name}).",
                    reason=(
                        "The configured template uses this loan-level field and "
                        "nothing on the application supplies it."
                    ),
                )
            )

    result = render_agreement_body(body, context, terms.schedule, tables)
    if result.unknown_fields:
        warnings.append(
            "The template references placeholders this engine cannot fill: "
            + ", ".join(result.unknown_fields)
        )

    return PreviewResult(
        # The banner is prepended to the RENDERED body so the disclaimer travels
        # with any copy of the HTML, not just the JSON envelope.
        html=PREVIEW_BANNER_HTML + result.html,
        title=f"PREVIEW — {title}",
        application_id=getattr(application, "id", None),
        application_status=_s(getattr(application, "status", None)),
        template_source=source,
        template_id=getattr(template, "id", None),
        template_version=getattr(template, "version", None),
        merge_data=context,
        missing_fields=tuple(missing),
        not_applicable_fields=tuple(not_applicable),
        unknown_fields=result.unknown_fields,
        warnings=tuple(warnings),
        terms=terms,
        generated_at=now,
    )


# ---------------------------------------------------------------------------
# DB wrapper — the only part that queries
# ---------------------------------------------------------------------------


def generate_agreement_preview(
    db: Session, application: PlatformCreditApplication
) -> PreviewResult:
    """Load the application graph and render the preview. NEVER persists.

    Every call re-reads and re-renders, so the preview can never go stale
    against the application's current terms.
    """
    from app.models.loan import Vendor
    from app.models.platform.borrower_portal import PlatformPatientBankAccount
    from app.models.platform.credit_product import PlatformCreditProduct
    from app.models.platform.crm import PlatformIndustryCategory
    from app.models.platform.loan import PlatformLoan
    from app.models.platform.loan_offer import PlatformLoanOffer
    from app.models.platform.patient import PlatformPatient
    from app.services import company_info
    from app.services.application_actions import (
        co_borrower_applications,
        primary_application,
    )

    # A co-borrower file is not previewed on its own — the agreement is written
    # on the primary file, with the co-borrower merged into it.
    application = primary_application(db, application)

    patient = (
        db.query(PlatformPatient)
        .filter(PlatformPatient.id == application.patient_id)
        .first()
    )
    product = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.id == application.credit_product_id)
        .first()
        if application.credit_product_id is not None
        else None
    )
    vendor = (
        db.query(Vendor).filter(Vendor.id == application.vendor_id).first()
        if application.vendor_id is not None
        else None
    )
    industry_category = (
        db.query(PlatformIndustryCategory)
        .filter(PlatformIndustryCategory.id == vendor.industry_category_id)
        .first()
        if vendor is not None and vendor.industry_category_id is not None
        else None
    )

    co_borrowers = co_borrower_applications(db, application)
    co_borrower = co_borrowers[0] if co_borrowers else None
    co_borrower_patient = (
        db.query(PlatformPatient)
        .filter(PlatformPatient.id == co_borrower.patient_id)
        .first()
        if co_borrower is not None
        else None
    )

    bank_account = (
        db.query(PlatformPatientBankAccount)
        .filter(
            PlatformPatientBankAccount.patient_id == application.patient_id,
            PlatformPatientBankAccount.status == "active",
        )
        .order_by(
            PlatformPatientBankAccount.is_default.desc(),
            PlatformPatientBankAccount.created_at.asc(),
        )
        .first()
    )

    accepted_offer = (
        db.query(PlatformLoanOffer)
        .filter(
            PlatformLoanOffer.application_id == application.id,
            PlatformLoanOffer.status == "accepted",
        )
        .order_by(PlatformLoanOffer.accepted_at.desc())
        .first()
    )

    # Usually None (the whole point is a PRE-loan preview) — but once activation
    # has booked one, showing the real account number beats a placeholder.
    loan = (
        db.query(PlatformLoan)
        .filter(PlatformLoan.application_id == application.id)
        .order_by(PlatformLoan.created_at.asc())
        .first()
    )

    template = document_engine.resolve_template(
        db,
        "loan_agreement",
        product_id=application.credit_product_id,
        vendor_id=application.vendor_id,
    )

    preview = build_preview(
        application,
        template=template,
        patient=patient,
        product=product,
        vendor=vendor,
        industry_category=industry_category,
        co_borrower=co_borrower,
        co_borrower_patient=co_borrower_patient,
        bank_account=bank_account,
        company=company_info.get_company_info(db),
        loan=loan,
        accepted_offer=accepted_offer,
    )
    logger.info(
        "application_agreement_preview_generated",
        application_id=str(application.id),
        template_source=preview.template_source,
        missing_field_count=len(preview.missing_fields),
        warning_count=len(preview.warnings),
    )
    return preview
