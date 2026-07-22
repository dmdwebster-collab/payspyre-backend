"""Loan lifecycle state machine — wires the standalone LMS pieces together.

Migration 026 / ``loan_servicing.py`` gave us the loan *spine* (book a loan +
amortization schedule). The adapters (``SignNowAdapter``, ``ZumrailsAdapter``)
gave us the rails. This module is the *lifecycle* that connects them:

    approved application
        │  book_loan()
        ▼
    PlatformLoan(status=pending_disbursement,
                 agreement_status=not_sent, disbursement_status=not_started)
        │  send_agreement()                 → SignNow invite
        ▼
    agreement_status=sent (agreement_ref = SignNow document id)
        │  on_agreement_signed()            ← SignNow "document.complete" webhook
        ▼  triggers Zumrails disbursement
    agreement_status=signed,
    disbursement_status=in_progress (disbursement_ref = Zumrails txn id)
        │  on_disbursement_complete()       ← Zumrails "completed" webhook
        ▼
    status=active, disbursed_at set, disbursement_status=completed

DESIGN RULES
------------
* **Forward-only.** Each transition checks the current state and is a no-op
  (returns the loan unchanged) if it has already happened or if it would move
  backward. This makes every entry point safe to call from an at-least-once
  webhook or a manual retry without double-acting.
* **Idempotent.** Re-delivering the same SignNow/Zumrails webhook re-enters the
  same method and short-circuits.
* **Graceful degradation.** Adapters are *constructed from*
  ``integration_settings.get(db, provider)``. If a provider row is missing or
  ``enabled is False`` (or lacks credentials), the lifecycle LOGS and leaves the
  loan in its current pending state rather than raising — booking still works
  with the integrations turned off, and the operator wires them later.
* **No business/decision logic.** Pricing + the amortization schedule are owned
  by ``loan_servicing.create_loan_from_application``; this module only sequences
  side effects and flips lifecycle flags.

Credentials are read here (the wiring layer) and *injected* into the adapters —
the adapters themselves never touch ``integration_settings`` (keeps them unit
testable). For tests, adapters can be injected directly via ``signnow=`` /
``zumrails=`` so no DB/network is needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import PlatformLoan
from app.services import archive
from app.services import integration_settings
from app.services import loan_servicing
from app.services.esign.signnow_adapter import SignerInput
from app.services.payments.zumrails_adapter import (
    TransactionStatus,
    ZumrailsError,
)

logger = get_logger(__name__)


# Provider slugs in platform_integration_settings (see KNOWN_PROVIDERS).
_SIGNNOW_PROVIDER = "signnow"
_ZUMRAILS_PROVIDER = "zumrails"

# How long a disbursement may sit in ``in_progress`` before reconciliation will
# poll Zumrails for it. Tuned conservatively: EFT settlement and the terminal
# webhook normally land well inside a day, so anything older is "stuck" and
# worth an active status check rather than waiting forever on a webhook.
_DEFAULT_RECONCILE_AFTER = timedelta(hours=6)


# ---------------------------------------------------------------------------
# Money-movement audit trail
# ---------------------------------------------------------------------------
# Until now only the decision + adverse-action flows wrote PlatformEvent rows, so
# the LMS money path (book → disburse) left no auditable history. These event
# types record each money-moving lifecycle transition. Names mirror the structlog
# events for grep-ability; the rows carry only ids + amounts (cents) + status —
# never PII.
LOAN_BOOKED_EVENT = "loan_booked"
LOAN_DISBURSEMENT_INITIATED_EVENT = "loan_disbursement_initiated"
LOAN_DISBURSED_EVENT = "loan_disbursed"
LOAN_DISBURSEMENT_FAILED_EVENT = "loan_disbursement_failed"
LOAN_CHARGED_OFF_EVENT = "loan_charged_off"
# Agreement (e-sign) lifecycle — recorded so the notification processor can
# send the borrower-facing "signature required" / "agreement signed" emails
# (Dave's Customer notification spec) off the same event stream.
LOAN_AGREEMENT_SENT_EVENT = "loan_agreement_sent"
LOAN_AGREEMENT_SIGNED_EVENT = "loan_agreement_signed"


def _record_loan_event(db: Session, loan: PlatformLoan, event_type: str, after: dict) -> None:
    """Append a money-movement audit row for a loan lifecycle transition.

    Added to the session (NOT flushed/committed here) so it shares the caller's
    transaction and is persisted atomically with the state change by the same
    ``db.commit()``. Query-free and flush-free, so it is safe under the unit
    tests' in-memory fake session. No PII: only loan/application ids, amounts in
    cents, and lifecycle status.
    """
    db.add(
        PlatformEvent(
            event_type=event_type,
            actor="system",
            application_id=getattr(loan, "application_id", None),
            payload={
                "v": 1,
                "actor": {"type": "system", "id": "system"},
                "loan_id": str(loan.id),
                "application_id": (
                    str(loan.application_id) if getattr(loan, "application_id", None) else None
                ),
                "after": after,
            },
        )
    )


def _disbursed_after(loan: PlatformLoan) -> dict:
    """The ``after`` payload for a ``loan_disbursed`` (funds released) event."""
    return {
        "status": loan.status,
        "disbursement_status": loan.disbursement_status,
        "amount_cents": loan.principal_cents,
        "disbursement_ref": loan.disbursement_ref,
        "disbursed_at": loan.disbursed_at.isoformat() if loan.disbursed_at else None,
    }


# ---------------------------------------------------------------------------
# Adapter construction from integration_settings (gracefully optional)
# ---------------------------------------------------------------------------


def _build_signnow_adapter(db: Session):
    """Construct a ``SignNowAdapter`` from the enabled ``signnow`` settings row,
    or return ``None`` if the provider is missing / disabled / unconfigured.

    config:  base_url, (optional) template_id
    secrets: api_key (bearer token), (optional) webhook_secret
    """
    setting = integration_settings.get(db, _SIGNNOW_PROVIDER)
    if setting is None or not setting.enabled:
        return None
    config = setting.config or {}
    secrets = setting.secrets or {}
    base_url = config.get("base_url")
    if not base_url:
        return None
    api_key = secrets.get("api_key") or secrets.get("access_token")
    if not api_key and all(
        secrets.get(k) for k in ("client_id", "client_secret", "username", "password")
    ):
        # No static bearer — acquire (and cache) one via OAuth2.
        from app.services.esign.signnow_oauth import get_signnow_token

        api_key = get_signnow_token(
            client_id=secrets["client_id"],
            client_secret=secrets["client_secret"],
            username=secrets["username"],
            password=secrets["password"],
            base_url=base_url,
        )
    if not api_key:
        return None
    # Local import keeps the adapter dependency optional at import time.
    from app.services.esign.signnow_adapter import SignNowAdapter

    return SignNowAdapter(
        api_key=api_key,
        base_url=base_url,
        webhook_secret=secrets.get("webhook_secret"),
    )


def _build_zumrails_adapter(db: Session):
    """Construct a ``ZumrailsAdapter`` from the enabled ``zumrails`` settings row,
    or return ``None`` if the provider is missing / disabled / unconfigured.

    config:  base_url, (optional) funding_source_id, currency, auth_mode
    secrets: api_key, api_secret, (optional) webhook_secret
    """
    setting = integration_settings.get(db, _ZUMRAILS_PROVIDER)
    if setting is None or not setting.enabled:
        return None
    config = setting.config or {}
    secrets = setting.secrets or {}
    api_key = secrets.get("api_key")
    api_secret = secrets.get("api_secret")
    base_url = config.get("base_url")
    if not api_key or not base_url:
        return None
    from app.services.payments.zumrails_adapter import ZumrailsAdapter

    auth_mode = config.get("auth_mode", "oauth")
    if auth_mode == "oauth" and api_secret:
        # Resolve + cache the bearer token once via the shared helper, then hand the
        # adapter a ready token so it doesn't re-authorize on every call.
        from app.services.payments.zumrails_auth import get_zumrails_token

        api_key = get_zumrails_token(api_key=api_key, api_secret=api_secret, base_url=base_url)
        api_secret = ""
        auth_mode = "static_bearer"

    return ZumrailsAdapter(
        api_key=api_key,
        api_secret=api_secret or "",
        base_url=base_url,
        webhook_secret=secrets.get("webhook_secret"),
        funding_source_id=config.get("funding_source_id"),
        currency=config.get("currency", "CAD"),
        auth_mode=auth_mode,
    )


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def book_loan(
    db: Session,
    application: PlatformCreditApplication,
    first_due_date=None,
) -> PlatformLoan:
    """Book a loan from an APPROVED application (lifecycle entry point).

    Idempotent: if a loan already exists for this application, the existing loan
    is returned unchanged (no second booking). Otherwise delegates to
    ``loan_servicing.create_loan_from_application`` which creates the loan in
    ``pending_disbursement`` with its full amortization schedule. The new
    lifecycle flags default to ``agreement_status=not_sent`` /
    ``disbursement_status=not_started``.

    This is the hook ``flow_orchestrator``'s approved path should call (see the
    PR report's WIRING NEEDED) — it does NOT send the agreement or disburse;
    those are the explicit downstream transitions below.
    """
    existing = (
        db.query(PlatformLoan)
        .filter(PlatformLoan.application_id == application.id)
        .first()
    )
    if existing is not None:
        logger.info(
            "loan_book_idempotent_skip",
            application_id=str(application.id),
            loan_id=str(existing.id),
        )
        return existing

    try:
        loan = loan_servicing.create_loan_from_application(
            db, application, first_due_date=first_due_date
        )
    except IntegrityError:
        # Race: another caller booked this application's loan between our SELECT and
        # INSERT. The unique constraint (migration 032) held — return the winner's
        # loan instead of surfacing a 500 / booking a duplicate.
        db.rollback()
        existing = (
            db.query(PlatformLoan)
            .filter(PlatformLoan.application_id == application.id)
            .first()
        )
        if existing is not None:
            logger.info(
                "loan_book_idempotent_race",
                application_id=str(application.id),
                loan_id=str(existing.id),
            )
            return existing
        raise
    _record_loan_event(
        db,
        loan,
        LOAN_BOOKED_EVENT,
        {
            "status": loan.status,
            "principal_cents": loan.principal_cents,
            "term_months": loan.term_months,
            "annual_rate_bps": loan.annual_rate_bps,
        },
    )
    db.commit()
    logger.info(
        "loan_booked",
        application_id=str(application.id),
        loan_id=str(loan.id),
        principal_cents=loan.principal_cents,
    )

    # WS-B: freeze the agreement documents (loan agreement + PAD) for this loan
    # from the versioned template store — the static snapshot the borrower sees
    # and signs. Best-effort by design: booking is a money-path transition and
    # must never fail because a template is missing/broken.
    try:
        from app.services import document_engine

        document_engine.generate_booking_documents(db, loan)
    except Exception:
        # Booking is already committed above; generate_booking_documents is
        # non-raising by contract. This guard is belt-and-suspenders only and
        # must NOT roll back the committed booking on a document hiccup.
        logger.warning(
            "loan_booking_document_generation_failed",
            loan_id=str(loan.id),
            exc_info=True,
        )
    return loan


def send_agreement(
    db: Session,
    loan: PlatformLoan,
    *,
    signer: Optional[SignerInput] = None,
    signnow=None,
) -> PlatformLoan:
    """Send the loan agreement to the borrower for e-signature (→ SignNow).

    Forward-only: a no-op if ``agreement_status`` is already past ``not_sent``
    (sent/signed/declined). On success sets ``agreement_status=sent`` and stores
    the SignNow document id in ``agreement_ref``.

    Graceful no-op: if no SignNow adapter can be built (provider disabled /
    unconfigured), logs and leaves the loan in ``not_sent`` — booking is not
    blocked by a turned-off integration.
    """
    if loan.agreement_status != "not_sent":
        logger.info(
            "loan_agreement_send_skip",
            loan_id=str(loan.id),
            agreement_status=loan.agreement_status,
        )
        return loan

    adapter = signnow if signnow is not None else _build_signnow_adapter(db)
    if adapter is None:
        logger.warning(
            "loan_agreement_send_noop_provider_disabled",
            loan_id=str(loan.id),
            provider=_SIGNNOW_PROVIDER,
        )
        return loan

    if signer is None:
        signer = _signer_for_loan(db, loan)

    # Send from a configured template if present, else require a document id is
    # already attached. The template id lives in the signnow config row.
    template_id = None
    setting = integration_settings.get(db, _SIGNNOW_PROVIDER)
    if setting is not None:
        template_id = (setting.config or {}).get("template_id")

    result = adapter.send_for_signature(
        signer=signer,
        template_id=template_id,
        document_id=loan.agreement_ref if not template_id else None,
        subject="Your loan agreement",
    )

    loan.agreement_status = "sent"
    loan.agreement_ref = result.document_id
    _record_loan_event(
        db,
        loan,
        LOAN_AGREEMENT_SENT_EVENT,
        {"agreement_status": "sent", "agreement_ref": result.document_id},
    )
    db.commit()
    db.refresh(loan)
    logger.info(
        "loan_agreement_sent",
        loan_id=str(loan.id),
        agreement_ref=loan.agreement_ref,
    )
    return loan


def on_agreement_signed(
    db: Session,
    loan: PlatformLoan,
    *,
    recipient_id: Optional[str] = None,
    zumrails=None,
) -> PlatformLoan:
    """Handle the SignNow "signed" callback → trigger the Zumrails disbursement.

    Entry point for the SignNow completion webhook (resolve the loan by
    ``agreement_ref`` == SignNow document id, then call this).

    Forward-only + idempotent:
      * Marks ``agreement_status=signed`` (from ``sent``).
      * Then, if disbursement hasn't already started, fires a Zumrails
        disbursement and sets ``disbursement_status=in_progress`` +
        ``disbursement_ref`` = the Zumrails transaction id. Re-delivery is a
        no-op because disbursement_status is no longer ``not_started``.

    Graceful no-op on disbursement: if no Zumrails adapter can be built, the
    agreement is still marked signed but disbursement stays ``not_started`` for
    a later retry.
    """
    if loan.agreement_status == "declined":
        logger.warning(
            "loan_agreement_signed_after_declined",
            loan_id=str(loan.id),
        )
        return loan

    if loan.agreement_status == "not_sent":
        # Out-of-order webhook (signed before we recorded the send). Accept it.
        logger.info("loan_agreement_signed_without_send", loan_id=str(loan.id))

    if loan.agreement_status != "signed":
        loan.agreement_status = "signed"
        _record_loan_event(
            db,
            loan,
            LOAN_AGREEMENT_SIGNED_EVENT,
            {"agreement_status": "signed", "agreement_ref": loan.agreement_ref},
        )
        db.commit()
        db.refresh(loan)
        logger.info("loan_agreement_signed", loan_id=str(loan.id))

    # Disbursement is its own hardened entry point so the lender/admin cockpit can
    # release a signed loan through the SAME locked, audited, idempotent path.
    return initiate_disbursement(db, loan, recipient_id=recipient_id, zumrails=zumrails)


def initiate_disbursement(
    db: Session,
    loan: PlatformLoan,
    *,
    recipient_id: Optional[str] = None,
    zumrails=None,
) -> PlatformLoan:
    """Fire the Zumrails disbursement for a loan, exactly once (lifecycle entry point).

    The single place that pushes a payout. Called by ``on_agreement_signed`` (the
    SignNow callback) AND by the admin cockpit's "release disbursement" action, so
    both go through the same row lock, idempotency, ref-setting, and money events —
    no path writes ``disbursement_status`` inline.

    Forward-only + idempotent: a row lock (``refresh(with_for_update=True)``)
    serializes callers, and the ``disbursement_status != not_started`` check makes
    re-entry a no-op. A retried push is additionally deduped vendor-side by
    ``client_transaction_id == loan.id``. Graceful no-op (no commit) if no adapter
    or recipient can be resolved, leaving the loan ``not_started`` for a later retry.
    """
    # Serialize the disbursement decision: take a row lock on the loan so two
    # concurrent callers can't both pass the not_started check below and
    # double-push to Zumrails. refresh(with_for_update=True) issues a
    # SELECT ... FOR UPDATE and refreshes disbursement_status to the committed value,
    # so the second caller blocks, then sees in_progress and no-ops. (The in-memory
    # check alone is not atomic.) A retried push that survives a transient timeout is
    # additionally deduped vendor-side by client_transaction_id=loan.id.
    db.refresh(loan, with_for_update=True)

    # Trigger disbursement exactly once.
    if loan.disbursement_status != "not_started":
        logger.info(
            "loan_disbursement_already_started",
            loan_id=str(loan.id),
            disbursement_status=loan.disbursement_status,
        )
        return loan

    adapter = zumrails if zumrails is not None else _build_zumrails_adapter(db)
    if adapter is None:
        logger.warning(
            "loan_disbursement_noop_provider_disabled",
            loan_id=str(loan.id),
            provider=_ZUMRAILS_PROVIDER,
        )
        return loan

    if recipient_id is None:
        recipient_id = _recipient_for_loan(db, loan)
    if not recipient_id:
        logger.warning(
            "loan_disbursement_noop_no_recipient",
            loan_id=str(loan.id),
        )
        return loan

    result = adapter.create_disbursement(
        recipient_id=recipient_id,
        amount_cents=loan.principal_cents,
        # loan id as the idempotency / correlation ref — a retried push with the
        # same ClientTransactionId is Zumrails' dedupe key.
        client_transaction_id=str(loan.id),
        memo=f"Loan disbursement {loan.id}",
    )

    loan.disbursement_status = "in_progress"
    loan.disbursement_ref = result.transaction_id

    # If Zumrails returns a terminal status synchronously, advance immediately.
    if result.status == TransactionStatus.COMPLETED:
        _mark_active(loan)
    elif result.status == TransactionStatus.FAILED:
        loan.disbursement_status = "failed"

    _record_loan_event(
        db,
        loan,
        LOAN_DISBURSEMENT_INITIATED_EVENT,
        {
            "amount_cents": loan.principal_cents,
            "disbursement_status": loan.disbursement_status,
            "disbursement_ref": loan.disbursement_ref,
        },
    )
    # A synchronous terminal result won't be followed by a webhook, so record the
    # terminal money event here too rather than leaving the trail incomplete.
    if loan.status == "active" and loan.disbursement_status == "completed":
        _record_loan_event(db, loan, LOAN_DISBURSED_EVENT, _disbursed_after(loan))
    elif loan.disbursement_status == "failed":
        _record_loan_event(
            db,
            loan,
            LOAN_DISBURSEMENT_FAILED_EVENT,
            {"amount_cents": loan.principal_cents, "disbursement_ref": loan.disbursement_ref},
        )

    db.commit()
    db.refresh(loan)
    logger.info(
        "loan_disbursement_initiated",
        loan_id=str(loan.id),
        disbursement_ref=loan.disbursement_ref,
        disbursement_status=loan.disbursement_status,
    )
    return loan


def charge_off_loan(
    db: Session,
    loan: PlatformLoan,
    *,
    reason_code: Optional[str] = None,
    actor: Optional[str] = None,
) -> PlatformLoan:
    """Write a loan off as a loss (lifecycle entry point).

    The single place that flips a loan to ``charged_off`` — so the most material
    loss event always emits a ``loan_charged_off`` money event on the WORM log
    (the admin cockpit used to set the column inline with no money event). Row-
    locked + idempotent: a second charge-off is a no-op; a paid-off / cancelled
    loan cannot be charged off.
    """
    db.refresh(loan, with_for_update=True)
    if loan.status == "charged_off":
        logger.info("loan_charge_off_idempotent_skip", loan_id=str(loan.id))
        return loan
    if loan.status in ("paid_off", "cancelled"):
        raise ValueError(f"cannot charge off a loan in status '{loan.status}'")

    loan.status = "charged_off"
    # Real close timestamp (migration 069): the Archive sorts and pages on Close
    # date, which used to be the drifting ``updated_at`` proxy.
    archive.stamp_loan_closed(loan)
    _record_loan_event(
        db,
        loan,
        LOAN_CHARGED_OFF_EVENT,
        {
            "status": "charged_off",
            "principal_balance_cents": loan.principal_balance_cents,
            "reason_code": reason_code,
            "actor": actor,
        },
    )
    db.commit()
    db.refresh(loan)
    logger.info("loan_charged_off", loan_id=str(loan.id), reason_code=reason_code)
    return loan


def on_disbursement_complete(
    db: Session,
    loan: PlatformLoan,
    ref: Optional[str] = None,
) -> PlatformLoan:
    """Handle the Zumrails "completed" callback → activate the loan.

    Entry point for the Zumrails disbursement webhook (resolve the loan by
    ``disbursement_ref`` == Zumrails transaction id, then call this).

    Forward-only + idempotent: sets ``disbursement_status=completed``,
    ``status=active`` and stamps ``disbursed_at`` (only if not already active).
    A re-delivered webhook is a no-op. If ``ref`` is supplied it is stored /
    cross-checked against ``disbursement_ref``.
    """
    if ref:
        if loan.disbursement_ref and loan.disbursement_ref != ref:
            logger.warning(
                "loan_disbursement_ref_mismatch",
                loan_id=str(loan.id),
                stored_ref=loan.disbursement_ref,
                webhook_ref=ref,
            )
        loan.disbursement_ref = loan.disbursement_ref or ref

    if loan.status == "active" and loan.disbursement_status == "completed":
        logger.info("loan_disbursement_complete_idempotent", loan_id=str(loan.id))
        return loan

    _mark_active(loan)
    _record_loan_event(db, loan, LOAN_DISBURSED_EVENT, _disbursed_after(loan))
    db.commit()
    db.refresh(loan)
    logger.info(
        "loan_activated",
        loan_id=str(loan.id),
        disbursed_at=loan.disbursed_at.isoformat() if loan.disbursed_at else None,
    )
    return loan


def on_disbursement_failed(
    db: Session,
    loan: PlatformLoan,
    ref: Optional[str] = None,
) -> PlatformLoan:
    """Handle a Zumrails "failed" callback → mark the disbursement failed.

    Leaves the loan in ``pending_disbursement`` (it never funded) and flips
    ``disbursement_status=failed`` so an operator can retry. Forward-only: a
    no-op if already completed.
    """
    if loan.disbursement_status == "completed":
        return loan
    loan.disbursement_status = "failed"
    _record_loan_event(
        db,
        loan,
        LOAN_DISBURSEMENT_FAILED_EVENT,
        {"amount_cents": loan.principal_cents, "disbursement_ref": loan.disbursement_ref or ref},
    )
    db.commit()
    db.refresh(loan)
    logger.warning("loan_disbursement_failed", loan_id=str(loan.id), ref=ref)
    return loan


# ---------------------------------------------------------------------------
# Reconciliation — advance disbursements stuck in_progress with no webhook
# ---------------------------------------------------------------------------


@dataclass
class ReconcileResult:
    """Summary of a ``reconcile_stuck_disbursements`` sweep (no PII).

    Counters are mutually exclusive per loan examined: each stuck loan lands in
    exactly one bucket. ``loans_completed`` / ``loans_failed`` are the loan ids
    advanced to a terminal state via the existing lifecycle entry points.
    """

    examined: int = 0
    loans_completed: list[str] = field(default_factory=list)
    loans_failed: list[str] = field(default_factory=list)
    still_pending: int = 0  # vendor status still non-terminal — left in_progress
    skipped_no_ref: int = 0  # in_progress but no disbursement_ref to poll
    errors: int = 0  # adapter/poll error — left untouched for the next sweep
    provider_disabled: bool = False  # adapter couldn't be built — whole sweep no-op'd

    def __str__(self) -> str:  # pragma: no cover - logging convenience
        return (
            f"examined={self.examined} completed={len(self.loans_completed)} "
            f"failed={len(self.loans_failed)} still_pending={self.still_pending} "
            f"skipped_no_ref={self.skipped_no_ref} errors={self.errors} "
            f"provider_disabled={self.provider_disabled}"
        )


def reconcile_stuck_disbursements(
    db: Session,
    *,
    stuck_for: timedelta = _DEFAULT_RECONCILE_AFTER,
    now: Optional[datetime] = None,
    zumrails=None,
    limit: int = 200,
) -> ReconcileResult:
    """Find loans wedged at ``disbursement_status=in_progress`` and resolve them.

    A disbursement is left ``in_progress`` forever if Zumrails returns a
    non-terminal status (PENDING/IN_PROGRESS/UNKNOWN) at push time and no
    terminal webhook ever arrives. This sweep is the safety net: for each such
    loan older than ``stuck_for`` it polls the Zumrails adapter for the
    transaction's *current* status and advances it via the existing
    ``on_disbursement_complete`` / ``on_disbursement_failed`` entry points —
    which own all the state logic and are idempotent + forward-only. This
    function deliberately contains **no** state transitions of its own.

    Graceful no-op: if no Zumrails adapter can be built (provider disabled /
    unconfigured) the sweep returns immediately with ``provider_disabled=True``
    and touches nothing. Per-loan adapter errors are swallowed (logged + counted)
    so one bad transaction can't abort the batch; that loan is retried next sweep.

    Tests inject ``zumrails=`` to bypass credential lookup, and ``now=`` to make
    the staleness cutoff deterministic.
    """
    result = ReconcileResult()

    adapter = zumrails if zumrails is not None else _build_zumrails_adapter(db)
    if adapter is None:
        logger.warning(
            "loan_disbursement_reconcile_noop_provider_disabled",
            provider=_ZUMRAILS_PROVIDER,
        )
        result.provider_disabled = True
        return result

    now = now or datetime.now(timezone.utc)
    cutoff = now - stuck_for

    # Stuck = still in_progress and not touched since the cutoff. updated_at flips
    # on every state change (onupdate=func.now()), so it's the freshest "this loan
    # hasn't moved" signal we have without adding a column.
    stuck = (
        db.query(PlatformLoan)
        .filter(
            PlatformLoan.disbursement_status == "in_progress",
            PlatformLoan.updated_at <= cutoff,
        )
        .order_by(PlatformLoan.updated_at.asc())
        .limit(limit)
        .all()
    )

    for loan in stuck:
        result.examined += 1
        if not loan.disbursement_ref:
            # In_progress with no vendor id to poll — can't reconcile here; an
            # operator must investigate. (Shouldn't happen: the ref is set in the
            # same commit as in_progress.)
            logger.warning(
                "loan_disbursement_reconcile_no_ref", loan_id=str(loan.id)
            )
            result.skipped_no_ref += 1
            continue

        try:
            txn = adapter.get_transaction_status(loan.disbursement_ref)
        except ZumrailsError as exc:
            # Transient/permanent both: leave the loan in_progress and move on —
            # do not guess a terminal state from a failed poll.
            logger.warning(
                "loan_disbursement_reconcile_poll_failed",
                loan_id=str(loan.id),
                disbursement_ref=loan.disbursement_ref,
                error=type(exc).__name__,
            )
            result.errors += 1
            continue

        if txn.status == TransactionStatus.COMPLETED:
            on_disbursement_complete(db, loan, ref=loan.disbursement_ref)
            result.loans_completed.append(str(loan.id))
        elif txn.status == TransactionStatus.FAILED:
            on_disbursement_failed(db, loan, ref=loan.disbursement_ref)
            result.loans_failed.append(str(loan.id))
        else:
            # PENDING / IN_PROGRESS / CANCELLED / UNKNOWN — non-terminal. We treat
            # these EXACTLY as the live Zumrails webhook does (payments.py: only
            # COMPLETED/FAILED write; everything else is ignored) so the same vendor
            # status never produces a different money outcome by arrival path. Leave
            # the loan in_progress; the next sweep (or a terminal webhook) decides it.
            logger.info(
                "loan_disbursement_reconcile_still_pending",
                loan_id=str(loan.id),
                vendor_status=txn.status.value,
            )
            result.still_pending += 1

    logger.info("loan_disbursement_reconcile_complete", result=str(result))
    return result


def on_agreement_declined(db: Session, loan: PlatformLoan) -> PlatformLoan:
    """Handle a SignNow "declined" callback → mark the agreement declined.

    Forward-only: no-op once signed. Does not disburse.
    """
    if loan.agreement_status == "signed":
        return loan
    loan.agreement_status = "declined"
    db.commit()
    db.refresh(loan)
    logger.info("loan_agreement_declined", loan_id=str(loan.id))
    return loan


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _mark_active(loan: PlatformLoan) -> None:
    """Flip a loan to its funded/active state (no commit — caller commits)."""
    loan.disbursement_status = "completed"
    if loan.disbursed_at is None:
        loan.disbursed_at = datetime.now(timezone.utc)
    # Only advance from pending_disbursement; never override a later terminal
    # status (paid_off / charged_off / cancelled).
    if loan.status == "pending_disbursement":
        loan.status = "active"


def _signer_for_loan(db: Session, loan: PlatformLoan) -> SignerInput:
    """Build the SignNow signer (borrower) from the loan's application/patient.

    PII (email/name) is sourced here at the wiring layer and handed to the
    adapter; it never appears in logs.
    """
    application = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == loan.application_id)
        .first()
    )
    patient = getattr(application, "patient", None) if application else None
    if patient is None and application is not None:
        from app.models.platform.patient import PlatformPatient

        patient = (
            db.query(PlatformPatient)
            .filter(PlatformPatient.id == application.patient_id)
            .first()
        )
    email = getattr(patient, "email", None) or ""
    first = getattr(patient, "legal_first_name", None) or ""
    last = getattr(patient, "legal_last_name", None) or ""
    name = (f"{first} {last}").strip() or email
    return SignerInput(email=email, name=name)


def _recipient_for_loan(db: Session, loan: PlatformLoan) -> Optional[str]:
    """Resolve the Zumrails recipient (payee) id for this loan's borrower.

    ASSUMPTION / SEAM: the borrower's Zumrails user id is expected to live on
    the patient record (or a linked funding-profile). It is NOT modeled in this
    wave, so this returns ``None`` unless a ``zumrails_user_id`` attribute is
    present — which makes ``on_agreement_signed`` gracefully no-op the
    disbursement until that mapping is wired. Tests inject ``recipient_id``
    directly to bypass this.
    """
    application = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == loan.application_id)
        .first()
    )
    patient = getattr(application, "patient", None) if application else None
    return getattr(patient, "zumrails_recipient_id", None)
