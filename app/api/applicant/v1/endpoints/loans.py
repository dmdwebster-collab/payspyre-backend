"""Borrower portal — read-only loan-servicing API (Borrower Portal §4).

A logged-in borrower (patient) can see and understand their loan(s): balance,
schedule, payments, statements. Every route is patient-JWT authed via
``get_current_applicant`` and **scoped to ``claims.patient_id``** by joining
``platform_loans.application_id -> platform_credit_applications.patient_id``.

Scoping is the security boundary (spec §4): a loan whose patient != the caller
is **404, not 403** — we never reveal that someone else's loan exists. Migrated
loans (``application_id IS NULL``) resolve to no patient and so never surface.

The days-past-due / next-due computation mirrors the clinic loan-book
(``dashboard_loanbook.py``) for spec parity — same definitions, patient-scoped
queries instead of vendor-scoped. Money is integer cents throughout;
``annual_rate_bps`` is basis points (1% = 100 bps).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload

from app.api.applicant.v1.deps import (
    ApplicantClaims,
    get_current_applicant,
    require_step_up,
)
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.borrower_portal import PlatformPatientBankAccount
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.loan import (
    PlatformCollectionAttempt,
    PlatformLoan,
    PlatformLoanPayment,
    PlatformLoanScheduleItem,
    PlatformLoanStatement,
    PlatformLoanTransaction,
)
from app.services import borrower_portal, loan_payments

router = APIRouter(tags=["borrower-loans"])


# ---------------------------------------------------------------------------
# Response schemas — local to this module (DO NOT touch shared schemas).
# Field names mirror the TS contract in the spec (§4).
# ---------------------------------------------------------------------------


class BorrowerLoanSummary(BaseModel):
    loan_id: UUID
    status: str
    principal_cents: int
    principal_balance_cents: int
    currency: str
    disbursed_at: Optional[datetime] = None
    next_due_date: Optional[date] = None
    next_due_cents: Optional[int] = None
    days_past_due: int


class BorrowerLoans(BaseModel):
    loans: list[BorrowerLoanSummary]


class BorrowerLoanDetail(BorrowerLoanSummary):
    annual_rate_bps: int
    apr_percent: float
    term_months: int
    agreement_status: str
    disbursement_status: str
    amount_paid_cents: int
    installments_total: int
    installments_paid: int
    product_name: Optional[str] = None
    # "Which clinic is this loan with?" (video 11 §1.9's Vendor/provider cell).
    # The platform has ONE vendors entity, so vendor = business_name and the
    # provider is the application's free-text ``provider_name`` (there is no
    # Provider entity yet — it stays None when the vendor never named one).
    vendor_id: Optional[UUID] = None
    vendor_name: Optional[str] = None
    vendor_dba_name: Optional[str] = None
    provider_name: Optional[str] = None


class ScheduleRow(BaseModel):
    installment_number: int
    due_date: date
    principal_cents: int
    interest_cents: int
    # Fee component of the installment = total - principal - interest (the
    # schedule model stores exactly those three money columns, so this is an
    # identity, not an allocation guess). SINGLE figure — the schedule layer
    # does not itemize fees into buckets, so TL's per-row fee dropdown has no
    # data behind it (see borrower_portal.schedule_fee_cents).
    fee_cents: int
    total_cents: int
    paid_cents: int
    status: str


class LoanSchedule(BaseModel):
    rows: list[ScheduleRow]


class PaymentRow(BaseModel):
    """One row of the borrower's Transactions tab (video 11 §1.10).

    Dave's eight columns map as:
      Status → ``status`` · Date → ``effective_date`` · Total → ``amount_cents``
      · Principal → ``principal_cents`` · Interest → ``interest_cents`` · Fee →
      ``fee_total_cents`` · Repayment mode → ``repayment_mode_label`` ·
      Repayment method → ``repayment_method_label``.

    Every money figure is the POSTED ledger allocation, read as written by the
    actuals engine — never recomputed here.
    """

    # --- pre-existing contract (unchanged) ---------------------------------
    amount_cents: int
    received_at: datetime
    method: str

    # --- ledger allocation (None only on the pre-ledger fallback path) -----
    status: str  # posted | reversed | reversal
    effective_date: Optional[date] = None
    principal_cents: Optional[int] = None
    interest_cents: Optional[int] = None
    fees_cents: Optional[int] = None
    add_on_cents: Optional[int] = None
    fee_total_cents: Optional[int] = None  # fees_cents + add_on_cents

    # --- mode / method -----------------------------------------------------
    repayment_mode: Optional[str] = None  # regular | add_on | special | payoff
    repayment_mode_label: str = "Payment"
    is_automatic: bool = False
    repayment_method: Optional[str] = None  # cash | check | eft | credit_card | adjustment
    repayment_method_label: str = ""

    # --- provenance --------------------------------------------------------
    txn_type: str = "receipt"  # payment | adjustment | reversal | receipt
    reference: Optional[str] = None
    reverses_reference: Optional[str] = None
    reversed_on: Optional[date] = None
    allocation_source: str = "ledger"  # ledger | unallocated


class LoanPayments(BaseModel):
    rows: list[PaymentRow]


class StatementRow(BaseModel):
    period_start: date
    period_end: date
    opening_balance_cents: int
    closing_balance_cents: int
    principal_paid_cents: int
    interest_paid_cents: int
    generated_at: datetime


class LoanStatements(BaseModel):
    rows: list[StatementRow]


# ---------------------------------------------------------------------------
# Pure helpers (mirror dashboard_loanbook.py for spec parity)
# ---------------------------------------------------------------------------


def _next_due_item(
    schedule_items: Iterable[PlatformLoanScheduleItem],
) -> Optional[PlatformLoanScheduleItem]:
    """Next payment (spec §6): earliest schedule row whose status is not in
    {paid, waived}, by ``installment_number``."""
    outstanding = [s for s in schedule_items if s.status not in ("paid", "waived")]
    if not outstanding:
        return None
    return min(outstanding, key=lambda s: s.installment_number)


def _days_past_due(
    next_item: Optional[PlatformLoanScheduleItem], as_of: date
) -> int:
    """days_past_due (spec §6) = ``max(0, today - next-payment due_date)``.

    The next payment is the earliest unpaid installment; if it's not yet due
    (or there is none), the loan is current -> 0.
    """
    if next_item is None:
        return 0
    return max(0, (as_of - next_item.due_date).days)


def _to_summary(loan: PlatformLoan, as_of: date) -> BorrowerLoanSummary:
    nxt = _next_due_item(loan.schedule)
    return BorrowerLoanSummary(
        loan_id=loan.id,
        status=loan.status,
        principal_cents=loan.principal_cents,
        principal_balance_cents=loan.principal_balance_cents,
        currency=loan.currency,
        disbursed_at=loan.disbursed_at,
        next_due_date=nxt.due_date if nxt else None,
        next_due_cents=(nxt.total_cents - nxt.paid_cents) if nxt else None,
        days_past_due=_days_past_due(nxt, as_of),
    )


# ---------------------------------------------------------------------------
# Scoping helpers — the ONLY path to a loan is patient-scoped.
# ---------------------------------------------------------------------------


def _patient_loans_query(db: Session, patient_id: UUID):
    """Base query for ONE patient's loans, scoped via the application join.

    The join to ``platform_credit_applications`` + the ``patient_id`` filter is
    the ONLY scoping boundary (loans carry no patient_id) — every caller starts
    here so scoping can't be forgotten. Migrated loans (application_id NULL)
    never match the inner join and so never surface.
    """
    return (
        db.query(PlatformLoan)
        .join(
            PlatformCreditApplication,
            PlatformLoan.application_id == PlatformCreditApplication.id,
        )
        .filter(PlatformCreditApplication.patient_id == patient_id)
    )


def _get_owned_loan(db: Session, loan_id: UUID, patient_id: UUID) -> PlatformLoan:
    """Fetch a loan owned by ``patient_id`` or raise 404.

    404 (not 403) by design (spec §4): we don't reveal that a loan exists when
    it belongs to someone else. A nonexistent loan and a not-yours loan are
    indistinguishable to the caller.
    """
    loan = (
        _patient_loans_query(db, patient_id)
        .options(joinedload(PlatformLoan.schedule))
        .filter(PlatformLoan.id == loan_id)
        .first()
    )
    if loan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Loan not found")
    return loan


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/loans", response_model=BorrowerLoans)
def list_loans(
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
) -> BorrowerLoans:
    """The caller's loans (spec §4.1). Scoped to ``claims.patient_id``."""
    loans = (
        _patient_loans_query(db, claims.patient_id)
        .options(joinedload(PlatformLoan.schedule))
        .order_by(PlatformLoan.created_at.desc(), PlatformLoan.id.desc())
        .all()
    )
    as_of = datetime.now(timezone.utc).date()
    return BorrowerLoans(loans=[_to_summary(loan, as_of) for loan in loans])


