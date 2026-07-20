"""Notification processor (WS2) — turns ``platform_events`` into sent notifications.

This is the outbox lane of the notification architecture: a cursor-based poller
(run from a cron, like the delinquency job) that scans new ``platform_events``,
maps each to zero-or-more notifications, and sends them through the same
``send_notification`` path WS1 built. One path serves both transactional events
(``decision_made``) and the synthetic dunning events WS3 emits
(``payment_due_reminder`` / ``payment_overdue``).

Safety model:

* **Dedup is the source of truth, not the cursor.** Before sending, the
  processor checks for an existing ``notification_sent`` *or*
  ``notification_skipped`` event for the same (source_event_id, channel). The
  cursor only bounds the scan; a reset cursor causes a one-time rescan, never a
  double-send.
* **Commit-after-send.** The vendor call already happened by the time
  ``send_notification`` returns, so its audit row is committed immediately —
  before the next send — so a crash mid-batch can never resend an email.
* **Transient vs permanent.** A transient vendor failure holds the cursor just
  below that event so it retries next run (later events are reprocessed but
  deduped). A permanent failure (no email, suppressed, email-only type on the
  SMS channel) writes a ``notification_skipped`` audit row and does NOT retry.

Declines are deliberately NOT handled here: the adverse-action notice is already
sent synchronously + idempotently by ``adverse_action.send_adverse_action_notice``
from ``FlowOrchestrator._decide``. Routing declines through this processor too
would double-send a legally-required notice.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.platform.notification_cursor import PlatformNotificationCursor
from app.services import flags as flags_service
from app.services.real_notification_dispatcher import (
    PermanentNotificationError,
    TransientNotificationError,
    _redact_pii,
)

logger = get_logger(__name__)

DEFAULT_CURSOR_NAME = "default"
SKIPPED_EVENT_TYPE = "notification_skipped"
# In-app (dashboard) channel: a notification_sent-shaped event with channel
# 'dashboard' and the rendered content in the payload. The borrower portal reads
# these later (see app/services/notification_config + the dashboard read path).
DASHBOARD_EVENT_TYPE = "dashboard_notification"
DASHBOARD_CHANNEL = "dashboard"

# Event types the processor reacts to. decision_made and application_cancelled
# (WS-E staff Cancel — a non-credit closure, so NOT an adverse-action notice)
# are transactional; the two payment_* types are the synthetic events WS3's
# dunning job emits; pad_pre_notification is the synthetic event the WS-G
# auto-collection job emits N business days before each scheduled PAD pull.
TRIGGER_EVENT_TYPES = (
    "decision_made",
    "application_cancelled",
    "payment_due_reminder",
    "payment_overdue",
    # WS-J: hardship amendment sent for e-signature — borrower must be told
    # (email; the payload carries channels+context, passthrough shape).
    "hardship_agreement_sent",
    "pad_pre_notification",
    # Loan lifecycle (Dave's Customer notification spec, 2026-07). Each maps to
    # a registry notification_type in _plan_loan_lifecycle below.
    "loan_disbursed",
    "loan_charged_off",
    "loan_payment_recorded",
    "loan_agreement_sent",
    "loan_agreement_signed",
)

# event_type → customer-facing notification_type for simple 1:1 lifecycle
# notifications. loan_payment_recorded is handled specially (may fan out to
# payment_received AND loan_repaid when the payment closes the loan).
_LIFECYCLE_NOTIFICATION_MAP = {
    "loan_disbursed": "loan_activated",
    "loan_charged_off": "loan_written_off",
    "loan_agreement_sent": "offer_accepted_signing",
    "loan_agreement_signed": "agreement_signed",
}


@dataclass(frozen=True)
class NotificationPlan:
    """One concrete notification to send for a source event."""
    notification_type: str
    contact_method: str  # "email" | "sms"
    context: dict
    loan_id: Optional[str] = None


@dataclass
class ProcessResult:
    scanned: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0          # transient failures (will retry)
    deferred: int = 0        # outside the province send window (will retry)
    dashboard: int = 0       # in-app dashboard notifications recorded
    cursor_advanced_to: int = 0


def _fmt_cents(cents: Optional[int]) -> str:
    """Integer cents → ``$1,234.56`` (best-effort; 0 if missing)."""
    return "${:,.2f}".format((cents or 0) / 100)


def _fmt_date(d) -> str:
    """date/datetime → ``July 20, 2026`` (mirrors dunning's display format)."""
    if d is None:
        return "—"
    return d.strftime("%B %d, %Y")


class NotificationProcessor:
    def __init__(
        self,
        db: Session,
        dispatcher: Any = None,
        cursor_name: str = DEFAULT_CURSOR_NAME,
        now: Optional[datetime] = None,
    ):
        self.db = db
        self.cursor_name = cursor_name
        # Wall-clock used for the per-province send-window gate. Injectable for
        # deterministic tests; defaults to now (UTC) per run.
        self._now = now
        # WS-E flag-suppression memo: (patient_id, loan_id) -> bool, per run.
        self._flag_suppression_cache: dict[tuple, bool] = {}
        # Same selector the applicant API uses; injectable for tests.
        if dispatcher is not None:
            self.dispatcher = dispatcher
        elif settings.USE_REAL_NOTIFICATIONS:
            from app.services.real_notification_dispatcher import RealNotificationDispatcher
            self.dispatcher = RealNotificationDispatcher(db)
        else:
            from app.services.mock_notification_dispatcher import MockNotificationDispatcher
            self.dispatcher = MockNotificationDispatcher(db)

    # -- public -------------------------------------------------------------

    def run(self, limit: int = 500) -> ProcessResult:
        """Process up to ``limit`` new trigger events. Commits as it goes."""
        result = ProcessResult()
        cursor = self._load_cursor()
        start_after = cursor.last_event_id
        stmt = text(
            """
            SELECT id, event_type, application_id, patient_id, payload
            FROM platform_events
            WHERE id > :after AND event_type IN :types
            ORDER BY id ASC
            LIMIT :lim
            """
        ).bindparams(bindparam("types", expanding=True))
        events = (
            self.db.execute(
                stmt,
                {"after": start_after, "types": list(TRIGGER_EVENT_TYPES), "lim": limit},
            )
            .mappings()
            .all()
        )

        now = self._now or datetime.now(timezone.utc)
        min_unresolved: Optional[int] = None
        max_scanned = start_after
        for ev in events:
            result.scanned += 1
            max_scanned = ev["id"]
            for plan in self._plan(ev):
                if self._already_handled(ev["id"], plan.contact_method, plan.notification_type):
                    continue

                # Dashboard (in-app) channel: never a vendor call — record the
                # rendered notification as an event the borrower portal reads.
                if plan.contact_method == DASHBOARD_CHANNEL:
                    self._record_dashboard(ev, plan)
                    self.db.commit()
                    result.dashboard += 1
                    continue

                # WS-E customer/loan flags: an active suppress-notifications
                # flag on the borrower or the loan blocks VENDOR sends only —
                # the skip is still audited (suppress sends, still log). The
                # dashboard branch above is exempt: an in-app record is the
                # log, not an outbound send. Adverse-action + magic-link never
                # route through this processor and are never suppressed.
                if self._flag_suppressed(ev["patient_id"], plan.loan_id):
                    self._record_skipped(
                        ev, plan, reason=flags_service.SUPPRESSION_SKIP_REASON
                    )
                    self.db.commit()
                    result.skipped += 1
                    continue

                # Vendor channels (email/sms): gate on the borrower's per-province
                # communication window. Outside the window → DEFER: do not send
                # this run, hold the cursor below this event so it retries next
                # run (reusing the transient cursor-hold pattern). NOT a skip:
                # skips are permanent + audited; this is a transient time gate.
                if not self._within_send_window(ev, now):
                    result.deferred += 1
                    min_unresolved = (
                        ev["id"] if min_unresolved is None else min(min_unresolved, ev["id"])
                    )
                    logger.info(
                        "notification_deferred_send_window",
                        source_event_id=ev["id"],
                        channel=plan.contact_method,
                        notification_type=plan.notification_type,
                    )
                    continue

                try:
                    self.dispatcher.send_notification(
                        patient_id=ev["patient_id"],
                        notification_type=plan.notification_type,
                        contact_method=plan.contact_method,
                        context=plan.context,
                        application_id=ev["application_id"],
                        loan_id=plan.loan_id,
                        source_event_id=ev["id"],
                    )
                    self.db.commit()  # persist the audit row before the next send
                    result.sent += 1
                except PermanentNotificationError as exc:
                    self.db.rollback()
                    self._record_skipped(ev, plan, reason=str(exc))
                    self.db.commit()
                    result.skipped += 1
                except TransientNotificationError as exc:
                    self.db.rollback()
                    result.failed += 1
                    min_unresolved = ev["id"] if min_unresolved is None else min(min_unresolved, ev["id"])
                    logger.warning(
                        "notification_transient_failure",
                        source_event_id=ev["id"],
                        channel=plan.contact_method,
                        notification_type=plan.notification_type,
                        error=_redact_pii(str(exc)),
                    )

        new_last = (min_unresolved - 1) if min_unresolved is not None else max_scanned
        if new_last > cursor.last_event_id:
            self._save_cursor(cursor, new_last)
            self.db.commit()
        result.cursor_advanced_to = cursor.last_event_id
        logger.info(
            "notification_processor_run",
            scanned=result.scanned,
            sent=result.sent,
            skipped=result.skipped,
            failed=result.failed,
            deferred=result.deferred,
            dashboard=result.dashboard,
            cursor=result.cursor_advanced_to,
        )
        return result

    # -- mapping ------------------------------------------------------------

    def _plan(self, ev) -> list[NotificationPlan]:
        etype = ev["event_type"]
        if etype == "decision_made":
            return self._plan_decision(ev)
        if etype == "application_cancelled":
            return self._plan_cancelled(ev)
        if etype in (
            "payment_due_reminder",
            "payment_overdue",
            "hardship_agreement_sent",
            "pad_pre_notification",
        ):
            return self._plan_passthrough(ev)
        if etype in _LIFECYCLE_NOTIFICATION_MAP or etype == "loan_payment_recorded":
            return self._plan_loan_lifecycle(ev)
        return []

    def _plan_decision(self, ev) -> list[NotificationPlan]:
        payload = ev["payload"] or {}
        decision = (payload.get("after") or {}).get("decision")
        if decision == "approved":
            ctx = self._approval_context(ev)
            if ctx is None:
                return []
            loan_id = ctx.get("_loan_id")
            ctx.pop("_loan_id", None)
            return [NotificationPlan("application_approved", "email", ctx, loan_id)]
        # declined → owned by adverse_action (sent in _decide); never here.
        # manual_review (under_review) → deferred: the under-review template
        # needs a review-SLA business input + bespoke fields. Tracked follow-up.
        return []

    def _plan_cancelled(self, ev) -> list[NotificationPlan]:
        """WS-E staff Cancel — notify the applicant of the NON-CREDIT closure.

        The event payload carries the reason directory's borrower_facing_text as
        snapshotted at cancel time (never PII); the borrower's name is looked up
        at send time like the approval path does. Email-only."""
        from app.models.platform.patient import PlatformPatient

        payload = ev["payload"] or {}
        reason_text = payload.get("borrower_facing_text") or "Your application has been cancelled."
        patient = (
            self.db.query(PlatformPatient)
            .filter(PlatformPatient.id == ev["patient_id"])
            .first()
        )
        name = " ".join(
            p for p in (
                getattr(patient, "legal_first_name", None),
                getattr(patient, "legal_last_name", None),
            ) if p
        ).strip() or "there"
        context = {
            "borrower_name": name,
            "application_id": str(ev["application_id"]),
            "cancel_reason_text": reason_text,
        }
        return [NotificationPlan("application_cancelled", "email", context)]

    def _plan_passthrough(self, ev) -> list[NotificationPlan]:
        """Synthetic dunning events (WS3) carry their own render context +
        channels in the payload; the processor fans them out, honoring the
        current notification-rule config per channel.

        Channels were chosen by the dunning scan when the event was emitted, but
        config can change between emit and process — so re-check each channel
        against the live rule (enable flag + rule-disabled), and drop any channel
        that is no longer enabled. Falls back to the payload channels if no rule
        row exists (the config service returns the dataclass-derived defaults)."""
        from app.services.notification_config import get_rule

        payload = ev["payload"] or {}
        context = payload.get("context") or {}
        channels = payload.get("channels") or ["email"]
        loan_id = payload.get("loan_id")
        ntype = ev["event_type"]

        rule = get_rule(self.db, ntype)
        if not rule.enabled:
            return []
        allowed = set(rule.enabled_channels)
        plans = []
        for ch in channels:
            if ch not in allowed:
                continue  # channel turned off in config since emit — drop it.
            plans.append(NotificationPlan(ntype, ch, context, loan_id))
        return plans

    def _plan_loan_lifecycle(self, ev) -> list[NotificationPlan]:
        """Customer notifications for loan lifecycle events (Dave Customer v1.0).

        1:1 for disbursed/charged-off/agreement events; loan_payment_recorded
        fans out to payment_received plus loan_repaid when that payment closed
        the loan. Channels honor the live notification rule (email by default;
        SMS only when the spec ships an SMS body or an override supplies one)."""
        from app.services.notification_config import get_rule
        from app.services.notification_render import NOTIFICATION_TYPES

        payload = ev["payload"] or {}
        loan_id = payload.get("loan_id")
        if not loan_id:
            return []
        ctx = self._loan_context(ev, loan_id)
        if ctx is None:
            return []

        etype = ev["event_type"]
        if etype == "loan_payment_recorded":
            after = payload.get("after") or {}
            amount = after.get("amount_cents")
            pctx = {
                **ctx,
                "payment_amount": _fmt_cents(amount),
                "payment_date": _fmt_date(datetime.now(timezone.utc).date()),
                "next_payment_amount": ctx.get("next_payment_amount", "—"),
                "due_date": ctx.get("due_date", "—"),
            }
            ntypes = [("payment_received", pctx)]
            if after.get("loan_status") == "paid_off":
                ntypes.append(("loan_repaid", ctx))
        else:
            ntypes = [(_LIFECYCLE_NOTIFICATION_MAP[etype], ctx)]

        plans: list[NotificationPlan] = []
        for ntype, context in ntypes:
            rule = get_rule(self.db, ntype)
            if not rule.enabled:
                continue
            spec = NOTIFICATION_TYPES.get(ntype)
            for ch in rule.enabled_channels:
                if ch == "sms" and not (spec and spec.sms_template) and not rule.override_for("sms"):
                    continue
                plans.append(NotificationPlan(ntype, ch, context, loan_id))
        return plans

    def _loan_context(self, ev, loan_id) -> Optional[dict]:
        """Shared context for loan lifecycle notifications: borrower name,
        short loan id, vendor name, plus next-installment fields when the
        schedule has an upcoming row. Returns None if the loan is gone."""
        from app.models.loan import Vendor
        from app.models.platform.credit_application import PlatformCreditApplication
        from app.models.platform.loan import PlatformLoan, PlatformLoanScheduleItem
        from app.models.platform.patient import PlatformPatient

        loan = self.db.get(PlatformLoan, loan_id)
        if loan is None:
            logger.info("lifecycle_notification_no_loan", loan_id=str(loan_id))
            return None

        application = (
            self.db.get(PlatformCreditApplication, loan.application_id)
            if loan.application_id else None
        )
        patient = None
        if application is not None and getattr(application, "patient_id", None):
            patient = self.db.get(PlatformPatient, application.patient_id)
        elif ev["patient_id"]:
            patient = self.db.get(PlatformPatient, ev["patient_id"])
        name = " ".join(
            p for p in (
                getattr(patient, "legal_first_name", None),
                getattr(patient, "legal_last_name", None),
            ) if p
        ).strip() or "there"

        vendor_name = "your provider"
        if application is not None and getattr(application, "vendor_id", None):
            vendor = self.db.get(Vendor, application.vendor_id)
            if vendor is not None:
                vendor_name = vendor.dba_name or vendor.business_name

        ctx = {
            "full_name": name,
            "borrower_name": name,
            "loan_id": str(loan.id)[:8],
            "vendor_name": vendor_name,
            # loan_written_off extras (harmless for other types; explicit keys
            # beat StrictUndefined surprises for rule content-overrides).
            "outstanding_balance": _fmt_cents(getattr(loan, "principal_balance_cents", None)),
            "written_off_amount": _fmt_cents(getattr(loan, "principal_balance_cents", None)),
            "days_overdue": 0,
            "current_date": _fmt_date(datetime.now(timezone.utc).date()),
        }
        nxt = (
            self.db.query(PlatformLoanScheduleItem)
            .filter(
                PlatformLoanScheduleItem.loan_id == loan.id,
                PlatformLoanScheduleItem.status.in_(("scheduled", "partial", "late")),
            )
            .order_by(PlatformLoanScheduleItem.installment_number.asc())
            .first()
        )
        first = (
            self.db.query(PlatformLoanScheduleItem)
            .filter(PlatformLoanScheduleItem.loan_id == loan.id)
            .order_by(PlatformLoanScheduleItem.installment_number.asc())
            .first()
        )
        if first is not None:
            ctx["first_payment_amount"] = _fmt_cents(first.total_cents)
            ctx["first_payment_date"] = _fmt_date(first.due_date)
        else:
            ctx["first_payment_amount"] = "—"
            ctx["first_payment_date"] = "—"
        if nxt is not None:
            ctx["next_payment_amount"] = _fmt_cents(nxt.total_cents)
            ctx["due_date"] = _fmt_date(nxt.due_date)
        return ctx

    def _approval_context(self, ev) -> Optional[dict]:
        """Build the approval-email context from the booked loan + patient."""
        from app.models.platform.loan import PlatformLoan, PlatformLoanScheduleItem
        from app.models.platform.patient import PlatformPatient

        app_id = ev["application_id"]
        loan = (
            self.db.query(PlatformLoan)
            .filter(PlatformLoan.application_id == app_id)
            .first()
        )
        if loan is None:
            # Loan booking is defensive/idempotent in _decide and may lag; without
            # it we can't populate the approval email — skip (cursor holds via the
            # absence of a sent/skip marker only if we return a plan, so return []).
            logger.info("approval_email_no_loan_yet", application_id=str(app_id))
            return None

        first = (
            self.db.query(PlatformLoanScheduleItem)
            .filter(PlatformLoanScheduleItem.loan_id == loan.id)
            .order_by(PlatformLoanScheduleItem.installment_number.asc())
            .first()
        )
        patient = (
            self.db.query(PlatformPatient).filter(PlatformPatient.id == ev["patient_id"]).first()
        )
        name = " ".join(
            p for p in (
                getattr(patient, "legal_first_name", None),
                getattr(patient, "legal_last_name", None),
            ) if p
        ).strip() or "there"
        vendor_name = "your provider"
        application = None
        if app_id:
            from app.models.platform.credit_application import PlatformCreditApplication

            application = self.db.get(PlatformCreditApplication, app_id)
        if application is not None and getattr(application, "vendor_id", None):
            from app.models.loan import Vendor

            vendor = self.db.get(Vendor, application.vendor_id)
            if vendor is not None:
                vendor_name = vendor.dba_name or vendor.business_name

        base = settings.BORROWER_PORTAL_BASE_URL.rstrip("/")
        return {
            "borrower_name": name,
            "full_name": name,
            "application_id": str(app_id),
            "loan_id": str(loan.id)[:8],
            "vendor_name": vendor_name,
            "amount": _fmt_cents(loan.principal_cents),
            "interest_rate": "{:.2f}%".format((loan.annual_rate_bps or 0) / 100),
            "term": f"{loan.term_months} months",
            "monthly_payment": _fmt_cents(first.total_cents if first else None),
            "agreement_url": f"{base}/loans/{loan.id}/agreement",
            "_loan_id": str(loan.id),
        }

    # -- WS-E flag suppression ----------------------------------------------

    def _flag_suppressed(self, patient_id, loan_id) -> bool:
        """Memoized per-run check of the customer/loan suppression flags."""
        key = (
            str(patient_id) if patient_id is not None else None,
            str(loan_id) if loan_id is not None else None,
        )
        if key not in self._flag_suppression_cache:
            self._flag_suppression_cache[key] = flags_service.notification_suppressed(
                self.db, patient_id=patient_id, loan_id=loan_id
            )
        return self._flag_suppression_cache[key]

    # -- dedup + skip audit -------------------------------------------------

    def _already_handled(
        self, source_event_id: int, channel: str, notification_type: str
    ) -> bool:
        """True if a notification was already sent, skipped, OR recorded as an
        in-app dashboard notification for this (source event, channel,
        notification type) — the double-send / double-record guard. The type is
        part of the key because one source event may legitimately fan out to
        several notifications (e.g. the final loan_payment_recorded →
        payment_received AND loan_repaid). A deferred send writes NO marker
        (that is what makes it retry next run)."""
        row = self.db.execute(
            text(
                """
                SELECT 1 FROM platform_events
                WHERE event_type IN ('notification_sent', :skipped, :dashboard)
                  AND payload->>'source_event_id' = :sid
                  AND payload->>'channel' = :ch
                  AND payload->>'notification_type' = :nt
                LIMIT 1
                """
            ),
            {
                "skipped": SKIPPED_EVENT_TYPE,
                "dashboard": DASHBOARD_EVENT_TYPE,
                "sid": str(source_event_id),
                "ch": channel,
                "nt": notification_type,
            },
        ).first()
        return row is not None

    def _record_skipped(self, ev, plan: NotificationPlan, *, reason: str) -> None:
        from app.models.platform.event import PlatformEvent

        payload = {
            "v": 1,
            "actor": {"type": "system", "id": "system"},
            "application_id": str(ev["application_id"]) if ev["application_id"] else None,
            "patient_id": str(ev["patient_id"]) if ev["patient_id"] else None,
            "channel": plan.contact_method,
            "notification_type": plan.notification_type,
            "source_event_id": ev["id"],
            "loan_id": plan.loan_id,
            "reason_redacted": _redact_pii(reason)[:500],
            "skipped_at": datetime.now(timezone.utc).isoformat(),
        }
        self.db.add(
            PlatformEvent(
                event_type=SKIPPED_EVENT_TYPE,
                actor="system",
                patient_id=ev["patient_id"],
                application_id=ev["application_id"],
                payload=payload,
            )
        )
        self.db.flush()

    # -- dashboard channel + send-window gate -------------------------------

    def _within_send_window(self, ev, now: datetime) -> bool:
        """True if a vendor (email/sms) send is allowed for this borrower right
        now per the per-province communication window. Province is read from
        platform_patient_fields; unknown province → permissive (True)."""
        from app.services.notification_config import (
            get_patient_province,
            is_within_send_window,
        )

        province = get_patient_province(self.db, ev["patient_id"])
        return is_within_send_window(province, now)

    def _record_dashboard(self, ev, plan: NotificationPlan) -> None:
        """Record an in-app (dashboard) notification as a ``dashboard_notification``
        event carrying the rendered subject + body. This is the WRITE side of the
        dashboard channel; the borrower portal reads these events later. Deduped
        upstream via :meth:`_already_handled` on (source_event_id, 'dashboard')."""
        from app.models.platform.event import PlatformEvent
        from app.services.notification_config import content_override
        from app.services.notification_render import render_dashboard

        override = content_override(self.db, plan.notification_type, DASHBOARD_CHANNEL)
        try:
            subject, body = render_dashboard(plan.notification_type, plan.context, override)
        except Exception as exc:  # pragma: no cover - defensive; bad template/context
            # Render failure is permanent (a code/config bug), not transient —
            # audit a skip so the cursor can advance and we don't retry forever.
            self._record_skipped(ev, plan, reason=f"dashboard_render_failed: {exc}")
            return
        payload = {
            "v": 1,
            "actor": {"type": "system", "id": "system"},
            "application_id": str(ev["application_id"]) if ev["application_id"] else None,
            "patient_id": str(ev["patient_id"]) if ev["patient_id"] else None,
            "channel": DASHBOARD_CHANNEL,
            "notification_type": plan.notification_type,
            "source_event_id": ev["id"],
            "loan_id": plan.loan_id,
            "subject": subject,
            "body": body,
            "read": False,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        self.db.add(
            PlatformEvent(
                event_type=DASHBOARD_EVENT_TYPE,
                actor="system",
                patient_id=ev["patient_id"],
                application_id=ev["application_id"],
                payload=payload,
            )
        )
        self.db.flush()

    # -- cursor -------------------------------------------------------------

    def _load_cursor(self) -> PlatformNotificationCursor:
        cursor = (
            self.db.query(PlatformNotificationCursor)
            .filter(PlatformNotificationCursor.name == self.cursor_name)
            .first()
        )
        if cursor is None:
            cursor = PlatformNotificationCursor(name=self.cursor_name, last_event_id=0)
            self.db.add(cursor)
            # Commit immediately so a later per-event rollback (transient failure)
            # can't discard the cursor row itself.
            self.db.commit()
        return cursor

    def _save_cursor(self, cursor: PlatformNotificationCursor, last_event_id: int) -> None:
        cursor.last_event_id = last_event_id
        self.db.add(cursor)
        self.db.flush()
