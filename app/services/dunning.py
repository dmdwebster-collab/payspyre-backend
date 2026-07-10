"""Dunning scan (WS3) — emits payment reminder / overdue events.

Time-based half of the notification architecture: scans the loan amortization
schedule and, for each installment that hits a configured milestone (N days
before due, or N days past due), emits a synthetic ``payment_due_reminder`` /
``payment_overdue`` event into ``platform_events``. The notification processor
(WS2) then renders + sends those — so dunning never calls a vendor directly and
reuses the whole send/dedup/suppression/audit path.

Idempotent: each milestone for each installment is emitted at most once, keyed by
``dunning_key = "<schedule_item_id>:<milestone>"`` and deduped against the event
log. Safe to re-run any number of times on the same day.

POLICY: every business-policy knob lives in ``DunningPolicy`` below with the
launch-recommended defaults (see docs/dunning_policy_questions.md). Dave's answers
are one-line edits here — the scan logic is policy-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Policy — launch-recommended defaults (docs/dunning_policy_questions.md)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DunningPolicy:
    """Notification cadence. These offsets are data, not hardcoded logic — the
    scan triggers a reminder for any installment whose due date is exactly N days
    away (or N days past). The long-term home for these is a system-configurable
    notification-rule table (create N reminders, set offset days + channels per
    rule); this dataclass is the seam that table will populate. Defaults below
    are PaySpyre's current cadence (confirmed by David Wilson, 2026-06-22).
    """
    # Proactive reminders BEFORE the due date: 7 days + 2 days out. The 7-day
    # lead gives borrowers time to flag a problem or request a reschedule.
    reminder_days_before: tuple[int, ...] = (7, 2)
    # Overdue escalation AFTER the due date; collections beyond 90d are manual.
    overdue_days_after: tuple[int, ...] = (7, 14, 21, 30, 60, 90)
    # Channels per message type. Currently email-only across the board; SMS is a
    # planned per-notification opt-in (see notification-channel config backlog).
    reminder_channels: tuple[str, ...] = ("email",)
    overdue_channels: tuple[str, ...] = ("email",)
    # NO late fees. Canadian provincial consumer-protection legislation restricts
    # them; enabling per-province requires legal clarification. (Turnkey carried a
    # default late-fee option; we do not use it.)
    late_fee_enabled: bool = False


DEFAULT_POLICY = DunningPolicy()

# Loan statuses we actively chase (disbursed loans only).
_CHASEABLE_LOAN_STATUSES = ("active", "delinquent")
# Installment statuses that still owe money AND may be chased. ``suspended``
# (schedule surgery, WS-F) is DELIBERATELY excluded: a staff-suspended
# installment must never generate reminders/overdue notices — that is the
# entire point of suspending it. Pinned by tests/test_schedule_surgery.py.
_OPEN_ITEM_STATUSES = ("scheduled", "partial", "late")

REMINDER_EVENT = "payment_due_reminder"
OVERDUE_EVENT = "payment_overdue"


@dataclass
class DunningResult:
    as_of: date
    reminders_emitted: int = 0
    overdue_emitted: int = 0
    skipped_duplicate: int = 0
    emitted_keys: list[str] = field(default_factory=list)


def _fmt_cents(cents: Optional[int]) -> str:
    return "${:,.2f}".format((cents or 0) / 100)


def _fmt_date(d: date) -> str:
    return d.strftime("%B %d, %Y")


def run_dunning_scan(
    db: Session, as_of: date, policy: DunningPolicy = DEFAULT_POLICY
) -> DunningResult:
    """Emit reminder/overdue events for installments hitting a milestone on
    ``as_of``. Caller commits. Idempotent via the per-(item, milestone) key.

    Cadence (offsets) and channels are read from the DB-backed notification-rule
    config (``platform_notification_rules``); the ``policy`` dataclass is the
    typed fallback used per-type when no row exists (see
    :func:`app.services.notification_config.get_rule`). This is the seam Turnkey
    parity required: the team edits the config table, not this code.
    """
    # Late import: notification_config imports from this module (DunningPolicy),
    # so import here to avoid a circular import at module load.
    from app.services.notification_config import get_rule

    result = DunningResult(as_of=as_of)

    reminder_rule = get_rule(db, REMINDER_EVENT, policy)
    overdue_rule = get_rule(db, OVERDUE_EVENT, policy)

    # Pre-due reminders: due_date == as_of + N. Skip entirely if the rule is
    # disabled or has no enabled channels (the processor also re-checks channels,
    # but not emitting at all keeps the event log clean).
    if reminder_rule.enabled and reminder_rule.enabled_channels:
        for n in reminder_rule.offsets:
            target = _add_days(as_of, n)
            for row in _items_due_on(db, target):
                key = f"{row['item_id']}:due-{n}"
                if _already_emitted(db, key):
                    result.skipped_duplicate += 1
                    continue
                ctx = _context(row, days_until_due=n, days_overdue=None)
                _emit(
                    db, REMINDER_EVENT, row, key,
                    list(reminder_rule.enabled_channels), ctx,
                )
                result.reminders_emitted += 1
                result.emitted_keys.append(key)

    # Overdue escalation: due_date == as_of - N.
    if overdue_rule.enabled and overdue_rule.enabled_channels:
        for n in overdue_rule.offsets:
            target = _add_days(as_of, -n)
            for row in _items_due_on(db, target):
                key = f"{row['item_id']}:overdue-{n}"
                if _already_emitted(db, key):
                    result.skipped_duplicate += 1
                    continue
                ctx = _context(row, days_until_due=None, days_overdue=n)
                _emit(
                    db, OVERDUE_EVENT, row, key,
                    list(overdue_rule.enabled_channels), ctx,
                )
                result.overdue_emitted += 1
                result.emitted_keys.append(key)

    logger.info(
        "dunning_scan_complete",
        as_of=as_of.isoformat(),
        reminders=result.reminders_emitted,
        overdue=result.overdue_emitted,
        duplicates=result.skipped_duplicate,
    )
    return result


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _add_days(d: date, n: int) -> date:
    from datetime import timedelta

    return d + timedelta(days=n)


def _items_due_on(db: Session, target: date) -> list[dict]:
    """Open installments on chaseable loans with ``due_date == target``, joined
    to the loan + borrower so we can build the notification context."""
    stmt = text(
        """
        SELECT s.id            AS item_id,
               s.total_cents   AS total_cents,
               s.paid_cents    AS paid_cents,
               s.due_date      AS due_date,
               l.id            AS loan_id,
               l.application_id AS application_id,
               a.patient_id    AS patient_id,
               p.legal_first_name AS first_name,
               p.legal_last_name  AS last_name
        FROM platform_loan_schedule s
        JOIN platform_loans l ON l.id = s.loan_id
        JOIN platform_credit_applications a ON a.id = l.application_id
        JOIN platform_patients p ON p.id = a.patient_id
        WHERE s.due_date = :target
          AND s.status IN :open_statuses
          AND l.status IN :loan_statuses
        """
    ).bindparams(
        bindparam("open_statuses", expanding=True),
        bindparam("loan_statuses", expanding=True),
    )
    rows = db.execute(
        stmt,
        {
            "target": target,
            "open_statuses": list(_OPEN_ITEM_STATUSES),
            "loan_statuses": list(_CHASEABLE_LOAN_STATUSES),
        },
    ).mappings().all()
    return [dict(r) for r in rows]


def _context(row: dict, *, days_until_due: Optional[int], days_overdue: Optional[int]) -> dict:
    name = " ".join(p for p in (row.get("first_name"), row.get("last_name")) if p).strip() or "there"
    outstanding = max(0, (row["total_cents"] or 0) - (row.get("paid_cents") or 0))
    base = settings.BORROWER_PORTAL_BASE_URL.rstrip("/")
    ctx = {
        "borrower_name": name,
        "loan_id": str(row["loan_id"])[:8],
        "payment_amount": _fmt_cents(outstanding),
        "due_date": _fmt_date(row["due_date"]),
        "payment_url": f"{base}/account",
        "account_url": f"{base}/account",
    }
    if days_until_due is not None:
        ctx["days_until_due"] = days_until_due
    if days_overdue is not None:
        ctx["days_overdue"] = days_overdue
    return ctx


def _already_emitted(db: Session, dunning_key: str) -> bool:
    stmt = text(
        """
        SELECT 1 FROM platform_events
        WHERE event_type IN :types
          AND payload->>'dunning_key' = :key
        LIMIT 1
        """
    ).bindparams(bindparam("types", expanding=True))
    return (
        db.execute(stmt, {"types": [REMINDER_EVENT, OVERDUE_EVENT], "key": dunning_key}).first()
        is not None
    )


def _emit(
    db: Session, event_type: str, row: dict, dunning_key: str, channels: list[str], context: dict
) -> None:
    from app.models.platform.event import PlatformEvent

    application_id = row["application_id"]
    patient_id = row["patient_id"]
    payload = {
        "v": 1,
        "actor": {"type": "system", "id": "system"},
        "application_id": str(application_id) if application_id else None,
        "patient_id": str(patient_id) if patient_id else None,
        "loan_id": str(row["loan_id"]),
        "dunning_key": dunning_key,
        "channels": channels,
        "context": context,
    }
    db.add(
        PlatformEvent(
            event_type=event_type,
            actor="system",
            patient_id=patient_id,
            application_id=application_id,
            payload=payload,
        )
    )
    db.flush()
