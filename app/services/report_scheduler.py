"""WS-H scheduled-reports engine (Turnkey parity, video 05 item 6).

A schedule = a report (a stock ``report_exports`` dataset OR a saved builder
definition) + a cadence (daily / weekly / monthly) + recipients (static emails,
or per-vendor fan-out). The cron-callable :func:`run_due_schedules` renders each
DUE schedule's XLSX through the existing ``report_exports`` machinery and emails
it with the artifact attached, through a sender built exactly like the message
notifier's (settings-area creds, env fallback).

DELIVERY IS INERT WITHOUT SendGrid — expected. When notifications are off or no
capable sender is available, a schedule still RENDERS (proving the pipeline) and
its run is logged ``rendered`` instead of ``sent``; nothing is emailed. Flipping
to live is a credential change, not a build (mirrors the rest of the stack).

Split of responsibilities:
  * ORCHESTRATION — :func:`run_due_schedules` (the cron seam) and
    :func:`run_schedule` (one schedule). Callers own nothing; this commits its
    own run-log + cursor advances per schedule so one bad schedule can't lose
    another's progress.
  * The DUE math + period windows are pure (``reports_depth``); rendering is
    pure-ish (``report_exports``, DB-read only).

IDEMPOTENT: a schedule advances ``last_period_key`` only after a successful
render (delivered or inert). Re-running the cron the same period is a no-op.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.loan import Vendor
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.loan import PlatformLoan
from app.models.platform.report_schedule import (
    PlatformReportDefinition,
    PlatformReportSchedule,
    PlatformReportScheduleRun,
)
from app.services import report_exports
from app.services.metrics import reports_depth as RD

logger = get_logger(__name__)


@dataclass
class RunSummary:
    schedules_considered: int = 0
    schedules_run: int = 0
    reports_rendered: int = 0
    emails_sent: int = 0
    statuses: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Rendering (pure-ish — DB reads only, no writes).
# ---------------------------------------------------------------------------


def _render_for(
    db: Session,
    schedule: PlatformReportSchedule,
    *,
    vendor_ids: Optional[list[UUID]],
    date_from: date,
    date_to: date,
) -> report_exports.ReportResult:
    """Render this schedule's report for a scope + window (stock or custom)."""
    if schedule.definition_id is not None:
        definition = (
            db.query(PlatformReportDefinition)
            .filter(PlatformReportDefinition.id == schedule.definition_id)
            .first()
        )
        if definition is None:
            raise ValueError(f"definition {schedule.definition_id} missing")
        return report_exports.generate_custom_report(
            db,
            name=definition.name,
            base_dataset=definition.base_dataset,
            columns=list(definition.columns or []),
            vendor_ids=vendor_ids,
            date_from=date_from,
            date_to=date_to,
        )
    return report_exports.generate_report(
        db,
        schedule.report_key,
        vendor_ids=vendor_ids,
        date_from=date_from,
        date_to=date_to,
    )


def _resolve_vendor_recipients(db: Session, vendor_id: UUID) -> list[str]:
    """Clinic staff emails for a vendor, falling back to the vendor contact —
    the same resolution the message notifier uses."""
    from app.services.clinic_membership import resolve_clinic_user_emails

    emails = resolve_clinic_user_emails(db, vendor_id)
    if emails:
        return emails
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if vendor is not None and vendor.email:
        return [vendor.email]
    return []


def _vendors_on_book(db: Session, scope: Optional[list[UUID]]) -> list[UUID]:
    """Distinct vendor ids that have at least one loan on the book (optionally
    limited to a static scope) — the fan-out set for a per-vendor schedule."""
    q = (
        db.query(PlatformCreditApplication.vendor_id)
        .join(PlatformLoan, PlatformLoan.application_id == PlatformCreditApplication.id)
        .filter(PlatformCreditApplication.vendor_id.isnot(None))
    )
    if scope:
        q = q.filter(PlatformCreditApplication.vendor_id.in_(scope))
    return [row[0] for row in q.distinct().all()]


# ---------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------