@router.get("/loans/{loan_id}", response_model=BorrowerLoanDetail)
def get_loan(
    loan_id: UUID,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
) -> BorrowerLoanDetail:
    """Full detail for one owned loan (spec §4.2). 404 if not the caller's."""
    loan = _get_owned_loan(db, loan_id, claims.patient_id)
    as_of = datetime.now(timezone.utc).date()
    summary = _to_summary(loan, as_of)

    amount_paid_cents = sum(p.amount_cents for p in loan.payments)
    installments_total = len(loan.schedule)
    installments_paid = sum(1 for s in loan.schedule if s.status == "paid")

    product_name: Optional[str] = None
    vendor_id: Optional[UUID] = None
    vendor_name: Optional[str] = None
    vendor_dba_name: Optional[str] = None
    provider_name: Optional[str] = None
    if loan.application_id is not None:
        product_name = (
            db.query(PlatformCreditProduct.name)
            .join(
                PlatformCreditApplication,
                PlatformCreditApplication.credit_product_id == PlatformCreditProduct.id,
            )
            .filter(PlatformCreditApplication.id == loan.application_id)
            .scalar()
        )
        # Vendor / provider (B2). LEFT join: an application without a vendor
        # (direct-to-consumer origination) still returns its provider_name.
        origin = (
            db.query(
                PlatformCreditApplication.vendor_id,
                PlatformCreditApplication.provider_name,
                Vendor.business_name,
                Vendor.dba_name,
            )
            .outerjoin(Vendor, Vendor.id == PlatformCreditApplication.vendor_id)
            .filter(PlatformCreditApplication.id == loan.application_id)
            .first()
        )
        if origin is not None:
            vendor_id, provider_name, vendor_name, vendor_dba_name = origin

    return BorrowerLoanDetail(
        **summary.model_dump(),
        annual_rate_bps=loan.annual_rate_bps,
        apr_percent=loan.annual_rate_bps / 100,
        term_months=loan.term_months,
        agreement_status=loan.agreement_status,
        disbursement_status=loan.disbursement_status,
        amount_paid_cents=amount_paid_cents,
        installments_total=installments_total,
        installments_paid=installments_paid,
        product_name=product_name,
        vendor_id=vendor_id,
        vendor_name=vendor_name,
        vendor_dba_name=vendor_dba_name,
        provider_name=provider_name,
    )


