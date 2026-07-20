"""Hardship v1 (WS-J) — deferment + due-date change, e-sign gated.

Dave (03__WP_Servicing §5): "We need something that is more aptly labeled as
HARDSHIP … anything that is going to trigger the need to send authorization to
the borrower", and "we can't unilaterally change those things without the
borrower first signing legal documentation."

THE HARD RULE (money/legal path, pinned by tests): **no schedule mutation
before ``signed_at`` is set.** The flow is:

    create_request      — validates + computes the exact schedule-effect
                          PREVIEW; persists a ``draft``. Nothing touches the
                          schedule.
    send_for_signature  — builds a structured amendment summary (a full
                          merge-field document engine is P1) and sends it via
                          the existing SignNow adapter; ``awaiting_signature``
                          with a config signature window (default 30 days).
                          In simulation mode (no adapter configured) the
                          request still advances and the NON-PROD dev
                          force-sign endpoint substitutes for the borrower.
    mark_signed         — entry point for the SignNow completion webhook (and
                          the dev force-sign). Sets ``signed_at`` FIRST, then
                          APPLIES the change by COMPOSING WS-F's
                          schedule-surgery primitives (suspend installments +
                          append end-of-contract custom transactions); the
                          ``snap_back`` snapshot is captured before mutation.
    decline / cancel / expire — terminal, nothing applied.
    run_maintenance     — expires stale signature windows and marks elapsed
                          deferments ``completed``.

v1 completion semantics: applied changes PERSIST (deferred installments stay
suspended with their amounts appended as custom transactions at contract end;
shifted due dates keep their new dates). The full "temporary Adjustment of
Terms with automatic snap-back at expiry" machine is the documented P1
follow-up — ``snap_back`` already stores everything it needs.

Interest during a deferment KEEPS ACCRUING on the outstanding principal (the
actuals engine charges per-diem on what is actually outstanding — WS-A). The
preview states this plainly and carries an integer-cents estimate.

Commit ownership follows WS-F: functions add + flush only; callers commit.
Money is integer cents. No PII in event payloads beyond the staff actor id.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.platform.event import PlatformEvent
from app.models.platform.hardship import PlatformHardshipRequest
from app.models.platform.loan import PlatformLoan, PlatformLoanScheduleItem
from app.services import schedule_surgery
from app.services.interest_engine import accrue_interest_cents

logger = get_logger(__name__)

# Event types — the audit trail (every transition is a platform_events row).
REQUEST_CREATED_EVENT = "hardship_request_created"
SENT_FOR_SIGNATURE_EVENT = "hardship_sent_for_signature"
APPLIED_EVENT = "hardship_applied"
DECLINED_EVENT = "hardship_declined"
CANCELLED_EVENT = "hardship_cancelled"
EXPIRED_EVENT = "hardship_expired"
COMPLETED_EVENT = "hardship_completed"

# Synthetic trigger event for the borrower notification (outbox lane — the
# NotificationProcessor fans it out; see TRIGGER_EVENT_TYPES there).
AGREEMENT_NOTIFICATION_EVENT = "hardship_agreement_sent"

# Statuses an installment can be deferred FROM (mirrors WS-F's suspendable set:
# paid/waived carry nothing to defer; suspended is already parked).
_DEFERRABLE_ITEM_STATUSES = ("scheduled", "partial", "late")
# Due-date changes only touch OPEN FUTURE installments — rewriting a past-due
# item's due date would rewrite delinquency history (month-end / vendor
# distribution impact needs human review per Dave, and 'late' re-derivation is
# owned by the aging job).
_SHIFTABLE_ITEM_STATUSES = ("scheduled", "partial")


@dataclass(frozen=True)
class HardshipPolicy:
    """Every hardship policy knob in ONE place — recommended defaults,
    FLAGGED FOR DAVE (never a business decision made here):

    * ``max_deferred_installments`` / ``rolling_window_months`` — at most N
      installments deferred per rolling window (defaults: 3 per 12 months).
    * ``signature_window_days`` — how long the borrower has to sign the
      amendment before the request expires (default 30).
    * ``due_day_min`` / ``due_day_max`` — a due-date change must land on a
      day that exists in EVERY month (1..28), so no month-skew surprises.
    """

    max_deferred_installments: int = 3
    rolling_window_months: int = 12
    signature_window_days: int = 30
    due_day_min: int = 1
    due_day_max: int = 28


DEFAULT_POLICY = HardshipPolicy()


class HardshipError(ValueError):
    """A hardship rule violation (bad state / bad params) — maps to 4xx."""


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _add_months(d: date, months: int) -> date:
    """Calendar-safe month addition (day clamped to the target month's end)."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _require_text(value: Optional[str], label: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise HardshipError(f"a {label} is required")
    return cleaned


def _emit(
    db: Session,
    event_type: str,
    request: PlatformHardshipRequest,
    actor: str,
    payload_extra: dict,
) -> None:
    db.add(
        PlatformEvent(
            event_type=event_type,
            actor=actor,
            application_id=None,
            payload={
                "v": 1,
                "actor": {"type": "staff", "id": actor},
                "hardship_request_id": str(request.id),
                "loan_id": str(request.loan_id),
                "kind": request.kind,
                "status": request.status,
                **payload_extra,
            },
        )
    )
    db.flush()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _items_by_id(loan: PlatformLoan) -> dict:
    return {str(item.id): item for item in loan.schedule}


def _outstanding_cents(item: PlatformLoanScheduleItem) -> int:
    return max(0, (item.total_cents or 0) - (item.paid_cents or 0))


# ---------------------------------------------------------------------------
# Validation + preview (create — draft; NOTHING touches the schedule)
# ---------------------------------------------------------------------------


def _deferred_count_in_window(
    db: Session,
    loan: PlatformLoan,
    policy: HardshipPolicy,
    as_of: Optional[datetime] = None,
) -> int:
    """Installments already deferred for this loan by hardship requests that
    took effect (active/completed) inside the rolling window. Python-side
    filtering keeps this trivially fake-able in DB-free tests."""
    as_of = as_of or _now()
    cutoff = as_of - timedelta(days=policy.rolling_window_months * 31)
    rows = (
        db.query(PlatformHardshipRequest)
        .filter(PlatformHardshipRequest.loan_id == loan.id)
        .all()
    )
    count = 0
    for row in rows:
        if row.kind != "deferment" or row.status not in ("active", "completed"):
            continue
        applied = row.applied_at
        if applied is None:
            continue
        if applied.tzinfo is None:
            applied = applied.replace(tzinfo=timezone.utc)
        if applied < cutoff:
            continue
        count += len((row.params or {}).get("installment_ids", []))
    return count


def _validate_and_preview_deferment(
    db: Session, loan: PlatformLoan, params: dict, policy: HardshipPolicy
) -> dict:
    ids = params.get("installment_ids") or []
    if not isinstance(ids, list) or not ids:
        raise HardshipError("deferment requires a non-empty installment_ids list")
    if len(set(str(i) for i in ids)) != len(ids):
        raise HardshipError("duplicate installment ids in deferment request")

    by_id = _items_by_id(loan)
    items: list[PlatformLoanScheduleItem] = []
    for raw in ids:
        item = by_id.get(str(raw))
        if item is None:
            raise HardshipError("schedule item not found on this loan")
        if item.status not in _DEFERRABLE_ITEM_STATUSES:
            raise HardshipError(
                f"installment {item.installment_number} cannot be deferred from "
                f"status '{item.status}' (must be one of {_DEFERRABLE_ITEM_STATUSES})"
            )
        items.append(item)
    items.sort(key=lambda i: i.installment_number)

    already = _deferred_count_in_window(db, loan, policy)
    if already + len(items) > policy.max_deferred_installments:
        raise HardshipError(
            f"deferment limit exceeded: {already} installment(s) already deferred in the "
            f"rolling {policy.rolling_window_months}-month window; requesting {len(items)} "
            f"more would exceed the maximum of {policy.max_deferred_installments}"
        )

    # The deferred amounts append as custom transactions AFTER the contract's
    # last scheduled installment, one per deferred item, one interval apart.
    contract_end = max(i.due_date for i in loan.schedule)
    changes = []
    estimated_extra_interest = 0
    for n, item in enumerate(items, start=1):
        new_date = _add_months(contract_end, n)
        amount = _outstanding_cents(item)
        delta_days = max(0, (new_date - item.due_date).days)
        # Estimate: the item's principal portion stays outstanding for the
        # extra days and accrues per-diem (the actuals engine charges what is
        # ACTUALLY outstanding, so the real figure follows payment behaviour).
        extra = accrue_interest_cents(
            item.principal_cents, loan.annual_rate_bps, delta_days
        )
        estimated_extra_interest += extra
        changes.append(
            {
                "action": "suspend_and_append",
                "schedule_item_id": str(item.id),
                "installment_number": item.installment_number,
                "original_due_date": item.due_date.isoformat(),
                "new_scheduled_date": new_date.isoformat(),
                "amount_cents": amount,
                "deferred_days": delta_days,
                "estimated_additional_interest_cents": extra,
            }
        )

    return {
        "kind": "deferment",
        "changes": changes,
        "deferred_installments": len(items),
        "deferred_in_window_before_this": already,
        "estimated_additional_interest_cents": estimated_extra_interest,
        "interest_disclosure": (
            "Interest continues to accrue daily on your outstanding principal "
            "during the deferment. Deferring these installments is estimated to "
            "add the amount shown in additional interest over the life of the "
            "loan; the exact amount depends on your actual payment dates."
        ),
    }


def _validate_and_preview_due_date_change(
    loan: PlatformLoan, params: dict, policy: HardshipPolicy
) -> dict:
    new_day = params.get("new_day_of_month")
    item_shifts = params.get("item_shifts")
    if bool(new_day is not None) == bool(item_shifts):
        raise HardshipError(
            "due_date_change requires exactly one of new_day_of_month or item_shifts"
        )

    today = date.today()
    changes = []
    estimated_extra_interest = 0

    if new_day is not None:
        if not isinstance(new_day, int) or not (
            policy.due_day_min <= new_day <= policy.due_day_max
        ):
            raise HardshipError(
                f"new_day_of_month must be an integer between {policy.due_day_min} "
                f"and {policy.due_day_max} (a day that exists in every month)"
            )
        targets = [
            i
            for i in loan.schedule
            if i.status in _SHIFTABLE_ITEM_STATUSES and i.due_date > today
        ]
        if not targets:
            raise HardshipError("no open future installments to move")
        for item in sorted(targets, key=lambda i: i.installment_number):
            new_date = date(item.due_date.year, item.due_date.month, new_day)
            if new_date == item.due_date:
                continue
            delta_days = (new_date - item.due_date).days
            sign = 1 if delta_days > 0 else -1
            extra = sign * accrue_interest_cents(
                item.principal_cents, loan.annual_rate_bps, abs(delta_days)
            )
            estimated_extra_interest += extra
            changes.append(
                {
                    "action": "shift_due_date",
                    "schedule_item_id": str(item.id),
                    "installment_number": item.installment_number,
                    "original_due_date": item.due_date.isoformat(),
                    "new_due_date": new_date.isoformat(),
                    "shift_days": delta_days,
                    "estimated_interest_impact_cents": extra,
                }
            )
        if not changes:
            raise HardshipError("all open installments are already due on that day")
    else:
        if not isinstance(item_shifts, list) or not item_shifts:
            raise HardshipError("item_shifts must be a non-empty list")
        by_id = _items_by_id(loan)
        for shift in item_shifts:
            item = by_id.get(str(shift.get("item_id")))
            if item is None:
                raise HardshipError("schedule item not found on this loan")
            if item.status not in _SHIFTABLE_ITEM_STATUSES or item.due_date <= today:
                raise HardshipError(
                    f"installment {item.installment_number} is not an open future "
                    f"installment (status '{item.status}', due {item.due_date})"
                )
            try:
                new_date = date.fromisoformat(str(shift.get("new_due_date")))
            except (TypeError, ValueError):
                raise HardshipError("item_shifts entries need a valid new_due_date (YYYY-MM-DD)")
            # Month bound: the shifted date stays inside the installment's
            # original month (a month-end crossing changes vendor-distribution
            # periods and needs human review per Dave — out of v1 scope).
            if (new_date.year, new_date.month) != (item.due_date.year, item.due_date.month):
                raise HardshipError(
                    f"installment {item.installment_number}: the new due date must stay "
                    f"within its original month ({item.due_date.strftime('%B %Y')})"
                )
            if new_date == item.due_date:
                raise HardshipError(
                    f"installment {item.installment_number}: new due date equals the current one"
                )
            delta_days = (new_date - item.due_date).days
            sign = 1 if delta_days > 0 else -1
            extra = sign * accrue_interest_cents(
                item.principal_cents, loan.annual_rate_bps, abs(delta_days)
            )
            estimated_extra_interest += extra
            changes.append(
                {
                    "action": "shift_due_date",
                    "schedule_item_id": str(item.id),
                    "installment_number": item.installment_number,
                    "original_due_date": item.due_date.isoformat(),
                    "new_due_date": new_date.isoformat(),
                    "shift_days": delta_days,
                    "estimated_interest_impact_cents": extra,
                }
            )

    return {
        "kind": "due_date_change",
        "changes": changes,
        "estimated_additional_interest_cents": estimated_extra_interest,
        "interest_disclosure": (
            "Interest accrues daily on your outstanding principal. Moving a due "
            "date later means the principal is outstanding for more days and "
            "accrues more interest (moving it earlier accrues less); the exact "
            "amount depends on your actual payment dates."
        ),
    }


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def create_request(
    db: Session,
    loan: PlatformLoan,
    *,
    kind: str,
    params: dict,
    reason: str,
    comment: str,
    actor: str,
    policy: HardshipPolicy = DEFAULT_POLICY,
) -> PlatformHardshipRequest:
    """Create a DRAFT hardship request with its exact schedule-effect preview.

    Validates everything up front; persists nothing but the request row.
    NOTHING touches the schedule here (the money/legal-path rule).
    """
    reason = _require_text(reason, "reason")
    comment = _require_text(comment, "comment")
    if loan.status not in ("active", "delinquent"):
        raise HardshipError(
            f"hardship applies to a live loan (status '{loan.status}' is not active/delinquent)"
        )

    if kind == "deferment":
        preview = _validate_and_preview_deferment(db, loan, params, policy)
    elif kind == "due_date_change":
        preview = _validate_and_preview_due_date_change(loan, params, policy)
    else:
        raise HardshipError(f"unknown hardship kind {kind!r}")

    request = PlatformHardshipRequest(
        loan_id=loan.id,
        kind=kind,
        params=params,
        status="draft",
        reason=reason,
        comment=comment,
        created_by=actor,
        preview=preview,
    )
    db.add(request)
    db.flush()
    _emit(
        db,
        REQUEST_CREATED_EVENT,
        request,
        actor,
        {
            "reason": reason,
            "comment": comment,
            "change_count": len(preview["changes"]),
            "estimated_additional_interest_cents": preview[
                "estimated_additional_interest_cents"
            ],
        },
    )
    return request


def build_amendment_summary(
    request: PlatformHardshipRequest, loan: PlatformLoan
) -> str:
    """The structured amendment summary the borrower signs.

    v1 is deliberately simple structured text (rendered into the e-sign doc /
    notification): a full merge-field document engine (per-product amendment
    templates with stamped fields) is the documented P1 follow-up.
    """
    preview = request.preview or {}
    kind_label = (
        "Installment deferment" if request.kind == "deferment" else "Due-date change"
    )
    lines = [
        f"LOAN AMENDMENT — HARDSHIP: {kind_label.upper()}",
        f"Loan: {loan.id}",
        f"Reason: {request.reason}",
        "",
        "Changes (effective only after you sign):",
    ]
    for ch in preview.get("changes", []):
        if ch["action"] == "suspend_and_append":
            lines.append(
                f"  - Installment #{ch['installment_number']} "
                f"(due {ch['original_due_date']}, ${ch['amount_cents'] / 100:,.2f} outstanding) "
                f"is deferred to {ch['new_scheduled_date']}."
            )
        else:
            lines.append(
                f"  - Installment #{ch['installment_number']} due date moves from "
                f"{ch['original_due_date']} to {ch['new_due_date']}."
            )
    est = preview.get("estimated_additional_interest_cents", 0)
    lines += [
        "",
        preview.get("interest_disclosure", ""),
        f"Estimated interest impact: ${est / 100:,.2f}.",
        "",
        "No change takes effect until this amendment is signed. All other terms "
        "of your loan agreement remain unchanged.",
    ]
    return "\n".join(lines)


def send_for_signature(
    db: Session,
    request: PlatformHardshipRequest,
    loan: PlatformLoan,
    *,
    actor: str,
    esign=None,
    policy: HardshipPolicy = DEFAULT_POLICY,
) -> PlatformHardshipRequest:
    """Send the amendment for e-signature → ``awaiting_signature``.

    ``esign`` is an optional pre-built SignNow adapter (tests / callers);
    otherwise one is built from integration_settings. SIMULATION MODE: when no
    adapter (or no hardship template) is configured, the request still advances
    to ``awaiting_signature`` with no vendor call — the NON-PROD dev force-sign
    endpoint then substitutes for the borrower's signature. Either way, NOTHING
    touches the schedule until ``mark_signed``.
    """
    if request.status != "draft":
        raise HardshipError(
            f"only a draft can be sent for signature (status '{request.status}')"
        )

    now = _now()
    document_id: Optional[str] = None
    signing_url: Optional[str] = None

    if esign is None:
        from app.services.loan_lifecycle import _build_signnow_adapter

        esign = _build_signnow_adapter(db)

    template_id = None
    if esign is not None:
        from app.services import integration_settings as integration_settings_service

        setting = integration_settings_service.get(db, "signnow")
        if setting is not None:
            config = setting.config or {}
            # A dedicated hardship-amendment template, falling back to the
            # loan-agreement template if that's all the account has.
            template_id = config.get("hardship_template_id") or config.get("template_id")

    if esign is not None and template_id:
        from app.services.loan_lifecycle import _signer_for_loan

        signer = _signer_for_loan(db, loan)
        result = esign.send_for_signature(
            signer=signer,
            template_id=template_id,
            subject="Your loan amendment (hardship) — signature required",
            message=build_amendment_summary(request, loan),
        )
        document_id = result.document_id
        signing_url = result.signing_url
    else:
        logger.info(
            "hardship_esign_simulation_mode",
            hardship_request_id=str(request.id),
            reason="no signnow adapter/template configured",
        )

    request.status = "awaiting_signature"
    request.esign_document_ref = document_id
    request.signature_requested_at = now
    request.signature_expires_at = now + timedelta(days=policy.signature_window_days)
    db.flush()

    _emit(
        db,
        SENT_FOR_SIGNATURE_EVENT,
        request,
        actor,
        {
            "esign_document_ref": document_id,
            "simulation_mode": document_id is None,
            "signature_expires_at": request.signature_expires_at.isoformat(),
        },
    )
    _emit_borrower_notification(db, request, loan, signing_url=signing_url)
    return request


def _emit_borrower_notification(
    db: Session,
    request: PlatformHardshipRequest,
    loan: PlatformLoan,
    *,
    signing_url: Optional[str],
) -> None:
    """Synthetic ``hardship_agreement_sent`` trigger event — the outbox lane's
    NotificationProcessor fans it out to the borrower (email; in-app via rule
    config). Same passthrough shape as the dunning events: channels + fully
    rendered context in the payload, no PII beyond name/URL context fields."""
    from app.core.config import settings
    from app.models.platform.credit_application import PlatformCreditApplication
    from app.models.platform.patient import PlatformPatient

    application = None
    patient = None
    if loan.application_id is not None:
        application = (
            db.query(PlatformCreditApplication)
            .filter(PlatformCreditApplication.id == loan.application_id)
            .first()
        )
    patient_id = getattr(application, "patient_id", None) or loan.patient_id
    if patient_id is not None:
        patient = (
            db.query(PlatformPatient).filter(PlatformPatient.id == patient_id).first()
        )
    name = " ".join(
        p
        for p in (
            getattr(patient, "legal_first_name", None),
            getattr(patient, "legal_last_name", None),
        )
        if p
    ).strip() or "there"

    base = settings.BORROWER_PORTAL_BASE_URL.rstrip("/")
    kind_label = (
        "installment deferment" if request.kind == "deferment" else "due-date change"
    )
    context = {
        "borrower_name": name,
        "kind_label": kind_label,
        "summary": build_amendment_summary(request, loan),
        "signing_url": signing_url or f"{base}/account",
        "expires_date": (
            request.signature_expires_at.date().isoformat()
            if request.signature_expires_at
            else ""
        ),
    }
    db.add(
        PlatformEvent(
            event_type=AGREEMENT_NOTIFICATION_EVENT,
            actor="system",
            patient_id=patient_id,
            application_id=loan.application_id,
            payload={
                "v": 1,
                "actor": {"type": "system", "id": "system"},
                "hardship_request_id": str(request.id),
                "loan_id": str(loan.id),
                "channels": ["email"],
                "context": context,
            },
        )
    )
    db.flush()


def mark_signed(
    db: Session,
    request: PlatformHardshipRequest,
    loan: PlatformLoan,
    *,
    actor: str,
    now: Optional[datetime] = None,
) -> PlatformHardshipRequest:
    """The borrower SIGNED the amendment → apply the change.

    Entry point for the SignNow completion webhook and the non-prod dev
    force-sign. Idempotent: an already-active/completed request is returned
    unchanged (webhook re-delivery safe). Sets ``signed_at`` BEFORE any
    mutation — the apply step hard-asserts it (money/legal-path rule).
    """
    if request.status in ("active", "completed"):
        return request  # idempotent replay
    if request.status != "awaiting_signature":
        raise HardshipError(
            f"cannot record a signature on a '{request.status}' request"
        )

    now = now or _now()
    expires = request.signature_expires_at
    if expires is not None:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if now > expires:
            _expire(db, request, actor="system")
            raise HardshipError("signature window has expired")

    # LEGAL GATE: the signature is recorded first; only then may the schedule
    # change. _apply() asserts signed_at is set.
    request.signed_at = now
    db.flush()

    _apply(db, request, loan, actor=actor)
    return request


def _apply(
    db: Session,
    request: PlatformHardshipRequest,
    loan: PlatformLoan,
    *,
    actor: str,
) -> None:
    """Apply the signed change by composing WS-F surgery primitives.

    ``snap_back`` (original schedule state) is captured BEFORE mutation.
    Deferment: suspend each installment + append its outstanding amount as an
    end-of-contract custom transaction. Due-date change: shift the item due
    dates (no WS-F primitive exists for a date shift; the direct, audited
    column update here is deliberate and minimal — suspension/custom logic is
    NOT reimplemented).
    """
    if request.signed_at is None:  # the money/legal-path invariant
        raise HardshipError("cannot apply an unsigned hardship request")

    preview = request.preview or {}
    changes = preview.get("changes", [])
    by_id = _items_by_id(loan)

    snap_items = []
    custom_ids = []
    applied_detail = []

    # Snapshot BEFORE mutation.
    for ch in changes:
        item = by_id.get(ch["schedule_item_id"])
        if item is None:
            raise HardshipError("schedule item vanished between preview and apply")
        snap_items.append(
            {
                "schedule_item_id": str(item.id),
                "installment_number": item.installment_number,
                "prior_status": item.status,
                "prior_due_date": item.due_date.isoformat(),
                "paid_cents": item.paid_cents,
            }
        )
    request.snap_back = {"items": snap_items, "custom_transaction_ids": []}

    surgery_comment = (
        f"Hardship {request.id} ({request.kind}) — borrower-signed amendment; "
        f"reason: {request.reason}"
    )

    if request.kind == "deferment":
        for ch in changes:
            item = by_id[ch["schedule_item_id"]]
            schedule_surgery.suspend_installment(
                db, loan, item.id, comment=surgery_comment, actor=actor
            )
            row = schedule_surgery.add_custom_transaction(
                db,
                loan,
                scheduled_date=date.fromisoformat(ch["new_scheduled_date"]),
                amount_cents=ch["amount_cents"],
                repayment_mode="regular",
                comment=surgery_comment,
                actor=actor,
            )
            custom_ids.append(str(row.id))
            applied_detail.append(
                {
                    "schedule_item_id": ch["schedule_item_id"],
                    "installment_number": ch["installment_number"],
                    "suspended": True,
                    "custom_transaction_id": str(row.id),
                    "new_scheduled_date": ch["new_scheduled_date"],
                    "amount_cents": ch["amount_cents"],
                }
            )
        request.snap_back = {
            "items": snap_items,
            "custom_transaction_ids": custom_ids,
        }
        request.status = "active"
    else:  # due_date_change — permanent once applied; completes immediately.
        for ch in changes:
            item = by_id[ch["schedule_item_id"]]
            if item.status not in _SHIFTABLE_ITEM_STATUSES:
                raise HardshipError(
                    f"installment {item.installment_number} changed state to "
                    f"'{item.status}' between preview and apply — recreate the request"
                )
            item.due_date = date.fromisoformat(ch["new_due_date"])
            applied_detail.append(
                {
                    "schedule_item_id": ch["schedule_item_id"],
                    "installment_number": ch["installment_number"],
                    "old_due_date": ch["original_due_date"],
                    "new_due_date": ch["new_due_date"],
                }
            )
        request.status = "active"

    request.applied_at = _now()
    db.flush()
    _emit(
        db,
        APPLIED_EVENT,
        request,
        actor,
        {
            "signed_at": request.signed_at.isoformat(),
            "applied": applied_detail,
            "comment": surgery_comment,
        },
    )

    # A due-date change has no ongoing window — it completes on apply.
    if request.kind == "due_date_change":
        _complete(db, request, actor="system")

    logger.info(
        "hardship_applied",
        hardship_request_id=str(request.id),
        loan_id=str(loan.id),
        kind=request.kind,
        changes=len(applied_detail),
    )


def decline(
    db: Session,
    request: PlatformHardshipRequest,
    *,
    actor: str,
    comment: Optional[str] = None,
) -> PlatformHardshipRequest:
    """Borrower declined the amendment → terminal; nothing was ever applied."""
    if request.status != "awaiting_signature":
        raise HardshipError(f"cannot decline a '{request.status}' request")
    request.status = "declined"
    db.flush()
    _emit(db, DECLINED_EVENT, request, actor, {"comment": (comment or "").strip() or None})
    return request


def cancel(
    db: Session,
    request: PlatformHardshipRequest,
    *,
    actor: str,
    comment: str,
) -> PlatformHardshipRequest:
    """Staff withdraw a draft / awaiting-signature request. MANDATORY comment.
    Terminal; nothing applied."""
    cleaned = _require_text(comment, "comment")
    if request.status not in ("draft", "awaiting_signature"):
        raise HardshipError(f"cannot cancel a '{request.status}' request")
    request.status = "cancelled"
    request.cancelled_by = actor
    request.cancelled_at = _now()
    db.flush()
    _emit(db, CANCELLED_EVENT, request, actor, {"comment": cleaned})
    return request


def _expire(db: Session, request: PlatformHardshipRequest, *, actor: str) -> None:
    request.status = "expired"
    db.flush()
    _emit(db, EXPIRED_EVENT, request, actor, {})


def _complete(db: Session, request: PlatformHardshipRequest, *, actor: str) -> None:
    request.status = "completed"
    request.completed_at = _now()
    db.flush()
    _emit(
        db,
        COMPLETED_EVENT,
        request,
        actor,
        {
            # v1: the applied changes persist (see module docstring); full
            # temporary-AoT auto snap-back is the P1 follow-up.
            "snap_back_performed": False,
        },
    )


def run_maintenance(db: Session, *, as_of: Optional[datetime] = None) -> dict:
    """Housekeeping pass (cron-able, also safe to call opportunistically):

    * ``awaiting_signature`` past its window → ``expired`` (nothing applied).
    * ``active`` deferments whose appended custom transactions are all in the
      past → ``completed`` (v1: changes persist — see module docstring).

    Caller owns the commit (same convention as the service's other functions).
    """
    as_of = as_of or _now()
    expired = completed = 0

    stale = (
        db.query(PlatformHardshipRequest)
        .filter(PlatformHardshipRequest.status == "awaiting_signature")
        .all()
    )
    for request in stale:
        # Status re-checked Python-side (same convention as
        # _deferred_count_in_window: keeps the pass trivially fake-able in
        # DB-free tests, and guards against rows this very pass already
        # transitioned).
        if request.status != "awaiting_signature":
            continue
        expires = request.signature_expires_at
        if expires is None:
            continue
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if as_of > expires:
            _expire(db, request, actor="system")
            expired += 1

    active = (
        db.query(PlatformHardshipRequest)
        .filter(PlatformHardshipRequest.status == "active")
        .all()
    )
    for request in active:
        if request.status != "active" or request.kind != "deferment":
            continue
        dates = [
            date.fromisoformat(ch["new_scheduled_date"])
            for ch in (request.preview or {}).get("changes", [])
            if ch.get("new_scheduled_date")
        ]
        if dates and all(d < as_of.date() for d in dates):
            _complete(db, request, actor="system")
            completed += 1

    return {"expired": expired, "completed": completed}


# ---------------------------------------------------------------------------
# E-sign webhook translation (called by the SignNow webhook endpoint)
# ---------------------------------------------------------------------------


def handle_esign_event(db: Session, *, document_id: str, status: str) -> Optional[str]:
    """Route a verified SignNow event at a hardship amendment document.

    Returns a short outcome string when the document belongs to a hardship
    request (``applied`` / ``declined`` / ``ignored``), or ``None`` when it is
    not a hardship document (the webhook then falls through to its existing
    orphan handling). Caller owns the commit.
    """
    request = (
        db.query(PlatformHardshipRequest)
        .filter(PlatformHardshipRequest.esign_document_ref == document_id)
        .first()
    )
    if request is None:
        return None

    loan = db.query(PlatformLoan).filter(PlatformLoan.id == request.loan_id).first()
    if loan is None:
        logger.warning(
            "hardship_esign_loan_missing", hardship_request_id=str(request.id)
        )
        return "ignored"

    if status == "signed":
        try:
            mark_signed(db, request, loan, actor="vendor:signnow")
        except HardshipError as exc:
            # e.g. window expired between send and sign — audited, never a 5xx.
            logger.warning(
                "hardship_esign_sign_rejected",
                hardship_request_id=str(request.id),
                reason=str(exc),
            )
            return "ignored"
        return "applied"
    if status == "declined":
        try:
            decline(db, request, actor="vendor:signnow", comment="Declined via SignNow")
        except HardshipError:
            return "ignored"  # replay of a terminal state
        return "declined"
    return "ignored"
