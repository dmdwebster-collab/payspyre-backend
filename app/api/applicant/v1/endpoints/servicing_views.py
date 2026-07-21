"""Borrower servicing views (WS-J items 5-7 read side).

* Initial-vs-current schedule views with the closed-payments toggle (TL's
  "Initial schedule" / "Show closed payments" switches, video 11 §1.8).
* Next-payment widget (date / amount / countdown).
* Payout-figure requests (≤30 days ahead; staff task; NEVER suspends payments).
* Stage-driven status banner for the portal header.

All loan routes reuse the patient-scoped ownership boundary from ``loans.py``
(``_get_owned_loan`` — 404, never 403, for someone else's loan).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import ApplicantClaims, get_current_applicant
from app.api.applicant.v1.endpoints.loans import _get_owned_loan
from app.core.logging import get_logger
from app.db.base import get_db
from app.models.platform.borrower_portal import PlatformPayoutRequest
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import PlatformLoan
from app.services import borrower_portal

logger = get_logger(__name__)
router = APIRouter(tags=["borrower-servicing"])


# --- schemas ----------------------------------------------------------------


class ScheduleViewRow(BaseModel):
    installment_number: int
    due_date: str
    principal_cents: int
    interest_cents: int
    total_cents: int
    status: str
    paid_cents: int
    remaining_cents: int


class ScheduleView(BaseModel):
    view: str
    include_closed: bool
    rows: list[ScheduleViewRow]


class NextPayment(BaseModel):
    due_date: str
    amount_cents: int
    days_until: int
    overdue: bool
    installment_number: int


class NextPaymentResponse(BaseModel):
    next_payment: Optional[NextPayment] = None


class PayoutRequestBody(BaseModel):
    requested_payout_date: date
    note: Optional[str] = Field(None, max_length=1000)


class PayoutRequestOut(BaseModel):
    request_id: UUID
    loan_id: UUID
    requested_payout_date: date
    status: str
    quoted_amount_cents: Optional[int] = None
    response_note: Optional[str] = None
    created_at: datetime
    # Dave's mandatory messaging: an inquiry never suspends payments.
    disclaimer: str = borrower_portal.PAYOUT_DISCLAIMER


class PayoutRequestList(BaseModel):
    requests: list[PayoutRequestOut]


class BannerResponse(BaseModel):
    stage: str
    message: str
    application_id: Optional[UUID] = None
    loan_id: Optional[UUID] = None


# --- schedule views ---------------------------------------------------------


@router.get("/loans/{loan_id}/schedule-view", response_model=ScheduleView)
def get_schedule_view(
    loan_id: UUID,
    view: Literal["initial", "current"] = Query("current"),
    include_closed: bool = Query(True),
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    """The schedule tab's two presentations of the SAME plan:

    * ``initial`` — the as-agreed amortization (no payment overlay);
    * ``current`` — the actuals-adjusted live rows (status, paid, remaining),
      with ``include_closed=false`` hiding paid/waived rows.
    """
    loan = _get_owned_loan(db, loan_id, claims.patient_id)
    rows = borrower_portal.shape_schedule_rows(loan.schedule, view, include_closed)
    return ScheduleView(
        view=view,
        include_closed=include_closed if view == "current" else True,
        rows=[ScheduleViewRow(**r) for r in rows],
    )


@router.get("/loans/{loan_id}/next-payment", response_model=NextPaymentResponse)
def get_next_payment(
    loan_id: UUID,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    """Next-payment widget data (payment date, amount, "Repayment in N")."""
    loan = _get_owned_loan(db, loan_id, claims.patient_id)
    widget = borrower_portal.next_payment_widget(
        loan.schedule, datetime.now(timezone.utc).date()
    )
    return NextPaymentResponse(next_payment=NextPayment(**widget) if widget else None)


# --- payout requests --------------------------------------------------------


def _payout_out(row: PlatformPayoutRequest) -> PayoutRequestOut:
    return PayoutRequestOut(
        request_id=row.id,
        loan_id=row.loan_id,
        requested_payout_date=row.requested_payout_date,
        status=row.status,
        quoted_amount_cents=row.quoted_amount_cents,
        response_note=row.response_note,
        created_at=row.created_at,
    )


@router.post(
    "/loans/{loan_id}/payout-requests",
    response_model=PayoutRequestOut,
    status_code=status.HTTP_201_CREATED,
)
def create_payout_request(
    loan_id: UUID,
    body: PayoutRequestBody,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    """Ask for a payout figure as of a date ≤30 days out (WS-J item 5).

    Creates a STAFF task — the figure is never self-served, and scheduled
    payments are NOT suspended by the inquiry (only an actual payoff suspends
    them). Staff respond via the forward payout calculator (workstream I)."""
    loan = _get_owned_loan(db, loan_id, claims.patient_id)

    today = datetime.now(timezone.utc).date()
    problem = borrower_portal.validate_payout_date(body.requested_payout_date, today)
    if problem is not None:
        raise HTTPException(status_code=422, detail=problem)

    request = PlatformPayoutRequest(
        patient_id=claims.patient_id,
        loan_id=loan.id,
        requested_payout_date=body.requested_payout_date,
        note=body.note,
        status="open",
    )
    db.add(request)
    db.flush()
    db.add(
        PlatformEvent(
            event_type="payout_request_created",
            actor="patient",
            patient_id=claims.patient_id,
            payload={
                "v": 1,
                "actor": {"type": "patient", "id": str(claims.patient_id)},
                "patient_id": str(claims.patient_id),
                "loan_id": str(loan.id),
                "payout_request_id": str(request.id),
                "requested_payout_date": body.requested_payout_date.isoformat(),
            },
        )
    )
    db.commit()
    db.refresh(request)
    logger.info(
        "payout_request_created",
        patient_id=str(claims.patient_id),
        loan_id=str(loan.id),
        payout_request_id=str(request.id),
    )
    return _payout_out(request)


@router.get("/loans/{loan_id}/payout-requests", response_model=PayoutRequestList)
def list_payout_requests(
    loan_id: UUID,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    loan = _get_owned_loan(db, loan_id, claims.patient_id)
    rows = (
        db.query(PlatformPayoutRequest)
        .filter(
            PlatformPayoutRequest.loan_id == loan.id,
            PlatformPayoutRequest.patient_id == claims.patient_id,
        )
        .order_by(PlatformPayoutRequest.created_at.desc())
        .all()
    )
    return PayoutRequestList(requests=[_payout_out(r) for r in rows])


# --- status banner ----------------------------------------------------------


@router.get("/portal/banner", response_model=BannerResponse)
def get_banner(
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    """Stage-driven banner for the portal header (the TL top dialog Dave
    called a "good thing"): derived from the caller's most recent application
    and, once one exists, its loan's lifecycle state."""
    application = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.patient_id == claims.patient_id)
        .order_by(PlatformCreditApplication.created_at.desc())
        .first()
    )
    loan = None
    if application is not None:
        loan = (
            db.query(PlatformLoan)
            .filter(PlatformLoan.application_id == application.id)
            .first()
        )
    banner = borrower_portal.banner_for(
        application.status if application else None,
        loan.status if loan else None,
    )
    return BannerResponse(
        stage=banner["stage"],
        message=banner["message"],
        application_id=application.id if application else None,
        loan_id=loan.id if loan else None,
    )