@router.get("/loans/{loan_id}/schedule", response_model=LoanSchedule)
def get_schedule(
    loan_id: UUID,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
) -> LoanSchedule:
    """Amortization schedule (spec §4.3), ordered by installment_number."""
    _get_owned_loan(db, loan_id, claims.patient_id)  # 404 if not owned
    rows = (
        db.query(PlatformLoanScheduleItem)
        .filter(PlatformLoanScheduleItem.loan_id == loan_id)
        .order_by(PlatformLoanScheduleItem.installment_number.asc())
        .all()
    )
    return LoanSchedule(
        rows=[
            ScheduleRow(
                installment_number=r.installment_number,
                due_date=r.due_date,
                principal_cents=r.principal_cents,
                interest_cents=r.interest_cents,
                fee_cents=borrower_portal.schedule_fee_cents(r),
                total_cents=r.total_cents,
                paid_cents=r.paid_cents,
                status=r.status,
            )
            for r in rows
        ]
    )


@router.get("/loans/{loan_id}/payments", response_model=LoanPayments)
def get_payments(
    loan_id: UUID,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
) -> LoanPayments:
    """The Transactions tab (spec §4.4 / video 11 §1.10), most-recent first.

    Reads the IMMUTABLE LEDGER (``platform_loan_transactions``) — the system of
    record for how each payment was applied. The principal / interest / fee
    figures are the allocations the actuals engine POSTED; this endpoint never
    recomputes or replays them.

    Reversals: a reversed payment is NOT dropped. The original comes back with
    ``status="reversed"`` (+ ``reversed_on``), and the compensating row comes
    back as its own ``status="reversal"`` carrying the amounts it undoes.

    Pre-ledger loans (cash receipts written before migration 049) fall back to
    ``platform_loan_payments`` with ``allocation_source="unallocated"`` and NULL
    allocations — an honest "we don't have a posted split" rather than a guess.
    """
    _get_owned_loan(db, loan_id, claims.patient_id)  # 404 if not owned
    ledger = (
        db.query(PlatformLoanTransaction)
        .filter(PlatformLoanTransaction.loan_id == loan_id)
        .order_by(PlatformLoanTransaction.seq.asc())
        .all()
    )
    receipts: list[PlatformLoanPayment] = []
    automatic_payment_ids: set = set()
    if not ledger:
        receipts = (
            db.query(PlatformLoanPayment)
            .filter(PlatformLoanPayment.loan_id == loan_id)
            .order_by(PlatformLoanPayment.received_at.desc())
            .all()
        )
        # Which of those receipts settled an AUTO-collection attempt (the rail
        # ref is shared): TL's "Automatic payment" vs a borrower-made one.
        refs = [p.external_ref for p in receipts if p.external_ref]
        if refs:
            auto_refs = {
                row[0]
                for row in db.query(PlatformCollectionAttempt.external_ref)
                .filter(
                    PlatformCollectionAttempt.loan_id == loan_id,
                    PlatformCollectionAttempt.external_ref.in_(refs),
                )
                .all()
            }
            automatic_payment_ids = {
                p.id for p in receipts if p.external_ref in auto_refs
            }
    return LoanPayments(
        rows=[
            PaymentRow(**row)
            for row in borrower_portal.shape_transaction_rows(
                ledger, receipts, automatic_payment_ids
            )
        ]
    )


