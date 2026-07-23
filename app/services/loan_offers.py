"""Multi-offer approvals (WS-D) — the underwriter→borrower offer loop.

Turnkey's "Create Loan Offers" modal (video 02, f85-f87) rebuilt Dave's way:

* An underwriter APPROVES an application by creating 1..N offers (settings
  ``OFFER_MAX_PER_APPLICATION``, default 3), each validated against the
  product's bounds (amount / term / rate / s.347 APR cap).
* The offers land on the borrower dashboard; the borrower reviews and picks
  EXACTLY ONE. The acceptance CONFIRMATION step is retained deliberately —
  the e-sign invite (a per-envelope SignNow cost) goes out only after an
  explicit confirmed acceptance, never speculatively per offer.
* Acceptance books the loan with the offer's terms (via the hardened
  ``loan_lifecycle.book_loan`` entry point) and voids the unpicked siblings.
* Unaccepted offers expire ``OFFER_EXPIRY_DAYS`` (default 30) after creation —
  swept by ``app.jobs.offer_expiry`` and lazily on read/accept.

Pure validation helpers live at the top (DB-free, unit-tested); the DB
choreography below uses row locks + the migration-058 partial unique index so
two concurrent acceptances can never both book.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import PlatformLoan
from app.models.platform.loan_offer import PlatformLoanOffer
from app.schemas.pricing_config import parse_pricing_config, quote_fees_cents
from app.services.flow_orchestrator import mark_approved, mark_offers_expired
from app.services.loan_quote import (
    CRIMINAL_RATE_CAP_BPS,
    compute_apr_bps,
    exceeds_criminal_rate,
)

logger = get_logger(__name__)

# Application statuses from which an underwriter may create offers. Terminal
# closures (rejected/withdrawn/expired) and the pre-submission states are out;
# "approved" is allowed so an underwriter can extend an additional offer while
# earlier ones are still open (the cap still applies).
OFFERABLE_STATUSES = ("verifying", "underwriting", "under_review", "approved")

# Event types (WORM audit + notification triggers).
OFFERS_CREATED_EVENT = "application_offers_created"
OFFER_ACCEPTED_EVENT = "loan_offer_accepted"
OFFER_WITHDRAWN_EVENT = "loan_offer_withdrawn"
OFFER_EXPIRED_EVENT = "loan_offer_expired"
OFFERS_ALL_EXPIRED_EVENT = "application_offers_expired"


class OfferError(ValueError):
    """Business-rule rejection; endpoints map it to 409/422."""


@dataclass(frozen=True)
class OfferSpec:
    """The terms of one proposed offer (validated before persisting)."""

    amount_cents: int
    term_months: int
    annual_rate_bps: int
    start_date: Optional[date] = None
    first_due_date: Optional[date] = None
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# Pure validation — DB-free, unit-tested
# ---------------------------------------------------------------------------


def validate_offer_spec(product: Any, spec: OfferSpec) -> list[str]:
    """Validate one offer against the product's bounds. Returns human-readable
    problems (empty = valid). PURE — ``product`` only needs
    ``min_amount_cents`` / ``max_amount_cents`` / ``pricing_config`` attributes.

    Checks (each mirrors the booking chokepoint's own guards, moved up-front so
    an underwriter is told at offer time, not at acceptance):
      * amount within the product row's min/max AND the pricing-config bounds;
      * term within term_min/max_months, and in term_options when set;
      * rate within the pricing interest band [min_rate_bps, max_rate_bps];
      * the Canadian APR (incl. cost-of-borrowing fees) below the Criminal Code
        s.347 cap.
    """
    problems: list[str] = []

    if spec.amount_cents <= 0:
        problems.append("amount_cents must be positive")
    if spec.term_months <= 0:
        problems.append("term_months must be positive")
    if spec.annual_rate_bps < 0:
        problems.append("annual_rate_bps must be >= 0")
    if problems:
        return problems

    min_amount = getattr(product, "min_amount_cents", None)
    max_amount = getattr(product, "max_amount_cents", None)
    if min_amount is not None and spec.amount_cents < min_amount:
        problems.append(
            f"amount {spec.amount_cents} is below the product minimum {min_amount}"
        )
    if max_amount is not None and spec.amount_cents > max_amount:
        problems.append(
            f"amount {spec.amount_cents} is above the product maximum {max_amount}"
        )

    cfg = parse_pricing_config(
        getattr(product, "pricing_config", None) or {}, context="offer validation"
    )
    if cfg.amount_min_cents is not None and spec.amount_cents < cfg.amount_min_cents:
        problems.append(
            f"amount {spec.amount_cents} is below the pricing minimum {cfg.amount_min_cents}"
        )
    if cfg.amount_max_cents is not None and spec.amount_cents > cfg.amount_max_cents:
        problems.append(
            f"amount {spec.amount_cents} is above the pricing maximum {cfg.amount_max_cents}"
        )
    if cfg.term_min_months is not None and spec.term_months < cfg.term_min_months:
        problems.append(
            f"term {spec.term_months}mo is below the product minimum {cfg.term_min_months}mo"
        )
    if cfg.term_max_months is not None and spec.term_months > cfg.term_max_months:
        problems.append(
            f"term {spec.term_months}mo is above the product maximum {cfg.term_max_months}mo"
        )
    if cfg.term_options and spec.term_months not in cfg.term_options:
        problems.append(
            f"term {spec.term_months}mo is not one of the product's term options {cfg.term_options}"
        )
    if cfg.interest is not None:
        if not (cfg.interest.min_rate_bps <= spec.annual_rate_bps <= cfg.interest.max_rate_bps):
            problems.append(
                f"rate {spec.annual_rate_bps}bps is outside the product band "
                f"[{cfg.interest.min_rate_bps}, {cfg.interest.max_rate_bps}]bps"
            )

    if (
        spec.start_date is not None
        and spec.first_due_date is not None
        and spec.first_due_date <= spec.start_date
    ):
        problems.append("first_due_date must be after start_date")

    # s.347 guard — same math as the booking chokepoint (defense-in-depth stays
    # there too; this makes the failure visible at offer-creation time).
    fees_cents = quote_fees_cents(cfg, spec.amount_cents, spec.term_months, "monthly")
    apr_bps = compute_apr_bps(
        spec.amount_cents, spec.annual_rate_bps, spec.term_months, "monthly", fees_cents
    )
    if exceeds_criminal_rate(apr_bps):
        problems.append(
            f"APR {apr_bps / 100:.2f}% reaches the Criminal Code s.347 cap "
            f"({CRIMINAL_RATE_CAP_BPS / 100:.0f}%)"
        )

    return problems


def is_expired(offer: Any, now: datetime) -> bool:
    """True when an ``offered`` offer's expiry has passed. PURE."""
    if getattr(offer, "status", None) != "offered":
        return False
    expires_at = getattr(offer, "expires_at", None)
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:  # defensive: naive timestamps compare as UTC
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= now


