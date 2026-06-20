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

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.loan import PlatformLoan
from app.services import integration_settings
from app.services import loan_servicing
from app.services.esign.signnow_adapter import SignerInput
from app.services.payments.zumrails_adapter import TransactionStatus

logger = get_logger(__name__)


# Provider slugs in platform_integration_settings (see KNOWN_PROVIDERS).
_SIGNNOW_PROVIDER = "signnow"
_ZUMRAILS_PROVIDER = "zumrails"


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
    api_key = secrets.get("api_key") or secrets.get("access_token")
    base_url = config.get("base_url")
    if not api_key or not base_url:
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

    return ZumrailsAdapter(
        api_key=api_key,
        api_secret=api_secret or "",
        base_url=base_url,
        webhook_secret=secrets.get("webhook_secret"),
        funding_source_id=config.get("funding_source_id"),
        currency=config.get("currency", "CAD"),
        auth_mode=config.get("auth_mode", "oauth"),
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

    loan = loan_servicing.create_loan_from_application(
        db, application, first_due_date=first_due_date
    )
    logger.info(
        "loan_booked",
        application_id=str(application.id),
        loan_id=str(loan.id),
        principal_cents=loan.principal_cents,
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
        db.commit()
        db.refresh(loan)
        logger.info("loan_agreement_signed", loan_id=str(loan.id))

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

    db.commit()
    db.refresh(loan)
    logger.info(
        "loan_disbursement_initiated",
        loan_id=str(loan.id),
        disbursement_ref=loan.disbursement_ref,
        disbursement_status=loan.disbursement_status,
    )
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
    db.commit()
    db.refresh(loan)
    logger.warning("loan_disbursement_failed", loan_id=str(loan.id), ref=ref)
    return loan


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
    return getattr(patient, "zumrails_user_id", None)
