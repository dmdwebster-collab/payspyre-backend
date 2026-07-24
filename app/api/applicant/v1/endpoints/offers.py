"""Borrower-facing loan offers (WS-D multi-offer approvals).

The Turnkey flow Dave approved (video 02 §5 "Offers"): the underwriter creates
up to N offers; they land HERE — the borrower dashboard — for review; the
borrower picks EXACTLY ONE.

* ``GET  /applications/{id}/offers``               — open offers w/ installment
  preview + expiry (lazily expires overdue offers on read).
* ``POST /applications/{id}/offers/{offer_id}/accept`` — the retained
  acceptance-CONFIRMATION step (``confirm=true`` required): books the loan with
  the offer's terms, voids the siblings, and only THEN sends the e-sign invite
  (per-envelope SignNow cost is incurred once, on a confirmed acceptance —
  never speculatively per offer).

Auth: patient JWT + per-application scope (same as the finalize endpoints).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import (
    ApplicantClaims,
    get_current_applicant,
    require_app_scope,
)
from app.core.logging import get_logger
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.loan_offer import PlatformLoanOffer
from app.services import loan_offers
from app.services.loan_servicing import generate_amortization_schedule

logger = get_logger(__name__)
router = APIRouter(prefix="/applications", tags=["applicant-offers"])


class BorrowerOffer(BaseModel):
    offer_id: UUID
    amount_cents: int
    term_months: int
    annual_rate_bps: int
    payment_frequency: str
    estimated_installment_cents: Optional[int] = None
    start_date: Optional[date] = None
    first_due_date: Optional[date] = None
    status: str
    expires_at: datetime


class BorrowerOffersResponse(BaseModel):
    application_id: UUID
    offers: list[BorrowerOffer]


class AcceptOfferBody(BaseModel):
    # The retained confirmation step: the client must explicitly confirm the
    # acceptance — a bare POST is rejected so a mis-tap can't book a loan and
    # trigger the e-sign send.
    confirm: bool = False


class AcceptOfferResponse(BaseModel):
    application_id: UUID
    offer_id: UUID
    # ``loan_id`` / ``loan_status`` are populated on the classic path (a loan is
    # booked at acceptance). Under the activation-rework (Wave 2, flag ON) the loan
    # is booked later at activation, so acceptance returns no loan yet and both are
    # None; ``agreement_status`` then reflects the APPLICATION-level agreement.
    loan_id: Optional[UUID] = None
    loan_status: Optional[str] = None
    agreement_status: str
    message: str


def _get_application(db: Session, application_id: UUID) -> PlatformCreditApplication:
    application = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    return application


def _installment_preview(offer: PlatformLoanOffer) -> Optional[int]:
    """First-installment estimate for display (equal-payment schedule)."""
    try:
        first_due = offer.first_due_date or date.today()
        rows = generate_amortization_schedule(
            offer.amount_cents, offer.annual_rate_bps, offer.term_months, first_due
        )
        return rows[0].total_cents if rows else None
    except Exception:  # noqa: BLE001 — preview only, never blocks the listing
        return None


@router.get("/{application_id}/offers", response_model=BorrowerOffersResponse)
def list_offers(
    application_id: UUID,
    claims: ApplicantClaims = Depends(get_current_applicant),
    db: Session = Depends(get_db),
):
    """The borrower's open offers for this application (accepted one included so
    the dashboard can show what was picked)."""
    require_app_scope(application_id, claims)
    application = _get_application(db, application_id)
    # Lazy expiry on read: an offer past its expires_at is never shown as open.
    if loan_offers.sweep_application_expiry(db, application):
        db.commit()
    rows = (
        db.query(PlatformLoanOffer)
        .filter(
            PlatformLoanOffer.application_id == application_id,
            PlatformLoanOffer.status.in_(("offered", "accepted")),
        )
        .order_by(PlatformLoanOffer.created_at.asc())
        .all()
    )
    return BorrowerOffersResponse(
        application_id=application_id,
        offers=[
            BorrowerOffer(
                offer_id=r.id,
                amount_cents=r.amount_cents,
                term_months=r.term_months,
                annual_rate_bps=r.annual_rate_bps,
                payment_frequency=r.payment_frequency,
                estimated_installment_cents=_installment_preview(r),
                start_date=r.start_date,
                first_due_date=r.first_due_date,
                status=str(r.status),
                expires_at=r.expires_at,
            )
            for r in rows
        ],
    )


@router.post(
    "/{application_id}/offers/{offer_id}/accept", response_model=AcceptOfferResponse
)
def accept_offer(
    application_id: UUID,
    offer_id: UUID,
    body: AcceptOfferBody,
    claims: ApplicantClaims = Depends(get_current_applicant),
    db: Session = Depends(get_db),
):
    """Accept ONE offer (confirmation required) → books the loan with the
    offer's terms, voids the siblings, then sends the e-sign invite."""
    require_app_scope(application_id, claims)
    if not body.confirm:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Acceptance requires confirm=true — review the offer terms and confirm.",
        )
    application = _get_application(db, application_id)
    try:
        offer, loan = loan_offers.accept_offer(
            db, application, offer_id, actor=f"patient:{claims.patient_id}"
        )
    except loan_offers.OfferError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    db.commit()

    if loan is None:
        # Activation-rework (Wave 2, flag ON): no loan is booked at acceptance —
        # the agreement is now sent + signed on the APPLICATION, and the loan is
        # booked later at activation. Send the application-level agreement AFTER
        # the durable acceptance (cost control), mirroring the classic path's
        # post-commit send. Graceful no-op when SignNow is unconfigured / in
        # simulator mode; a send failure never rolls back the acceptance.
        from app.services import application_agreement

        try:
            application = application_agreement.send_agreement_for_application(
                db, application, actor=f"patient:{claims.patient_id}"
            )
        except Exception as exc:  # noqa: BLE001 — send is retryable, acceptance is done
            logger.error(
                "offer_accept_application_agreement_send_failed",
                application_id=str(application_id),
                error=str(exc),
            )
        return AcceptOfferResponse(
            application_id=application_id,
            offer_id=offer.id,
            loan_id=None,
            loan_status=None,
            agreement_status=str(application.agreement_status),
            message=(
                "Offer accepted. Your loan agreement is being prepared for signature."
                if application.agreement_status == "sent"
                else "Offer accepted."
            ),
        )

    # Classic path (flag OFF): the loan is booked. E-sign AFTER the durable
    # acceptance (cost control: one envelope, only for the picked offer). Graceful
    # no-op when SignNow is unconfigured; a send failure never rolls back it.
    from app.services import loan_lifecycle

    try:
        loan = loan_lifecycle.send_agreement(db, loan)
    except Exception as exc:  # noqa: BLE001 — send is retryable, acceptance is done
        logger.error(
            "offer_accept_agreement_send_failed",
            loan_id=str(loan.id),
            error=str(exc),
        )

    return AcceptOfferResponse(
        application_id=application_id,
        offer_id=offer.id,
        loan_id=loan.id,
        loan_status=str(loan.status),
        agreement_status=str(loan.agreement_status),
        message=(
            "Offer accepted. Your loan agreement is being prepared for signature."
            if loan.agreement_status == "sent"
            else "Offer accepted."
        ),
    )