@router.get("/loans/{loan_id}/statements", response_model=LoanStatements)
def get_statements(
    loan_id: UUID,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
) -> LoanStatements:
    """Periodic statements (spec §4.5), oldest period first."""
    _get_owned_loan(db, loan_id, claims.patient_id)  # 404 if not owned
    rows = (
        db.query(PlatformLoanStatement)
        .filter(PlatformLoanStatement.loan_id == loan_id)
        .order_by(PlatformLoanStatement.period_start.asc())
        .all()
    )
    return LoanStatements(
        rows=[
            StatementRow(
                period_start=s.period_start,
                period_end=s.period_end,
                opening_balance_cents=s.opening_balance_cents,
                closing_balance_cents=s.closing_balance_cents,
                principal_paid_cents=s.principal_paid_cents,
                interest_paid_cents=s.interest_paid_cents,
                generated_at=s.generated_at,
            )
            for s in rows
        ]
    )


# ---------------------------------------------------------------------------
# Pay Now — borrower-initiated payment (Zumrails collection). Merged in from the
# notification/Pay-Now work (PR #118); shares this module's patient-scoped
# ownership check. Settlement happens out-of-band via the Zumrails webhook.
# ---------------------------------------------------------------------------


class PayNowBody(BaseModel):
    # Optional because a PAYOFF amount is server-computed (non-editable): omit
    # it (or echo the quote from /payment-options to confirm). Required and
    # editable for regular; required and capped for add_on.
    amount_cents: Optional[int] = Field(
        None, gt=0, description="Amount to pay, in cents (omitted for payoff)"
    )
    # WS-F borrower modes. ``special`` is deliberately NOT offered here
    # (staff-only, permission-gated); ``add_on`` is valid only while the loan
    # carries an add-on balance (see /payment-options).
    mode: Literal["regular", "add_on", "payoff"] = "regular"