def validate_offer_count(existing_open: int, new_count: int, max_offers: int) -> Optional[str]:
    """Cap check. Returns a problem string or None. PURE."""
    if new_count < 1:
        return "at least one offer is required"
    if existing_open + new_count > max_offers:
        return (
            f"offer cap exceeded: {existing_open} open offer(s) + {new_count} new "
            f"> max {max_offers} per application"
        )
    return None


# ---------------------------------------------------------------------------
# DB choreography
# ---------------------------------------------------------------------------


def _event(
    db: Session,
    *,
    event_type: str,
    actor: str,
    application: PlatformCreditApplication,
    payload: dict,
) -> None:
    db.add(
        PlatformEvent(
            event_type=event_type,
            actor=actor,
            patient_id=application.patient_id,
            application_id=application.id,
            payload={"v": 1, "application_id": str(application.id), **payload},
        )
    )


def _open_offers(db: Session, application_id: UUID, *, lock: bool = False):
    q = db.query(PlatformLoanOffer).filter(
        PlatformLoanOffer.application_id == application_id,
        PlatformLoanOffer.status == "offered",
    )
    if lock:
        q = q.with_for_update()
    return q.all()


def create_offers(
    db: Session,
    application: PlatformCreditApplication,
    specs: list[OfferSpec],
    *,
    actor: str,
    now: Optional[datetime] = None,
) -> list[PlatformLoanOffer]:
    """Approve an application by creating offers (does NOT book a loan).

    The loan is booked only when the borrower accepts one offer — this is the
    behavioral difference from the single-outcome ``/decision`` endpoint, which
    books immediately and remains unchanged.

    Caller commits (endpoint owns the transaction).
    """
    now = now or datetime.now(timezone.utc)
    # Offer expiry / max-offers are admin-configurable via the application-process
    # config (WS W2-APPCONFIG). The config defaults equal settings.OFFER_* so an
    # unconfigured platform behaves exactly as before this workstream.
    from app.services.application_process_config import effective_offer_policy

    offer_policy = effective_offer_policy(db)
    max_offers = offer_policy.max_offers
    expiry_days = offer_policy.expiry_days

    if application.status not in OFFERABLE_STATUSES:
        raise OfferError(
            f"cannot create offers for an application in status '{application.status}'"
        )
    existing_loan = (
        db.query(PlatformLoan.id)
        .filter(PlatformLoan.application_id == application.id)
        .first()
    )
    if existing_loan is not None:
        raise OfferError(
            f"application already has a booked loan ({existing_loan[0]}); offers are closed"
        )

    open_offers = _open_offers(db, application.id)
    cap_problem = validate_offer_count(len(open_offers), len(specs), max_offers)
    if cap_problem:
        raise OfferError(cap_problem)

    product = application.credit_product
    all_problems: dict[int, list[str]] = {}
    for i, spec in enumerate(specs):
        problems = validate_offer_spec(product, spec)
        if problems:
            all_problems[i] = problems
    if all_problems:
        raise OfferError(f"invalid offer terms: {all_problems}")

    expires_at = now + timedelta(days=expiry_days)
    offers: list[PlatformLoanOffer] = []
    for spec in specs:
        offer = PlatformLoanOffer(
            application_id=application.id,
            credit_product_id=application.credit_product_id,
            amount_cents=spec.amount_cents,
            term_months=spec.term_months,
            annual_rate_bps=spec.annual_rate_bps,
            payment_frequency="monthly",
            start_date=spec.start_date,
            first_due_date=spec.first_due_date,
            status="offered",
            expires_at=expires_at,
            note=spec.note,
            created_by=actor,
        )
        db.add(offer)
        offers.append(offer)
    db.flush()

    before_status = application.status
    mark_approved(application)
    application.status_updated_at = now
    application.decision = {
        "outcome": "approved",
        "by": "admin",
        "mode": "multi_offer",
        "offer_ids": [str(o.id) for o in offers],
        "note": None,
    }
    application.decision_by = actor
    application.decision_at = now
    application.vendor_reprocessing_requested = False

    _event(
        db,
        event_type=OFFERS_CREATED_EVENT,
        actor=actor,
        application=application,
        payload={
            "before": {"status": before_status},
            "after": {"status": "approved"},
            "offers": [
                {
                    "offer_id": str(o.id),
                    "amount_cents": o.amount_cents,
                    "term_months": o.term_months,
                    "annual_rate_bps": o.annual_rate_bps,
                    "expires_at": o.expires_at.isoformat(),
                }
                for o in offers
            ],
        },
    )
    logger.info(
        "loan_offers_created",
        application_id=str(application.id),
        offer_count=len(offers),
        actor=actor,
    )
    return offers


