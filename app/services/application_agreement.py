"""Application-level agreement + Simulate-Signing (activation rework Wave 1).

Groundwork for the "loan created at ACTIVATION, not approval" rework. Today an
agreement is sent + signed on the LOAN (``loan_lifecycle.send_agreement`` /
``simulate_signing``). To let the borrower accept + sign BEFORE a loan is booked,
this module ports those two entry points to the APPLICATION, reading + writing
the new pre-loan columns on ``PlatformCreditApplication``
(``agreement_status`` / ``agreement_ref`` / ``agreement_signed_at``, migration
078) instead of a loan's.

It is a NEAR-VERBATIM port of the loan-level functions and reuses the SAME
Simulator/Live gating (:mod:`app.services.integration_mode`), the SAME
``SIMULATED_AGREEMENT_REF_PREFIX`` labelling, and the SAME ``ESignModeError``
type as ``loan_lifecycle``, so simulator-vs-live behaviour can never diverge
between the loan and application paths.

ADDITIVE ONLY this wave: nothing in the current approve/accept path calls these
yet. The behavioural cutover (booking the loan at activation off a signed
application agreement) is a LATER wave.

DESIGN RULES (inherited from loan_lifecycle):
  * **Forward-only.** ``send`` no-ops once past ``not_sent``; ``simulate_signing``
    is idempotent once ``signed`` and refuses a ``declined`` agreement.
  * **Mode-aware.** Simulator (the default) really transitions the agreement with
    a labelled ``SIMULATED-<application id>`` ref; live fires the real SignNow
    invite (graceful no-op when un-credentialed).
  * **Simulate-Signing is simulator-only.** Live mode raises ``ESignModeError``
    (surfaced as a 409), exactly like the loan-level version — the real SignNow
    completion webhook drives signing there.

Unlike the loan-level version, there is NO disbursement fan-out here: an
application has no money to move. Signing simply stamps the application ``signed``
+ ``agreement_signed_at``. Disbursement remains a loan-lifecycle concern.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.event import PlatformEvent
from app.services import integration_mode
from app.services.esign.signnow_adapter import SignerInput

# Reuse the loan-lifecycle constants/gating so the two paths never fork.
from app.services.loan_lifecycle import (
    ESignModeError,
    SIMULATED_AGREEMENT_REF_PREFIX,
    _SIGNNOW_PROVIDER,
    _build_signnow_adapter,
)

logger = get_logger(__name__)


# Agreement (e-sign) lifecycle events on the APPLICATION — analogous to
# ``loan_lifecycle.LOAN_AGREEMENT_SENT_EVENT`` / ``..._SIGNED_EVENT`` but keyed to
# the pre-loan application, so the notification processor / audit trail can tell a
# pre-loan agreement apart from a booked-loan agreement.
APPLICATION_AGREEMENT_SENT_EVENT = "application_agreement_sent"
APPLICATION_AGREEMENT_SIGNED_EVENT = "application_agreement_signed"


def _record_application_event(
    db: Session,
    application: PlatformCreditApplication,
    event_type: str,
    after: dict,
    *,
    actor: Optional[str] = None,
) -> None:
    """Append an audit row for an application agreement transition.

    Added to the session (NOT flushed/committed here) so it shares the caller's
    transaction and persists atomically with the state change. Query-free and
    flush-free, so it is safe under the unit tests' in-memory fake session. No
    PII: only the application id, the acting staff id, and lifecycle status.
    """
    db.add(
        PlatformEvent(
            event_type=event_type,
            actor=actor or "system",
            application_id=getattr(application, "id", None),
            payload={
                "v": 1,
                "actor": {"type": "admin" if actor else "system", "id": actor or "system"},
                "application_id": str(application.id),
                "after": after,
            },
        )
    )


def _signer_for_application(
    db: Session, application: PlatformCreditApplication
) -> SignerInput:
    """Build the SignNow signer (borrower) from the application/patient.

    PII (email/name) is sourced here at the wiring layer and handed to the
    adapter; it never appears in logs. Prefers the application's own structured
    fields, falling back to the linked patient record.
    """
    patient = getattr(application, "patient", None)
    if patient is None and getattr(application, "patient_id", None) is not None:
        from app.models.platform.patient import PlatformPatient

        patient = (
            db.query(PlatformPatient)
            .filter(PlatformPatient.id == application.patient_id)
            .first()
        )
    email = (
        getattr(application, "email", None)
        or getattr(patient, "email", None)
        or ""
    )
    first = (
        getattr(application, "first_name", None)
        or getattr(patient, "legal_first_name", None)
        or ""
    )
    last = (
        getattr(application, "last_name", None)
        or getattr(patient, "legal_last_name", None)
        or ""
    )
    name = (f"{first} {last}").strip() or email
    return SignerInput(email=email, name=name)


def send_agreement_for_application(
    db: Session,
    application: PlatformCreditApplication,
    *,
    actor: Optional[str] = None,
    signer: Optional[SignerInput] = None,
    signnow=None,
) -> PlatformCreditApplication:
    """Send the loan agreement to the borrower for e-signature, PRE-LOAN.

    Forward-only: a no-op if ``agreement_status`` is already past ``not_sent``
    (sent/signed/declined).

    MODE-AWARE (Dave's Integration SIMULATOR mandate), mirroring
    ``loan_lifecycle.send_agreement``:
      * SIMULATOR (the default) — transitions ``agreement_status`` to ``sent``
        with a clearly-labelled simulated ``agreement_ref``
        (``SIMULATED-<application id>``) and records a ``simulated: true`` event.
        NOT a no-op: the agreement is really "sent / awaiting signature", so the
        flow can be reviewed and completed via :func:`simulate_signing_for_application`.
      * LIVE — the real SignNow invite. If no adapter can be built (live but
        un-credentialed) it is a graceful no-op leaving ``not_sent`` for a retry
        once creds land — live is legitimately gated behind credentials.

    An explicitly injected ``signnow=`` adapter always takes the real path (the
    test / live seam), regardless of mode.
    """
    if application.agreement_status != "not_sent":
        logger.info(
            "application_agreement_send_skip",
            application_id=str(application.id),
            agreement_status=application.agreement_status,
        )
        return application

    # SIMULATOR path: no injected adapter + simulator mode -> labelled simulation.
    if signnow is None and integration_mode.is_simulator(db, _SIGNNOW_PROVIDER):
        application.agreement_status = "sent"
        application.agreement_ref = (
            f"{SIMULATED_AGREEMENT_REF_PREFIX}{application.id}"
        )
        _record_application_event(
            db,
            application,
            APPLICATION_AGREEMENT_SENT_EVENT,
            {
                "agreement_status": "sent",
                "agreement_ref": application.agreement_ref,
                "simulated": True,
            },
            actor=actor,
        )
        db.commit()
        db.refresh(application)
        logger.info(
            "application_agreement_sent_simulated",
            application_id=str(application.id),
            agreement_ref=application.agreement_ref,
        )
        return application

    adapter = signnow if signnow is not None else _build_signnow_adapter(db)
    if adapter is None:
        # LIVE mode but no adapter could be built (creds absent). Live is gated
        # behind credentials; leave ``not_sent`` for a retry once they land.
        logger.warning(
            "application_agreement_send_noop_live_unconfigured",
            application_id=str(application.id),
            provider=_SIGNNOW_PROVIDER,
        )
        return application

    if signer is None:
        signer = _signer_for_application(db, application)

    # Send from a configured template if present, else use an already-attached
    # document id. The template id lives in the signnow config row.
    template_id = None
    from app.services import integration_settings

    setting = integration_settings.get(db, _SIGNNOW_PROVIDER)
    if setting is not None:
        template_id = (setting.config or {}).get("template_id")

    result = adapter.send_for_signature(
        signer=signer,
        template_id=template_id,
        document_id=application.agreement_ref if not template_id else None,
        subject="Your loan agreement",
    )

    application.agreement_status = "sent"
    application.agreement_ref = result.document_id
    _record_application_event(
        db,
        application,
        APPLICATION_AGREEMENT_SENT_EVENT,
        {"agreement_status": "sent", "agreement_ref": result.document_id},
        actor=actor,
    )
    db.commit()
    db.refresh(application)
    logger.info(
        "application_agreement_sent",
        application_id=str(application.id),
        agreement_ref=application.agreement_ref,
    )
    return application


def simulate_signing_for_application(
    db: Session,
    application: PlatformCreditApplication,
    *,
    actor: Optional[str] = None,
) -> PlatformCreditApplication:
    """Complete a SIMULATED e-signature on the APPLICATION (SIMULATOR mode).

    The pre-loan analogue of ``loan_lifecycle.simulate_signing`` — the backend for
    the "Simulate Signing" button when the agreement lives on the application. It
    drives the SAME transition the real SignNow completion webhook drives
    (``agreement -> signed``) and stamps ``agreement_signed_at``, labelled
    ``simulated: true`` on its event.

    Guards (identical to the loan-level version):
      * LIVE mode -> :class:`ESignModeError` (the real SignNow flow applies; this
        path is disabled), surfaced by the endpoint as a 409.
      * Idempotent — already ``signed`` returns unchanged; ``declined`` is
        rejected. If the agreement was never "sent" (e.g. live-unconfigured left
        it ``not_sent``) it is accepted out-of-order, mirroring the real webhook.

    There is NO disbursement fan-out — an application has no money to move; that
    stays a loan-lifecycle concern once a loan is booked.
    """
    if integration_mode.is_live(db, _SIGNNOW_PROVIDER):
        raise ESignModeError(
            "Simulate Signing is only available while SignNow is in simulator "
            "mode. This integration is set to live — use the real SignNow "
            "signing flow, or switch it back to simulator."
        )
    if application.agreement_status == "declined":
        raise ESignModeError(
            "This agreement was declined and cannot be signed. Re-send it first."
        )
    if application.agreement_status == "signed":
        logger.info(
            "application_agreement_signed_idempotent",
            application_id=str(application.id),
        )
        return application

    if application.agreement_status == "not_sent":
        # Out-of-order (signed before we recorded the send). Accept it, mirroring
        # the real webhook's tolerance.
        logger.info(
            "application_agreement_signed_without_send",
            application_id=str(application.id),
        )

    application.agreement_status = "signed"
    application.agreement_signed_at = datetime.now(timezone.utc)
    _record_application_event(
        db,
        application,
        APPLICATION_AGREEMENT_SIGNED_EVENT,
        {
            "agreement_status": "signed",
            "agreement_ref": application.agreement_ref,
            "agreement_signed_at": application.agreement_signed_at.isoformat(),
            "simulated": True,
        },
        actor=actor,
    )
    db.commit()
    db.refresh(application)
    logger.info(
        "application_agreement_signed",
        application_id=str(application.id),
        simulated=True,
    )
    return application