class PayNowResponse(BaseModel):
    transaction_id: str
    status: str
    amount_cents: int
    # Defaulted, not required: the pre-WS-F contract (3 fields) must keep
    # working unchanged for existing callers/stubs of the service layer; the
    # real service always populates the mode.
    repayment_mode: str = "regular"


class PaymentMethodOut(BaseModel):
    """The DEFAULT verified bank account, read-only (WS-J item 4): Pay Now is
    locked to it — the modal displays it and never offers a choice."""

    institution_name: str
    currency: str
    routing_mask: Optional[str] = None
    account_mask: str


class PaymentOptions(BaseModel):
    as_of: date
    modes: list[str]
    outstanding_cents: int
    add_on_balance_cents: int
    payoff_cents: int
    # None until a default verified account exists (Flinks link or staff add).
    # Defaulted so pre-WS-J callers/stubs of the service layer keep working.
    payment_method: Optional[PaymentMethodOut] = None


@router.get("/loans/{loan_id}/payment-options", response_model=PaymentOptions)
def get_payment_options(
    loan_id: UUID,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
) -> PaymentOptions:
    """The borrower's Pay-Now options (WS-F): available repayment modes (an
    add-on option only when the add-on balance is positive) + the SERVER-QUOTED
    payoff amount (non-editable — quote here, confirm in POST /payments).
    WS-J: also surfaces the DEFAULT verified bank account read-only — the
    payment method is locked to it (not selectable in the modal)."""
    loan = _get_owned_loan(db, loan_id, claims.patient_id)
    default_account = (
        db.query(PlatformPatientBankAccount)
        .filter(
            PlatformPatientBankAccount.patient_id == claims.patient_id,
            PlatformPatientBankAccount.status == "active",
            PlatformPatientBankAccount.is_default.is_(True),
        )
        .first()
    )
    payment_method = (
        PaymentMethodOut(
            institution_name=default_account.institution_name,
            currency=default_account.currency,
            routing_mask=default_account.routing_mask,
            account_mask=default_account.account_mask,
        )
        if default_account
        else None
    )
    return PaymentOptions(
        **loan_payments.payment_options(db, loan), payment_method=payment_method
    )


@router.post(
    "/loans/{loan_id}/payments",
    response_model=PayNowResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def pay_now(
    loan_id: UUID,
    body: PayNowBody,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(require_step_up),
) -> PayNowResponse:
    """Initiate a borrower payment (Zumrails collection). Patient-scoped (404 if
    not owned). The payment settles out-of-band when the Zumrails webhook
    confirms it; this returns the in-flight transaction so the UI can poll.
    WS-F: ``mode`` selects regular (editable amount) / add_on (only while an
    add-on balance exists) / payoff (server-quoted, non-editable).
    WS-J: payments are a SENSITIVE action — ``require_step_up`` demands a 2FA
    step-up token for enrolled patients (additive no-op for everyone else)."""
    loan = _get_owned_loan(db, loan_id, claims.patient_id)
    try:
        result = loan_payments.initiate_payment(
            db, loan, body.amount_cents, mode=body.mode
        )
    except loan_payments.PaymentValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except loan_payments.PaymentProviderUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    return PayNowResponse(**result)