def sweep_application_expiry(
    db: Session,
    application: PlatformCreditApplication,
    *,
    now: Optional[datetime] = None,
) -> int:
    """Lazily expire this application's overdue offers (read-path hook).

    Returns the number of offers flipped. Caller commits.
    """
    now = now or datetime.now(timezone.utc)
    flipped = 0
    for offer in _open_offers(db, application.id):
        if is_expired(offer, now):
            offer.status = "expired"
            flipped += 1
            _event(
                db,
                event_type=OFFER_EXPIRED_EVENT,
                actor="system",
                application=application,
                payload={"offer_id": str(offer.id), "expires_at": offer.expires_at.isoformat()},
            )
    if flipped:
        _maybe_expire_application(db, application, now=now)
    return flipped


def accept_offer(
    db: Session,
    application: PlatformCreditApplication,
    offer_id: UUID,
    *,
    actor: str,
    now: Optional[datetime] = None,
) -> tuple[PlatformLoanOffer, PlatformLoan]:
    """Borrower accepts ONE offer → siblings void → loan books with its terms.

    Row-locks the application's offers so two concurrent acceptances serialize;
    the partial unique index (058) backstops. ``book_loan`` commits — after
    this returns, the acceptance is durable. The e-sign invite is the CALLER's
    post-commit step (cost control: send only after a confirmed acceptance).
    """
    now = now or datetime.now(timezone.utc)

    offers = (
        db.query(PlatformLoanOffer)
        .filter(PlatformLoanOffer.application_id == application.id)
        .with_for_update()
        .all()
    )
    target = next((o for o in offers if o.id == offer_id), None)
    if target is None:
        raise OfferError("offer not found for this application")

    if any(o.status == "accepted" for o in offers):
        raise OfferError("an offer has already been accepted for this application")
    if target.status != "offered":
        raise OfferError(f"offer is not open (status '{target.status}')")
    if is_expired(target, now):
        target.status = "expired"
        _event(
            db,
            event_type=OFFER_EXPIRED_EVENT,
            actor="system",
            application=application,
            payload={"offer_id": str(target.id), "expires_at": target.expires_at.isoformat()},
        )
        raise OfferError("offer has expired")

    target.status = "accepted"
    target.accepted_at = now
    voided: list[str] = []
    for sibling in offers:
        if sibling.id != target.id and sibling.status == "offered":
            sibling.status = "void"
            voided.append(str(sibling.id))

    # Merge the accepted terms into the decision record so the UNCHANGED booking
    # path (loan_servicing._resolve_pricing reads decision.amount_cents /
    # apr_bps / term_months) books exactly these terms — no booking-code fork.
    decision = dict(application.decision or {})
    decision.update(
        {
            "outcome": "approved",
            "amount_cents": target.amount_cents,
            "apr_bps": target.annual_rate_bps,
            "term_months": target.term_months,
            "accepted_offer_id": str(target.id),
        }
    )
    application.decision = decision
    if application.status != "approved":
        mark_approved(application)
        application.status_updated_at = now

    _event(
        db,
        event_type=OFFER_ACCEPTED_EVENT,
        actor=actor,
        application=application,
        payload={
            "offer_id": str(target.id),
            "voided_offer_ids": voided,
            "amount_cents": target.amount_cents,
            "term_months": target.term_months,
            "annual_rate_bps": target.annual_rate_bps,
        },
    )
    db.flush()

    from app.services import loan_lifecycle  # local import — avoids cycle at import time

    loan = loan_lifecycle.book_loan(db, application, first_due_date=target.first_due_date)
    logger.info(
        "loan_offer_accepted",
        application_id=str(application.id),
        offer_id=str(target.id),
        loan_id=str(loan.id),
    )
    return target, loan