def run_schedule(
    db: Session,
    schedule: PlatformReportSchedule,
    *,
    today: date,
    sender: Optional[object] = None,
) -> PlatformReportScheduleRun:
    """Render + (best-effort) deliver ONE due schedule, log the run, advance the
    cursor on success. Commits. Never raises — a failure is logged as a
    ``failed`` run and the cursor is NOT advanced (retried next cron)."""
    period_key, date_from, date_to = RD.previous_period(schedule.cadence, today)

    static_scope = [UUID(str(v)) for v in (schedule.params or {}).get("vendor_ids", [])]
    static_scope = static_scope or None

    status = "rendered"
    detail = None
    reports = 0
    recipients_notified = 0
    try:
        if schedule.per_vendor:
            targets = [
                (vid, [vid], _resolve_vendor_recipients(db, vid))
                for vid in _vendors_on_book(db, static_scope)
            ]
        else:
            targets = [(None, static_scope, list(schedule.recipients or []))]

        any_delivered = False
        any_recipients = False
        for _vendor_id, scope, recipients in targets:
            result = _render_for(
                db, schedule, vendor_ids=scope, date_from=date_from, date_to=date_to
            )
            reports += 1
            if not recipients:
                continue
            any_recipients = True
            sent = _deliver(
                db, schedule, result, recipients, period_key, sender=sender
            )
            recipients_notified += sent
            if sent:
                any_delivered = True

        if not any_recipients:
            status = "skipped"
            detail = "no recipients resolved"
        elif any_delivered:
            status = "sent"
        else:
            status = "rendered"
            detail = "rendered; delivery inert (no email creds / notifications off)"

        # Success (including inert): advance the idempotency cursor.
        schedule.last_period_key = period_key
    except Exception as exc:  # noqa: BLE001 — one schedule's failure is isolated
        db.rollback()
        status = "failed"
        detail = str(exc)[:500]
        reports = 0
        recipients_notified = 0
        logger.error(
            "report_schedule_failed",
            schedule_id=str(schedule.id),
            period=period_key,
            error=detail,
        )

    run = PlatformReportScheduleRun(
        schedule_id=schedule.id,
        period_key=period_key,
        status=status,
        detail=detail,
        report_count=reports,
        recipient_count=recipients_notified,
    )
    db.add(run)
    db.commit()
    return run


def _deliver(
    db: Session,
    schedule: PlatformReportSchedule,
    result: report_exports.ReportResult,
    recipients: list[str],
    period_key: str,
    *,
    sender: Optional[object],
) -> int:
    """Email the rendered XLSX to each recipient. Returns how many were sent.

    INERT when notifications are off and no sender is injected — returns 0
    without contacting any provider (the render already happened). A per-
    recipient send failure is logged and skipped, never fatal."""
    if sender is None and not settings.USE_REAL_NOTIFICATIONS:
        return 0
    active_sender = sender or _build_report_sender(db)
    if active_sender is None or not hasattr(active_sender, "send_report"):
        return 0

    subject = f"PaySpyre report: {result.title} ({period_key})"
    html = (
        f"<p>Your scheduled <strong>{result.title}</strong> report for "
        f"{period_key} is attached ({result.row_count} rows).</p>"
        "<p>This is an automated PaySpyre report delivery.</p>"
    )
    sent = 0
    for to_email in recipients:
        try:
            active_sender.send_report(
                to_email=to_email,
                subject=subject,
                html=html,
                attachment_filename=result.filename,
                attachment_bytes=result.content,
            )
            sent += 1
        except Exception as exc:  # noqa: BLE001 — one bad address ≠ skip the rest
            logger.warning(
                "report_schedule_delivery_failed",
                schedule_id=str(schedule.id),
                error=str(exc)[:200],
            )
    return sent


def _build_report_sender(db: Session):
    """Mirror the message notifier's sender build (settings-area creds, env
    fallback). Returns None if the provider isn't SendGrid (only SendGrid
    supports attachments today)."""
    if settings.EMAIL_PROVIDER != "sendgrid":
        return None
    from app.services.integration_creds import resolve
    from app.services.sendgrid_email import SendGridEmailSender

    c = resolve(
        db,
        "sendgrid",
        secret_keys=["api_key"],
        config_keys=["from_email"],
        env={"api_key": "SENDGRID_API_KEY", "from_email": "SENDGRID_FROM_EMAIL"},
    )
    return SendGridEmailSender(api_key=c["api_key"], from_email=c["from_email"])


def run_due_schedules(
    db: Session,
    *,
    today: Optional[date] = None,
    sender: Optional[object] = None,
    clock: Optional[Callable[[], date]] = None,
) -> RunSummary:
    """The cron seam: run every ACTIVE schedule that is due for its cadence.

    Due = the most recently completed period differs from ``last_period_key``
    (``reports_depth.is_due``). Each schedule is rendered + delivered + logged
    independently (its own commit), so a failure is contained.
    """
    today = today or (clock() if clock else date.today())
    summary = RunSummary()

    schedules = (
        db.query(PlatformReportSchedule)
        .filter(PlatformReportSchedule.active.is_(True))
        .all()
    )
    for schedule in schedules:
        summary.schedules_considered += 1
        if not RD.is_due(schedule.cadence, today, schedule.last_period_key):
            continue
        run = run_schedule(db, schedule, today=today, sender=sender)
        summary.schedules_run += 1
        summary.reports_rendered += run.report_count
        summary.emails_sent += run.recipient_count
        summary.statuses[run.status] = summary.statuses.get(run.status, 0) + 1

    logger.info(
        "report_schedules_run",
        considered=summary.schedules_considered,
        run=summary.schedules_run,
        rendered=summary.reports_rendered,
        sent=summary.emails_sent,
        statuses=summary.statuses,
    )
    return summary
