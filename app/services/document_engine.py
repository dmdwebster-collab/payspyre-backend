"""Document / merge-field engine (WS-B — Turnkey "System documents" parity).

Turns a versioned ``PlatformDocumentTemplate`` (HTML with merge-field
placeholders) plus one loan's facts into a rendered, frozen
``PlatformLoanDocument``. This is the engine behind:

* booking-time loan agreement + PAD agreement snapshots (``book_loan`` hook),
* admin on-demand generation/regeneration,
* the borrower documents tab (agreement / T&Cs / privacy) and on-demand
  account statements.

PLACEHOLDER SYNTAX (deliberately NOT Jinja — admin-authored template bodies
must not be able to execute expressions; this is a closed substitution
language):

* Scalar:  ``{{FieldName}}``            → HTML-escaped value
* Table:   ``{{Table:TableName}}``      → a full ``<table>`` rendered from the
                                          canonical column set below

Unknown placeholders render as empty strings and are reported back to the
caller (the admin preview endpoint surfaces them as warnings) — a typo'd field
never ships the literal ``{{...}}`` to a borrower.

The CANONICAL MERGE-FIELD DICTIONARY lives here (``MERGE_FIELDS`` /
``TABLE_FIELDS``) and is served by ``GET /admin/document-templates/merge-fields``
(Turnkey's "Merge fields" dialog). Mirrors Turnkey naming (PascalCase; company
fields match Dave's Mergefields doc / notification_render's global context).

Everything render-related is a PURE function over plain attribute objects so it
is DB-free testable; thin DB wrappers at the bottom own queries/persistence.
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.document_template import (
    DOCUMENT_KINDS,
    DOCUMENT_SCOPES,
    PlatformDocumentTemplate,
    PlatformLoanDocument,
)
from app.models.platform.loan import PlatformLoan
from app.models.platform.patient import PlatformPatient

logger = get_logger(__name__)

__all__ = [
    "DOCUMENT_KINDS",
    "DOCUMENT_SCOPES",
    "MERGE_FIELDS",
    "TABLE_FIELDS",
    "DocumentEngineError",
    "RenderResult",
    "render_template",
    "build_scalar_context",
    "build_tables",
    "pick_template",
    "resolve_template",
    "generate_loan_document",
    "generate_booking_documents",
    "generate_statement_document",
    "latest_active_template",
    "render_standalone_document",
]


class DocumentEngineError(Exception):
    """Raised for caller errors (unknown kind, no template, bad scope)."""


# ---------------------------------------------------------------------------
# Canonical merge-field dictionary (Turnkey "Merge fields" dialog parity)
# ---------------------------------------------------------------------------

#: Scalar fields, grouped exactly as the admin merge-fields browser shows them.
#: Every name here is produced by ``build_scalar_context`` (a test enforces the
#: two stay in lockstep — extend BOTH when adding a field).
MERGE_FIELDS: dict[str, dict[str, str]] = {
    "Borrower": {
        "BorrowerFullName": "Full legal name of the borrower.",
        "BorrowerFirstName": "Borrower's legal first name.",
        "BorrowerLastName": "Borrower's legal last name.",
        "BorrowerEmail": "Borrower's email address.",
        "BorrowerPhone": "Borrower's phone number (E.164).",
        "BorrowerDateOfBirth": "Borrower's date of birth (YYYY-MM-DD).",
    },
    "Loan": {
        "LoanId": "PaySpyre loan identifier.",
        "LoanStatus": "Current loan status (pending_disbursement/active/...).",
        "PrincipalAmount": "Loan principal, formatted (e.g. $2,400.00).",
        "AnnualInterestRate": "Annual interest rate, formatted (e.g. 9.90%).",
        "TermMonths": "Loan term in months.",
        "InstallmentCount": "Number of scheduled installments.",
        "FirstDueDate": "Due date of the first installment (YYYY-MM-DD).",
        "MaturityDate": "Due date of the final installment (YYYY-MM-DD).",
        "TotalOfPayments": "Sum of all scheduled installments, formatted.",
        "TotalInterest": "Total scheduled interest over the term, formatted.",
        "Currency": "Loan currency (e.g. CAD).",
        "DisbursedDate": "Date funds were disbursed (empty until disbursed).",
    },
    "Product": {
        "ProductName": "Credit product name.",
        "ProductCode": "Credit product code.",
        "ProductVertical": "Product vertical (dental/auto/veterinary).",
    },
    "Vendor": {
        "VendorName": "Vendor (clinic) business name.",
        "VendorDbaName": "Vendor 'doing business as' name.",
        "VendorEmail": "Vendor contact email.",
        "VendorPhone": "Vendor contact phone.",
        "VendorAddress": "Vendor street address (line 1).",
        "VendorCity": "Vendor city.",
        "VendorProvince": "Vendor province.",
        "VendorPostalCode": "Vendor postal code.",
    },
    "Company": {
        "CompanyName": "Lender legal name (PaySpyre Financial Inc.).",
        "SupportEmail": "Support email from company settings.",
        "CompanyPhone": "Company phone from company settings.",
        "WebsiteUrl": "Public website URL.",
        "TermsUrl": "Published terms-and-conditions URL.",
        "PrivacyUrl": "Published privacy-policy URL.",
    },
    "General": {
        "GeneratedDate": "Date this document was generated (YYYY-MM-DD).",
    },
    "Statement": {
        "StatementPeriodStart": "Statement period start (statements only).",
        "StatementPeriodEnd": "Statement period end (statements only).",
        "StatementOpeningBalance": "Opening principal balance, formatted.",
        "StatementClosingBalance": "Closing principal balance, formatted.",
        "StatementPrincipalPaid": "Principal paid in the period, formatted.",
        "StatementInterestPaid": "Interest paid in the period, formatted.",
    },
}

#: Table fields: ``{{Table:Name}}`` renders the whole table.
TABLE_FIELDS: dict[str, dict[str, Any]] = {
    "AmortizationSchedule": {
        "description": "Full amortization schedule (one row per installment).",
        "columns": ["#", "Due date", "Principal", "Interest", "Total"],
    },
    "FeeSchedule": {
        "description": "Enabled fees configured on the credit product.",
        "columns": ["Fee", "Amount", "When charged", "Add-on"],
    },
}

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+(?::[A-Za-z0-9_]+)?)\s*\}\}")


# ---------------------------------------------------------------------------
# Formatting helpers (pure)
# ---------------------------------------------------------------------------


def _money(cents: Optional[int], currency: str = "") -> str:
    if cents is None:
        return ""
    sign = "-" if cents < 0 else ""
    dollars = abs(int(cents)) / 100
    suffix = f" {currency}" if currency else ""
    return f"{sign}${dollars:,.2f}{suffix}"


def _percent_bps(bps: Optional[int]) -> str:
    if bps is None:
        return ""
    return f"{bps / 100:.2f}%"


def _date_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _s(value: Any) -> str:
    return "" if value is None else str(value)


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderResult:
    """Output of ``render_template``: the HTML + any placeholders that did not
    resolve (surfaced as admin-preview warnings, rendered as empty strings)."""

    html: str
    unknown_fields: tuple[str, ...] = field(default_factory=tuple)


def _render_table(name: str, rows: Iterable[dict[str, Any]]) -> str:
    spec = TABLE_FIELDS[name]
    columns: list[str] = spec["columns"]
    head = "".join(f"<th>{_html.escape(c)}</th>" for c in columns)
    body_rows = []
    for row in rows:
        cells = "".join(
            f"<td>{_html.escape(_s(row.get(c)))}</td>" for c in columns
        )
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        f'<table class="merge-table merge-table--{name.lower()}">'
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table>"
    )


def render_template(
    body_html: str,
    context: dict[str, Any],
    tables: Optional[dict[str, list[dict[str, Any]]]] = None,
) -> RenderResult:
    """Substitute merge fields into ``body_html`` (pure).

    * ``{{Name}}`` → ``html.escape(str(context[Name]))``
    * ``{{Table:Name}}`` → rendered HTML table from ``tables[Name]``
    * unknown names → empty string, reported in ``unknown_fields``.
    """
    tables = tables or {}
    unknown: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        token = match.group(1)
        if token.startswith("Table:"):
            table_name = token.split(":", 1)[1]
            if table_name in TABLE_FIELDS and table_name in tables:
                return _render_table(table_name, tables[table_name])
            unknown.append(token)
            return ""
        if token in context:
            return _html.escape(_s(context[token]))
        unknown.append(token)
        return ""

    rendered = _PLACEHOLDER_RE.sub(_sub, body_html)
    return RenderResult(html=rendered, unknown_fields=tuple(dict.fromkeys(unknown)))


# ---------------------------------------------------------------------------
# Context builders (pure — accept any attribute objects, DB-free testable)
# ---------------------------------------------------------------------------


def _company_context() -> dict[str, str]:
    """Company facts — same source of truth as notification_render's global
    context (Dave's CompanyName/SupportEmail/CompanyPhone merge fields)."""
    from app.core.config import settings

    return {
        "CompanyName": "PaySpyre Financial Inc.",
        "SupportEmail": getattr(settings, "SUPPORT_EMAIL", "support@payspyre.com"),
        "CompanyPhone": getattr(settings, "COMPANY_PHONE", ""),
        "WebsiteUrl": "https://www.payspyre.com",
        "TermsUrl": "https://www.payspyre.com/terms-and-conditions/",
        "PrivacyUrl": "https://www.payspyre.com/privacy-policy/",
    }


def build_scalar_context(
    loan: Any = None,
    patient: Any = None,
    product: Any = None,
    vendor: Any = None,
    *,
    now: Optional[datetime] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, str]:
    """The full scalar merge context for a document render (pure).

    Any entity may be None (fields render empty) — a template must never crash
    a booking. ``extra`` (e.g. statement fields) wins over computed values.
    """
    now = now or datetime.now(timezone.utc)
    schedule = sorted(
        list(getattr(loan, "schedule", None) or []),
        key=lambda s: getattr(s, "installment_number", 0),
    )
    total_of_payments = sum(getattr(s, "total_cents", 0) for s in schedule)
    total_interest = sum(getattr(s, "interest_cents", 0) for s in schedule)

    ctx: dict[str, str] = {
        # Borrower
        "BorrowerFullName": " ".join(
            p
            for p in (
                _s(getattr(patient, "legal_first_name", None)),
                _s(getattr(patient, "legal_last_name", None)),
            )
            if p
        ),
        "BorrowerFirstName": _s(getattr(patient, "legal_first_name", None)),
        "BorrowerLastName": _s(getattr(patient, "legal_last_name", None)),
        "BorrowerEmail": _s(getattr(patient, "email", None)),
        "BorrowerPhone": _s(getattr(patient, "phone_e164", None)),
        "BorrowerDateOfBirth": _date_str(getattr(patient, "dob", None)),
        # Loan
        "LoanId": _s(getattr(loan, "id", None)),
        "LoanStatus": _s(getattr(loan, "status", None)),
        "PrincipalAmount": _money(getattr(loan, "principal_cents", None)),
        "AnnualInterestRate": _percent_bps(getattr(loan, "annual_rate_bps", None)),
        "TermMonths": _s(getattr(loan, "term_months", None)),
        "InstallmentCount": _s(len(schedule) if schedule else ""),
        "FirstDueDate": _date_str(schedule[0].due_date) if schedule else "",
        "MaturityDate": _date_str(schedule[-1].due_date) if schedule else "",
        "TotalOfPayments": _money(total_of_payments) if schedule else "",
        "TotalInterest": _money(total_interest) if schedule else "",
        "Currency": _s(getattr(loan, "currency", None)),
        "DisbursedDate": _date_str(getattr(loan, "disbursed_at", None)),
        # Product
        "ProductName": _s(getattr(product, "name", None)),
        "ProductCode": _s(getattr(product, "code", None)),
        "ProductVertical": _s(getattr(product, "vertical", None)),
        # Vendor
        "VendorName": _s(getattr(vendor, "business_name", None)),
        "VendorDbaName": _s(getattr(vendor, "dba_name", None)),
        "VendorEmail": _s(getattr(vendor, "email", None)),
        "VendorPhone": _s(getattr(vendor, "phone", None)),
        "VendorAddress": _s(getattr(vendor, "address_line1", None)),
        "VendorCity": _s(getattr(vendor, "city", None)),
        "VendorProvince": _s(getattr(vendor, "province", None)),
        "VendorPostalCode": _s(getattr(vendor, "postal_code", None)),
        # Company + general
        **_company_context(),
        "GeneratedDate": now.date().isoformat(),
        # Statement fields default empty (populated via ``extra`` by the
        # statement path so the dictionary test can assert full coverage).
        "StatementPeriodStart": "",
        "StatementPeriodEnd": "",
        "StatementOpeningBalance": "",
        "StatementClosingBalance": "",
        "StatementPrincipalPaid": "",
        "StatementInterestPaid": "",
    }
    if extra:
        ctx.update({k: _s(v) for k, v in extra.items()})
    return ctx


def build_tables(loan: Any = None, product: Any = None) -> dict[str, list[dict[str, Any]]]:
    """Table merge data: amortization rows + product fee rows (pure).

    A malformed ``pricing_config`` yields an empty fee table (documents must
    keep generating); the parse error is logged, never raised.
    """
    schedule = sorted(
        list(getattr(loan, "schedule", None) or []),
        key=lambda s: getattr(s, "installment_number", 0),
    )
    amort_rows = [
        {
            "#": getattr(s, "installment_number", ""),
            "Due date": _date_str(getattr(s, "due_date", None)),
            "Principal": _money(getattr(s, "principal_cents", None)),
            "Interest": _money(getattr(s, "interest_cents", None)),
            "Total": _money(getattr(s, "total_cents", None)),
        }
        for s in schedule
    ]

    fee_rows: list[dict[str, Any]] = []
    raw_pricing = getattr(product, "pricing_config", None)
    if raw_pricing:
        try:
            from app.schemas.pricing_config import FeeCalc, parse_pricing_config

            cfg = parse_pricing_config(raw_pricing, context="document generation")
            timing_labels = {
                "per_payment": "Every payment",
                "at_origination": "At origination",
                "on_event": "On event (contingent)",
            }
            for fee in cfg.fees or []:
                if not fee.enabled:
                    continue
                if fee.calc == FeeCalc.FIXED_CENTS:
                    amount = _money(fee.amount)
                else:
                    amount = f"{_percent_bps(fee.amount)} of principal"
                fee_rows.append(
                    {
                        "Fee": fee.fee_type.value.replace("_", " ").title(),
                        "Amount": amount,
                        "When charged": timing_labels.get(
                            fee.charge_timing.value, fee.charge_timing.value
                        ),
                        "Add-on": "Yes" if fee.add_on else "No",
                    }
                )
        except Exception:
            logger.warning("document_engine_fee_parse_failed", exc_info=True)

    return {"AmortizationSchedule": amort_rows, "FeeSchedule": fee_rows}


def pick_template(
    templates: Iterable[Any],
    product_id: Optional[UUID] = None,
    vendor_id: Optional[UUID] = None,
) -> Optional[Any]:
    """Choose the winning template among active candidates of ONE kind (pure).

    Precedence: vendor-scoped (matching ``vendor_id``) > product-scoped
    (matching ``product_id``) > global. Within a scope the highest version
    wins. Non-matching scoped templates never apply.
    """
    best: Optional[Any] = None
    best_rank = (-1, -1)
    for t in templates:
        scope = getattr(t, "scope", "global")
        if scope == "vendor":
            if vendor_id is None or getattr(t, "vendor_id", None) != vendor_id:
                continue
            rank = 2
        elif scope == "product":
            if product_id is None or getattr(t, "product_id", None) != product_id:
                continue
            rank = 1
        else:
            rank = 0
        key = (rank, getattr(t, "version", 1))
        if key > best_rank:
            best, best_rank = t, key
    return best


# ---------------------------------------------------------------------------
# DB wrappers
# ---------------------------------------------------------------------------


def resolve_template(
    db: Session,
    kind: str,
    product_id: Optional[UUID] = None,
    vendor_id: Optional[UUID] = None,
) -> Optional[PlatformDocumentTemplate]:
    """The template that applies for ``kind`` given a loan's product/vendor."""
    if kind not in DOCUMENT_KINDS:
        raise DocumentEngineError(f"Unknown document kind: {kind}")
    candidates = (
        db.query(PlatformDocumentTemplate)
        .filter(
            PlatformDocumentTemplate.kind == kind,
            PlatformDocumentTemplate.active.is_(True),
        )
        .all()
    )
    return pick_template(candidates, product_id=product_id, vendor_id=vendor_id)


def latest_active_template(db: Session, kind: str) -> Optional[PlatformDocumentTemplate]:
    """Highest active GLOBAL version of ``kind`` (borrower T&Cs / privacy)."""
    if kind not in DOCUMENT_KINDS:
        raise DocumentEngineError(f"Unknown document kind: {kind}")
    return (
        db.query(PlatformDocumentTemplate)
        .filter(
            PlatformDocumentTemplate.kind == kind,
            PlatformDocumentTemplate.scope == "global",
            PlatformDocumentTemplate.active.is_(True),
        )
        .order_by(PlatformDocumentTemplate.version.desc())
        .first()
    )


def _load_merge_entities(
    db: Session, loan: PlatformLoan
) -> tuple[
    Optional[PlatformCreditApplication],
    Optional[PlatformPatient],
    Optional[PlatformCreditProduct],
    Optional[Any],
]:
    """Load the application/patient/product/vendor around a loan (best-effort:
    a migrated loan without an application still renders, with blanks)."""
    application: Optional[PlatformCreditApplication] = None
    patient: Optional[PlatformPatient] = None
    product: Optional[PlatformCreditProduct] = None
    vendor: Optional[Any] = None

    if loan.application_id is not None:
        application = (
            db.query(PlatformCreditApplication)
            .filter(PlatformCreditApplication.id == loan.application_id)
            .first()
        )
    if application is not None:
        patient = (
            db.query(PlatformPatient)
            .filter(PlatformPatient.id == application.patient_id)
            .first()
        )
        if application.credit_product_id is not None:
            product = (
                db.query(PlatformCreditProduct)
                .filter(PlatformCreditProduct.id == application.credit_product_id)
                .first()
            )
        if application.vendor_id is not None:
            from app.models.loan import Vendor

            vendor = db.query(Vendor).filter(Vendor.id == application.vendor_id).first()
    if patient is None and loan.patient_id is not None:
        patient = (
            db.query(PlatformPatient)
            .filter(PlatformPatient.id == loan.patient_id)
            .first()
        )
    return application, patient, product, vendor


def render_standalone_document(
    template: PlatformDocumentTemplate,
) -> RenderResult:
    """Render a loan-independent document (T&Cs, privacy) — company fields
    resolve, loan/borrower fields render empty."""
    return render_template(template.body_html, build_scalar_context(), tables={})


def generate_loan_document(
    db: Session,
    loan: PlatformLoan,
    kind: str,
    *,
    generated_via: str = "on_demand",
    created_by: Optional[UUID] = None,
    extra_context: Optional[dict[str, Any]] = None,
    commit: bool = True,
) -> PlatformLoanDocument:
    """Generate + persist a frozen document of ``kind`` for ``loan``.

    Resolves the applicable template (vendor > product > global), builds the
    merge context from the loan graph, renders, and stores the snapshot row.
    Raises ``DocumentEngineError`` when no active template exists.
    """
    if kind not in DOCUMENT_KINDS:
        raise DocumentEngineError(f"Unknown document kind: {kind}")

    application, patient, product, vendor = _load_merge_entities(db, loan)
    template = resolve_template(
        db,
        kind,
        product_id=getattr(application, "credit_product_id", None),
        vendor_id=getattr(application, "vendor_id", None),
    )
    if template is None:
        raise DocumentEngineError(f"No active template for kind: {kind}")

    context = build_scalar_context(
        loan=loan, patient=patient, product=product, vendor=vendor, extra=extra_context
    )
    tables = build_tables(loan=loan, product=product)
    result = render_template(template.body_html, context, tables)
    if result.unknown_fields:
        logger.warning(
            "document_engine_unknown_fields",
            loan_id=str(loan.id),
            kind=kind,
            template_id=str(template.id),
            unknown_fields=list(result.unknown_fields),
        )

    doc = PlatformLoanDocument(
        loan_id=loan.id,
        kind=kind,
        template_id=template.id,
        template_version=template.version,
        title=template.title,
        body_html=result.html,
        # JSONB-safe scalar snapshot (all values are strings already).
        merge_data=context,
        generated_via=generated_via,
        created_by=created_by,
    )
    db.add(doc)
    if commit:
        db.commit()
        db.refresh(doc)
    else:
        db.flush()
    logger.info(
        "loan_document_generated",
        loan_id=str(loan.id),
        kind=kind,
        template_version=template.version,
        generated_via=generated_via,
    )
    return doc


#: Kinds generated automatically at booking (agreement the borrower will sign +
#: PAD authorization). Static snapshot per loan — regenerations are on-demand.
BOOKING_KINDS = ("loan_agreement", "pad_agreement")


def generate_booking_documents(db: Session, loan: PlatformLoan) -> list[PlatformLoanDocument]:
    """Booking hook: freeze the agreement documents for a just-booked loan.

    Idempotent per (loan, kind): if a booking snapshot of that kind already
    exists it is kept (the agreement the borrower saw must never silently
    change). Best-effort per kind — a missing template logs and skips; the
    booking itself must never fail on document generation (caller also guards).
    """
    out: list[PlatformLoanDocument] = []
    for kind in BOOKING_KINDS:
        existing = (
            db.query(PlatformLoanDocument)
            .filter(
                PlatformLoanDocument.loan_id == loan.id,
                PlatformLoanDocument.kind == kind,
                PlatformLoanDocument.generated_via == "booking",
            )
            .first()
        )
        if existing is not None:
            out.append(existing)
            continue
        try:
            out.append(
                generate_loan_document(db, loan, kind, generated_via="booking")
            )
        except Exception:
            # Best-effort per kind: a missing template, render error, or any
            # other failure logs and skips — booking must never fail on
            # document generation (this function is documented as non-raising).
            logger.warning(
                "booking_document_skipped",
                loan_id=str(loan.id),
                kind=kind,
                exc_info=True,
            )
    return out


def generate_statement_document(
    db: Session,
    loan: PlatformLoan,
    period_start: date,
    period_end: date,
    *,
    generated_via: str = "borrower",
    created_by: Optional[UUID] = None,
) -> PlatformLoanDocument:
    """On-demand account statement: reuse ``loan_servicing.generate_statement``
    for the figures (idempotent per period), render through the
    ``account_statement`` template. Download-now; emailing lands with SendGrid.
    """
    from app.services import loan_servicing

    statement = loan_servicing.generate_statement(db, loan, (period_start, period_end))
    extra = {
        "StatementPeriodStart": _date_str(statement.period_start),
        "StatementPeriodEnd": _date_str(statement.period_end),
        "StatementOpeningBalance": _money(statement.opening_balance_cents),
        "StatementClosingBalance": _money(statement.closing_balance_cents),
        "StatementPrincipalPaid": _money(statement.principal_paid_cents),
        "StatementInterestPaid": _money(statement.interest_paid_cents),
    }
    return generate_loan_document(
        db,
        loan,
        "account_statement",
        generated_via=generated_via,
        created_by=created_by,
        extra_context=extra,
    )
