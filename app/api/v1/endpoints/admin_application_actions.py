"""P0 T3 — the six dead Originations controls, given backends.

Mounted under ``/api/v1/admin/applications``. Dave's 2026-07-21 review lists
six buttons on the application work surface; all six rendered ``disabled``,
which is the experience that produced the "10% works" verdict.

    Bank Accounts tab   Add Bank Account · Bank Verification
    Contacts tab        Send Email · Send SMS
    Credit Report tab   Hard Pull · Soft Pull

RBAC — every route in this module is ``admin``/``staff`` via the router-level
dependency. Bank-account mutation is deliberately UNREACHABLE from the clinic
(vendor) API: nothing under ``app/api/clinic`` imports this router, and Dave is
explicit — *"vendor users can not add bank accounts"*.

Honesty about mocks — Flinks is in simulator mode and the Equifax subscriber
agreement is unsigned, so bank-verification and bureau-pull responses carry
``simulated: true`` plus a human-readable ``simulated_reason``. The frontend
labels the result instead of implying a real bureau pull happened. The flag
comes from what actually ran (adapter vendor / feature flag), never a constant.

Every action appends a ``platform_events`` row with the staff actor.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status as http_status
from jinja2 import TemplateError
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.core.config import settings
from app.db.base import get_db
from app.models.platform.borrower_portal import PlatformPatientBankAccount
from app.models.platform.communication import PlatformCommunicationLog
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_report import PlatformCreditReportPull
from app.models.platform.event import PlatformEvent
from app.models.platform.patient import PlatformPatient
from app.models.platform.verification import PlatformVerification
from app.models.user import User
from app.services import application_actions
from app.services.adapters.base import PatientProfile
from app.services.application_actions import (
    ActionError,
    ResolvedParty,
    co_borrower_applications,
    primary_application,
    resolve_party,
)
from app.services.borrower_portal import mask_identifier
from app.services.notification_render import (
    NOTIFICATION_TYPES,
    UnknownNotificationType,
    render_email,
    render_sms,
)
from app.services.real_notification_dispatcher import (
    PermanentNotificationError,
    TransientNotificationError,
)

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _actor(user: User) -> str:
    return str(getattr(user, "id", "") or "unknown")


def _actor_label(user: User) -> str:
    return getattr(user, "email", None) or _actor(user)


def _get_application(db: Session, application_id: UUID) -> PlatformCreditApplication:
    row = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Application not found"
        )
    return row


def _resolve(
    db: Session,
    app_row: PlatformCreditApplication,
    party: str,
    co_application_id: Optional[UUID],
) -> ResolvedParty:
    try:
        return resolve_party(
            db, app_row, party=party, co_application_id=co_application_id
        )
    except ActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def _audit(
    db: Session,
    *,
    event_type: str,
    actor: str,
    application_id: UUID,
    patient_id: UUID,
    payload: dict,
) -> PlatformEvent:
    event = PlatformEvent(
        event_type=event_type,
        actor=actor,
        patient_id=patient_id,
        application_id=application_id,
        payload=payload,
    )
    db.add(event)
    db.flush()
    return event


def _patient(db: Session, patient_id: UUID) -> PlatformPatient:
    row = (
        db.query(PlatformPatient).filter(PlatformPatient.id == patient_id).first()
    )
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Patient not found"
        )
    return row


# ---------------------------------------------------------------------------
# 1 + 2. Bank Accounts tab — Add Bank Account, Bank Verification
# ---------------------------------------------------------------------------


class BankAccountRow(BaseModel):
    """One row of Dave's Bank Accounts table. NEVER carries the full account
    number — ``account_mask`` is the display value."""

    account_id: UUID
    bank_name: str
    institution_number: Optional[str] = None
    transit_number: Optional[str] = None
    account_mask: str
    account_holder: Optional[str] = None
    account_type: Optional[str] = None
    source: Optional[str] = None
    is_default: bool
    status: str
    created_at: Optional[datetime] = None


class PartySection(BaseModel):
    party: str
    application_id: UUID
    patient_id: UUID
    accounts: list[BankAccountRow]


class BankAccountsTab(BaseModel):
    """Dave's two-section layout: Borrower Bank Accounts / Co-Borrower Bank
    Accounts. ``co_borrower_available`` tells the dialog whether to offer the
    Co-Borrower option at all."""

    application_id: UUID
    co_borrower_available: bool
    borrower: PartySection
    co_borrowers: list[PartySection]


class AddBankAccountBody(BaseModel):
    """Dave's Add Bank Account dialog, field for field. All mandatory.

    Institution/transit/account are STRINGS so leading zeros survive — the
    digit-count and numeric rules are enforced in
    ``application_actions.validate_bank_account_input``.
    """

    party: str = Field("borrower", pattern="^(borrower|co_borrower)$")
    co_application_id: Optional[UUID] = Field(
        None, description="Required only when several co-borrower files exist."
    )
    bank_name: str = Field(..., min_length=1, max_length=25)
    institution_number: str = Field(..., min_length=1, max_length=3)
    transit_number: str = Field(..., min_length=1, max_length=5)
    account_number: str = Field(..., min_length=1, max_length=15)
    account_type: str = Field(..., description="Checking | Savings")
    account_holder: Optional[str] = Field(None, max_length=200)
    make_default: bool = False


def _bank_row(row: PlatformPatientBankAccount) -> BankAccountRow:
    return BankAccountRow(
        account_id=row.id,
        bank_name=row.institution_name,
        institution_number=row.institution_number,
        transit_number=row.transit_number,
        account_mask=row.account_mask,
        account_holder=row.account_holder,
        account_type=row.account_type,
        source=row.source
        or (
            application_actions.SOURCE_FLINKS
            if row.verified_via == "flinks"
            else application_actions.SOURCE_MANUAL
        ),
        is_default=bool(row.is_default),
        status=row.status,
        created_at=row.created_at,
    )


def _accounts_for(db: Session, patient_id: UUID) -> list[BankAccountRow]:
    rows = (
        db.query(PlatformPatientBankAccount)
        .filter(
            PlatformPatientBankAccount.patient_id == patient_id,
            PlatformPatientBankAccount.status == "active",
        )
        .order_by(PlatformPatientBankAccount.created_at.asc())
        .all()
    )
    return [_bank_row(r) for r in rows]


@router.get("/{application_id}/bank-accounts", response_model=BankAccountsTab)
def get_bank_accounts_tab(
    application_id: UUID,
    db: Session = Depends(get_db),
):
    """The Bank Accounts tab: borrower + co-borrower sections."""
    app_row = _get_application(db, application_id)
    primary = primary_application(db, app_row)
    linked = co_borrower_applications(db, primary)
    return BankAccountsTab(
        application_id=primary.id,
        co_borrower_available=bool(linked),
        borrower=PartySection(
            party=application_actions.PARTY_BORROWER,
            application_id=primary.id,
            patient_id=primary.patient_id,
            accounts=_accounts_for(db, primary.patient_id),
        ),
        co_borrowers=[
            PartySection(
                party=application_actions.PARTY_CO_BORROWER,
                application_id=row.id,
                patient_id=row.patient_id,
                accounts=_accounts_for(db, row.patient_id),
            )
            for row in linked
        ],
    )


@router.post(
    "/{application_id}/bank-accounts",
    response_model=BankAccountRow,
    status_code=http_status.HTTP_201_CREATED,
)
def add_bank_account(
    application_id: UUID,
    body: AddBankAccountBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "staff")),
):
    """**Add Bank Account** — staff-only manual add (Dave: vendor users cannot).

    Persists to the SELECTED party's borrower profile with ``source='manual'``.
    The full account number is Fernet-encrypted at rest and never returned;
    the response carries the mask.
    """
    app_row = _get_application(db, application_id)
    target = _resolve(db, app_row, body.party, body.co_application_id)
    # Validate the dialog input BEFORE any further lookup: a malformed
    # institution number is a 422 about the field, not a 404 about the record.
    try:
        clean = application_actions.validate_bank_account_input(
            bank_name=body.bank_name,
            institution_number=body.institution_number,
            transit_number=body.transit_number,
            account_number=body.account_number,
            account_type=body.account_type,
            account_holder=body.account_holder,
        )
    except ActionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    _patient(db, target.patient_id)

    row = PlatformPatientBankAccount(
        id=uuid4(),
        patient_id=target.patient_id,
        institution_name=clean.bank_name,
        currency="CAD",
        account_type=clean.account_type,
        institution_number=clean.institution_number,
        transit_number=clean.transit_number,
        account_holder=clean.account_holder,
        routing_mask=f"{clean.institution_number}-{clean.transit_number}",
        account_mask=mask_identifier(clean.account_number, keep=4) or "••••",
        account_number_encrypted=application_actions.encrypt_account_number(
            clean.account_number
        ),
        source=application_actions.SOURCE_MANUAL,
        verified_via="manual_staff",
        is_default=False,
        status="active",
        added_by=_actor(user),
    )
    db.add(row)
    db.flush()

    if body.make_default:
        others = (
            db.query(PlatformPatientBankAccount)
            .filter(
                PlatformPatientBankAccount.patient_id == target.patient_id,
                PlatformPatientBankAccount.is_default.is_(True),
                PlatformPatientBankAccount.id != row.id,
            )
            .all()
        )
        for other in others:
            other.is_default = False
        db.flush()
        row.is_default = True

    _audit(
        db,
        event_type="admin_bank_account_added",
        actor=_actor(user),
        application_id=target.application_id,
        patient_id=target.patient_id,
        payload={
            "v": 1,
            "actor": {"type": "staff", "id": _actor(user)},
            "application_id": str(target.application_id),
            "party": target.party,
            "account_id": str(row.id),
            "bank_name": clean.bank_name,
            "institution_number": clean.institution_number,
            "transit_number": clean.transit_number,
            # Mask only — the full number never enters an event payload.
            "account_mask": row.account_mask,
            "account_type": clean.account_type,
            "source": application_actions.SOURCE_MANUAL,
            "made_default": bool(body.make_default),
        },
    )
    db.commit()
    db.refresh(row)
    return _bank_row(row)


class BankVerificationBody(BaseModel):
    party: str = Field("borrower", pattern="^(borrower|co_borrower)$")
    co_application_id: Optional[UUID] = None


class BankVerificationOut(BaseModel):
    verification_id: UUID
    application_id: UUID
    patient_id: UUID
    party: str
    status: str
    vendor: Optional[str] = None
    vendor_session_ref: Optional[str] = None
    connect_url: Optional[str] = None
    simulated: bool
    simulated_reason: Optional[str] = None


@router.post(
    "/{application_id}/bank-verification",
    response_model=BankVerificationOut,
    status_code=http_status.HTTP_201_CREATED,
)
def request_bank_verification(
    application_id: UUID,
    body: BankVerificationBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "staff")),
):
    """**Bank Verification** — fire a Flinks request at borrower or co-borrower.

    Flinks has no production credentials, so ``VerificationDispatcher`` routes
    through the simulator; the request/record path is identical either way and
    the response says which ran (``simulated``).
    """
    app_row = _get_application(db, application_id)
    target = _resolve(db, app_row, body.party, body.co_application_id)
    _patient(db, target.patient_id)

    from app.services.verifications.dispatcher import VerificationDispatcher

    dispatch = VerificationDispatcher(db).initiate(
        "bank_link",
        target.application_id,
        target.patient_id,
        {"requested_by": _actor(user), "party": target.party},
    )
    simulated = application_actions.bank_verification_is_simulated(dispatch.vendor)

    verification = PlatformVerification(
        id=uuid4(),
        patient_id=target.patient_id,
        application_id=target.application_id,
        verification_type="bank_link",
        status="pending",
        vendor=dispatch.vendor,
        vendor_session_ref=dispatch.vendor_session_ref,
        cost_cents=dispatch.cost_cents,
    )
    db.add(verification)
    db.flush()

    _audit(
        db,
        event_type="admin_bank_verification_requested",
        actor=_actor(user),
        application_id=target.application_id,
        patient_id=target.patient_id,
        payload={
            "v": 1,
            "actor": {"type": "staff", "id": _actor(user)},
            "application_id": str(target.application_id),
            "party": target.party,
            "verification_id": str(verification.id),
            "vendor": dispatch.vendor,
            "vendor_session_ref": dispatch.vendor_session_ref,
            "simulated": simulated,
        },
    )
    db.commit()
    db.refresh(verification)
    return BankVerificationOut(
        verification_id=verification.id,
        application_id=target.application_id,
        patient_id=target.patient_id,
        party=target.party,
        status=verification.status,
        vendor=dispatch.vendor,
        vendor_session_ref=dispatch.vendor_session_ref,
        connect_url=dispatch.redirect_url,
        simulated=simulated,
        simulated_reason=(
            "Flinks is running in simulator mode (no production credentials); "
            "no real bank connection was opened."
            if simulated
            else None
        ),
    )


# ---------------------------------------------------------------------------
# 3 + 4. Contacts tab — Send Email, Send SMS
# ---------------------------------------------------------------------------


class AdHocEmailBody(BaseModel):
    """Dave's Send Email dialog: subject + body, optional template.

    ``notification_type`` seeds subject/body from the registry when the staff
    member picked a template and did not edit it; anything they typed wins.
    """

    party: str = Field("borrower", pattern="^(borrower|co_borrower)$")
    co_application_id: Optional[UUID] = None
    subject: Optional[str] = Field(None, max_length=300)
    body: Optional[str] = None
    notification_type: Optional[str] = None
    context: dict[str, Any] = Field(default_factory=dict)
    comment: Optional[str] = Field(None, max_length=2000)


class AdHocSmsBody(BaseModel):
    party: str = Field("borrower", pattern="^(borrower|co_borrower)$")
    co_application_id: Optional[UUID] = None
    body: Optional[str] = None
    notification_type: Optional[str] = None
    context: dict[str, Any] = Field(default_factory=dict)
    comment: Optional[str] = Field(None, max_length=2000)


class AdHocSendOut(BaseModel):
    communication_id: Optional[UUID] = None
    #: The ``notification_sent`` audit event id (absent only if the session has
    #: not assigned one — e.g. unit tests with a fake session).
    source_event_id: Optional[int] = None
    channel: str
    party: str
    application_id: UUID
    patient_id: UUID
    recipient: Optional[str] = None
    subject: Optional[str] = None
    body: str
    notification_type: str
    status: str
    #: True while SendGrid / Twilio credentials are absent — the message is
    #: rendered + logged but no vendor call is made.
    simulated: bool
    simulated_reason: Optional[str] = None


def _seed_from_template(
    channel: str, notification_type: Optional[str], context: dict
) -> tuple[Optional[str], Optional[str]]:
    """Render the optionally-selected template; ``(None, None)`` when none."""
    if not notification_type:
        return None, None
    if notification_type not in NOTIFICATION_TYPES:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Unknown notification_type '{notification_type}'",
        )
    try:
        if channel == "email":
            return render_email(notification_type, context)
        return None, render_sms(notification_type, context)
    except UnknownNotificationType as exc:  # pragma: no cover - guarded above
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except ValueError as exc:  # email-only template on the sms channel
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except TemplateError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Template render failed: {exc}",
        ) from exc


def _dispatcher(db: Session):
    """Same selector as the notification processor: the mock records the send
    inert (renders + logs, no vendor call) until real creds are configured."""
    if settings.USE_REAL_NOTIFICATIONS:
        from app.services.real_notification_dispatcher import (
            RealNotificationDispatcher,
        )

        return RealNotificationDispatcher(db)
    from app.services.mock_notification_dispatcher import MockNotificationDispatcher

    return MockNotificationDispatcher(db)


def _send_adhoc(
    db: Session,
    user: User,
    *,
    channel: str,
    app_row: PlatformCreditApplication,
    party: str,
    co_application_id: Optional[UUID],
    subject: Optional[str],
    body: Optional[str],
    notification_type: Optional[str],
    context: dict,
    comment: Optional[str],
) -> AdHocSendOut:
    target = _resolve(db, app_row, party, co_application_id)
    patient = _patient(db, target.patient_id)

    seed_subject, seed_body = _seed_from_template(channel, notification_type, context)
    final_subject = (subject or seed_subject) if channel == "email" else None
    final_body = body or seed_body
    if not (final_body or "").strip():
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A message body is required (type one or select a template).",
        )
    if channel == "email" and not (final_subject or "").strip():
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A subject is required for email.",
        )

    recipient = patient.email if channel == "email" else patient.phone_e164
    if not recipient:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"The selected {target.party.replace('_', '-')} has no "
                f"{'email address' if channel == 'email' else 'phone number'} on file."
            ),
        )

    log_type = notification_type or f"adhoc_{channel}"
    simulated = not settings.USE_REAL_NOTIFICATIONS
    dispatcher = _dispatcher(db)
    try:
        event_id = dispatcher.send_notification(
            patient_id=target.patient_id,
            notification_type=log_type,
            contact_method=channel,
            context=context,
            application_id=target.application_id,
            sent_by=_actor_label(user),
            sent_by_user_id=getattr(user, "id", None),
            comment=comment,
            subject_override=final_subject,
            body_override=final_body,
        )
    except PermanentNotificationError as exc:
        db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except TransientNotificationError as exc:
        db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail="Notification vendor unavailable; not sent. Try again.",
        ) from exc

    _audit(
        db,
        event_type="admin_adhoc_message_sent",
        actor=_actor(user),
        application_id=target.application_id,
        patient_id=target.patient_id,
        payload={
            "v": 1,
            "actor": {"type": "staff", "id": _actor(user)},
            "application_id": str(target.application_id),
            "party": target.party,
            "channel": channel,
            "notification_type": log_type,
            "template_used": notification_type,
            "subject": final_subject,
            "source_event_id": event_id,
            "simulated": simulated,
        },
    )
    db.commit()

    log_row = (
        db.query(PlatformCommunicationLog)
        .filter(PlatformCommunicationLog.source_event_id == event_id)
        .order_by(PlatformCommunicationLog.created_at.desc())
        .first()
    )
    return AdHocSendOut(
        communication_id=getattr(log_row, "id", None),
        source_event_id=event_id,
        channel=channel,
        party=target.party,
        application_id=target.application_id,
        patient_id=target.patient_id,
        recipient=recipient,
        subject=final_subject,
        body=final_body,
        notification_type=log_type,
        status=getattr(log_row, "status", "sent") or "sent",
        simulated=simulated,
        simulated_reason=(
            "Delivery vendors are not configured (USE_REAL_NOTIFICATIONS is "
            "off); the message was rendered and logged but not transmitted."
            if simulated
            else None
        ),
    )


@router.post(
    "/{application_id}/send-email",
    response_model=AdHocSendOut,
    status_code=http_status.HTTP_201_CREATED,
)
def send_email(
    application_id: UUID,
    body: AdHocEmailBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "staff")),
):
    """**Send Email** — ad-hoc staff email, through the notification outbox path
    so it lands in the communications log with the FULL rendered body."""
    app_row = _get_application(db, application_id)
    return _send_adhoc(
        db,
        user,
        channel="email",
        app_row=app_row,
        party=body.party,
        co_application_id=body.co_application_id,
        subject=body.subject,
        body=body.body,
        notification_type=body.notification_type,
        context=body.context,
        comment=body.comment,
    )


@router.post(
    "/{application_id}/send-sms",
    response_model=AdHocSendOut,
    status_code=http_status.HTTP_201_CREATED,
)
def send_sms(
    application_id: UUID,
    body: AdHocSmsBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "staff")),
):
    """**Send SMS** — ad-hoc staff SMS, same log/audit path as Send Email."""
    app_row = _get_application(db, application_id)
    return _send_adhoc(
        db,
        user,
        channel="sms",
        app_row=app_row,
        party=body.party,
        co_application_id=body.co_application_id,
        subject=None,
        body=body.body,
        notification_type=body.notification_type,
        context=body.context,
        comment=body.comment,
    )


# ---------------------------------------------------------------------------
# 5 + 6. Credit Report tab — Hard Pull, Soft Pull
# ---------------------------------------------------------------------------


class CreditPullBody(BaseModel):
    pull_type: str = Field(..., pattern="^(soft|hard)$")
    party: str = Field("borrower", pattern="^(borrower|co_borrower)$")
    co_application_id: Optional[UUID] = None


class CreditPullOut(BaseModel):
    pull_id: UUID
    application_id: UUID
    patient_id: UUID
    party: str
    pull_type: str
    bureau: str
    score: Optional[int] = None
    result: str
    bankruptcy: bool
    report: dict
    simulated: bool
    simulated_reason: Optional[str] = None
    requested_by: str
    created_at: Optional[datetime] = None


def _pull_out(row: PlatformCreditReportPull, party: str) -> CreditPullOut:
    return CreditPullOut(
        pull_id=row.id,
        application_id=row.application_id,
        patient_id=row.patient_id,
        party=party,
        pull_type=row.pull_type,
        bureau=row.bureau,
        score=row.score,
        result=row.result,
        bankruptcy=bool(row.bankruptcy),
        report=row.report or {},
        simulated=bool(row.simulated),
        simulated_reason=(
            application_actions.BUREAU_SIMULATED_REASON if row.simulated else None
        ),
        requested_by=row.requested_by,
        created_at=row.created_at,
    )


@router.get("/{application_id}/credit-report", response_model=list[CreditPullOut])
def list_credit_report_pulls(
    application_id: UUID,
    db: Session = Depends(get_db),
):
    """The Credit Report tab: every stored pull for this file, newest first."""
    app_row = _get_application(db, application_id)
    rows = (
        db.query(PlatformCreditReportPull)
        .filter(PlatformCreditReportPull.application_id == app_row.id)
        .order_by(PlatformCreditReportPull.created_at.desc())
        .all()
    )
    return [_pull_out(r, r.subject) for r in rows]


@router.post(
    "/{application_id}/credit-report/pull",
    response_model=CreditPullOut,
    status_code=http_status.HTTP_201_CREATED,
)
async def run_credit_report_pull(
    application_id: UUID,
    body: CreditPullBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "staff")),
):
    """**Hard Pull** / **Soft Pull** — admin-initiated bureau pull, stored
    against the application.

    The Equifax subscriber agreement is NOT signed, so this runs the MOCK
    bureau adapter and the stored row is permanently flagged ``simulated``.
    The response repeats the flag and the reason so the UI can never present a
    synthetic report as a real bureau file.
    """
    app_row = _get_application(db, application_id)
    target = _resolve(db, app_row, body.party, body.co_application_id)
    patient = _patient(db, target.patient_id)

    from app.services.adapters.mock_bureau import MockBureauAdapter

    # Bureau always goes through the mock until the subscriber agreement lands
    # (VerificationDispatcher enforces the same rule for the applicant flow).
    adapter = MockBureauAdapter()
    simulated = True
    profile = PatientProfile(patient_id=target.patient_id, email=patient.email)
    result = (
        await adapter.hard_pull(profile)
        if body.pull_type == "hard"
        else await adapter.soft_pull(profile)
    )

    verification = PlatformVerification(
        id=uuid4(),
        patient_id=target.patient_id,
        application_id=target.application_id,
        verification_type=(
            "bureau_hard" if body.pull_type == "hard" else "bureau_soft"
        ),
        status="passed" if result.result == "passed" else "manual_review",
        vendor=result.vendor,
        vendor_session_ref=None,
        completed_at=datetime.now(timezone.utc),
    )
    db.add(verification)
    db.flush()

    row = PlatformCreditReportPull(
        id=uuid4(),
        application_id=target.application_id,
        patient_id=target.patient_id,
        subject=target.party,
        pull_type=body.pull_type,
        bureau=result.vendor or "mock_bureau",
        score=result.score,
        result=result.result,
        bankruptcy=bool(result.bankruptcy),
        report=application_actions.bureau_result_payload(result),
        simulated=simulated,
        requested_by=_actor(user),
        verification_id=verification.id,
    )
    db.add(row)
    db.flush()

    _audit(
        db,
        event_type="admin_credit_report_pulled",
        actor=_actor(user),
        application_id=target.application_id,
        patient_id=target.patient_id,
        payload={
            "v": 1,
            "actor": {"type": "staff", "id": _actor(user)},
            "application_id": str(target.application_id),
            "party": target.party,
            "pull_id": str(row.id),
            "pull_type": body.pull_type,
            "bureau": row.bureau,
            "score": result.score,
            "result": result.result,
            "simulated": simulated,
            "verification_id": str(verification.id),
        },
    )
    db.commit()
    db.refresh(row)
    return _pull_out(row, target.party)
