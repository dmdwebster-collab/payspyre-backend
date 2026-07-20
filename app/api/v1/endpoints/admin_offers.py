"""Admin multi-offer approvals (WS-D) — the "Create Loan Offers" flow.

Turnkey video 02 f85-f87: on Approve, the underwriter stacks 1..N offer blocks
("+ Add Offer") and OKs them; the offers go to the borrower dashboard for
review & acceptance. Dave: "We can create multiple different offers... someone
is thinking about putting variable amounts down and they want to review the
disclosure for different terms of different options."

Differences from the single-outcome ``POST /applications/{id}/decision``
(which is UNCHANGED): approving via offers does NOT book a loan — the loan
books when the borrower accepts exactly one offer, and the e-sign invite goes
out only then (cost control). Offers expire after ``OFFER_EXPIRY_DAYS``
(default 30); at most ``OFFER_MAX_PER_APPLICATION`` (default 3) may be open.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session, joinedload

from app.core.auth import require_roles
from app.core.config import settings
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.loan_offer import PlatformLoanOffer
from app.services import loan_offers

router = APIRouter()


def _actor_id(user) -> str:
    return str(getattr(user, "id", "") or "unknown")


def _get_application(db: Session, application_id: UUID) -> PlatformCreditApplication:
    application = (
        db.query(PlatformCreditApplication)
        .options(joinedload(PlatformCreditApplication.credit_product))
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    return application


class OfferSpecIn(BaseModel):
    amount_cents: int = Field(gt=0)
    term_months: int = Field(gt=0)
    annual_rate_bps: int = Field(ge=0, le=10_000)
    start_date: Optional[date] = None
    first_due_date: Optional[date] = None
    note: Optional[str] = None


class CreateOffersBody(BaseModel):
    offers: list[OfferSpecIn] = Field(min_length=1)


class OfferRow(BaseModel):
    offer_id: UUID
    amount_cents: int
    term_months: int
    annual_rate_bps: int
    payment_frequency: str
    start_date: Optional[date] = None
    first_due_date: Optional[date] = None
    status: str
    expires_at: datetime
    accepted_at: Optional[datetime] = None
    note: Optional[str] = None
    created_by: str
    created_at: datetime


class OffersResponse(BaseModel):
    application_id: UUID
    application_status: str
    max_offers: int
    expiry_days: int
    offers: list[OfferRow]


def _row(o: PlatformLoanOffer) -> OfferRow:
    return OfferRow(
        offer_id=o.id,
        amount_cents=o.amount_cents,
        term_months=o.term_months,
        annual_rate_bps=o.annual_rate_bps,
        payment_frequency=o.payment_frequency,
        start_date=o.start_date,
        first_due_date=o.first_due_date,
        status=str(o.status),
        expires_at=o.expires_at,
        accepted_at=o.accepted_at,
        note=o.note,
        created_by=o.created_by,
        created_at=o.created_at,
    )


def _offers_response(db: Session, application: PlatformCreditApplication) -> OffersResponse:
    rows = (
        db.query(PlatformLoanOffer)
        .filter(PlatformLoanOffer.application_id == application.id)
        .order_by(PlatformLoanOffer.created_at.asc())
        .all()
    )
    return OffersResponse(
        application_id=application.id,
        application_status=str(application.status),
        max_offers=settings.OFFER_MAX_PER_APPLICATION,
        expiry_days=settings.OFFER_EXPIRY_DAYS,
        offers=[_row(o) for o in rows],
    )


@router.post("/applications/{application_id}/offers", response_model=OffersResponse)
def create_offers(
    application_id: UUID,
    body: CreateOffersBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Approve by creating up to N offers (validated against product bounds).
    Books NO loan — the borrower's acceptance does."""
    application = _get_application(db, application_id)
    specs = [
        loan_offers.OfferSpec(
            amount_cents=o.amount_cents,
            term_months=o.term_months,
            annual_rate_bps=o.annual_rate_bps,
            start_date=o.start_date,
            first_due_date=o.first_due_date,
            note=o.note,
        )
        for o in body.offers
    ]
    try:
        loan_offers.create_offers(db, application, specs, actor=_actor_id(user))
    except loan_offers.OfferError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    db.commit()
    return _offers_response(db, application)


@router.get("/applications/{application_id}/offers", response_model=OffersResponse)
def list_offers(
    application_id: UUID,
    db: Session = Depends(get_db),
    _user=Depends(require_roles("admin", "staff")),
):
    """All offers for an application, every status (audit view)."""
    application = _get_application(db, application_id)
    if loan_offers.sweep_application_expiry(db, application):
        db.commit()
    return _offers_response(db, application)


@router.post(
    "/applications/{application_id}/offers/{offer_id}/withdraw",
    response_model=OfferRow,
)
def withdraw_offer(
    application_id: UUID,
    offer_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Withdraw one OPEN offer (audited). The borrower can no longer accept it."""
    application = _get_application(db, application_id)
    try:
        offer = loan_offers.withdraw_offer(db, application, offer_id, actor=_actor_id(user))
    except loan_offers.OfferError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    db.commit()
    return _row(offer)


# --- WS-D: AI bank-statement analysis (human-visible) ------------------------


class BankAnalysisResponse(BaseModel):
    application_id: UUID
    analyzed_at: Optional[datetime] = None
    analysis: dict


@router.get(
    "/applications/{application_id}/bank-analysis",
    response_model=BankAnalysisResponse,
)
def get_bank_analysis(
    application_id: UUID,
    db: Session = Depends(get_db),
    _user=Depends(require_roles("admin", "staff")),
):
    """The latest AI bank-statement analysis for an application.

    Computed at bank-verification time (raw Flinks data is never stored) and
    persisted as PII-free aggregates on the ``verification_completed`` event —
    this endpoint surfaces the human-visible summary + category breakdown +
    micro-lender risk flag for the underwriting workspace. Static SQL (B608).
    """
    _get_application(db, application_id)  # 404 if missing
    row = db.execute(
        text(
            """
            SELECT occurred_at, payload
            FROM platform_events
            WHERE event_type = 'verification_completed'
              AND application_id = :app_id
              AND payload -> 'rich_payload' ? 'statement_analysis'
            ORDER BY occurred_at DESC
            LIMIT 1
            """
        ),
        {"app_id": str(application_id)},
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No bank-statement analysis available for this application "
                   "(bank verification not completed, or analyzed before WS-D).",
        )
    occurred_at, payload = row
    return BankAnalysisResponse(
        application_id=application_id,
        analyzed_at=occurred_at,
        analysis=payload["rich_payload"]["statement_analysis"],
    )