def withdraw_offer(
    db: Session,
    application: PlatformCreditApplication,
    offer_id: UUID,
    *,
    actor: str,
) -> PlatformLoanOffer:
    """Staff withdraw an open offer (audited). Caller commits."""
    offer = (
        db.query(PlatformLoanOffer)
        .filter(
            PlatformLoanOffer.id == offer_id,
            PlatformLoanOffer.application_id == application.id,
        )
        .with_for_update()
        .first()
    )
    if offer is None:
        raise OfferError("offer not found for this application")
    if offer.status != "offered":
        raise OfferError(f"only open offers can be withdrawn (status '{offer.status}')")
    offer.status = "withdrawn"
    _event(
        db,
        event_type=OFFER_WITHDRAWN_EVENT,
        actor=actor,
        application=application,
        payload={"offer_id": str(offer.id)},
    )
    return offer


def _maybe_expire_application(
    db: Session,
    application: PlatformCreditApplication,
    *,
    now: datetime,
) -> bool:
    """When EVERY offer is terminal without an acceptance and no loan exists,
    the approval lapses: application → ``expired`` (existing enum value)."""
    if application.status != "approved":
        return False
    statuses = [
        s
        for (s,) in db.query(PlatformLoanOffer.status)
        .filter(PlatformLoanOffer.application_id == application.id)
        .all()
    ]
    if not statuses or any(s in ("offered", "accepted") for s in statuses):
        return False
    has_loan = (
        db.query(PlatformLoan.id)
        .filter(PlatformLoan.application_id == application.id)
        .first()
        is not None
    )
    if has_loan:
        return False
    before = application.status
    mark_offers_expired(application)
    application.status_updated_at = now
    _event(
        db,
        event_type=OFFERS_ALL_EXPIRED_EVENT,
        actor="system",
        application=application,
        payload={"before": {"status": before}, "after": {"status": "expired"}},
    )
    logger.info("application_offers_expired", application_id=str(application.id))
    return True


@dataclass
class ExpirySweepResult:
    offers_expired: int = 0
    applications_expired: int = 0


def expire_offers(db: Session, *, now: Optional[datetime] = None) -> ExpirySweepResult:
    """The scheduled sweep (``python -m app.jobs.offer_expiry``): flip every
    overdue ``offered`` row to ``expired`` and lapse fully-expired approvals.
    Idempotent — an already-expired offer never matches again. Caller commits.
    """
    now = now or datetime.now(timezone.utc)
    result = ExpirySweepResult()
    overdue = (
        db.query(PlatformLoanOffer)
        .filter(
            PlatformLoanOffer.status == "offered",
            PlatformLoanOffer.expires_at <= now,
        )
        .with_for_update(skip_locked=True)
        .all()
    )
    touched_app_ids: set[UUID] = set()
    for offer in overdue:
        offer.status = "expired"
        result.offers_expired += 1
        touched_app_ids.add(offer.application_id)

    for app_id in touched_app_ids:
        application = (
            db.query(PlatformCreditApplication)
            .filter(PlatformCreditApplication.id == app_id)
            .first()
        )
        if application is None:
            continue
        _event(
            db,
            event_type=OFFER_EXPIRED_EVENT,
            actor="system",
            application=application,
            payload={"swept_at": now.isoformat()},
        )
        if _maybe_expire_application(db, application, now=now):
            result.applications_expired += 1
    return result
