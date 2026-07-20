"""System-configurable notification rules (Turnkey-parity).

Replaces today's hardcoded notification cadence/channels (the ``DunningPolicy``
dataclass in :mod:`app.services.dunning`) with a DB-backed, per-notification-type
config the team can manage instead of the developer editing code.

One row per notification type (e.g. ``payment_due_reminder``,
``payment_overdue``). Each row holds:

* ``offsets`` — list of integer day offsets. For *reminder* types these are
  days-BEFORE the due date; for *overdue* types they are days-AFTER. The dunning
  scan reads these to decide which milestones to fire.
* per-channel enable flags — ``email_enabled`` / ``dashboard_enabled`` /
  ``sms_enabled``. The notification processor honours these per send.
* optional per-channel content overrides — ``content_overrides`` JSONB keyed by
  channel (``{"email": {"subject": "...", "body": "..."}, "sms": {"body": "..."}}``).
  When absent the static template registry in
  :mod:`app.services.notification_render` is used (the typed fallback).

``DunningPolicy`` stays as the typed fallback default — if no row exists for a
type, the dataclass defaults apply, so the system degrades safely.
"""
from sqlalchemy import Boolean, Column, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


class PlatformNotificationRule(Base):
    __tablename__ = "platform_notification_rules"

    # Notification type, matching the keys in NOTIFICATION_TYPES + the dunning
    # event types (e.g. 'payment_due_reminder', 'payment_overdue'). Unique PK.
    notification_type = Column(String, primary_key=True)

    # Day offsets. days-before for reminders, days-after for overdue. JSONB list
    # of ints, e.g. [7, 2] or [7, 14, 21, 30, 60, 90].
    offsets = Column(JSONB, nullable=False, default=list)

    # Per-channel enable flags. Email on by default; SMS + dashboard present but
    # DISABLED by default (David's current cadence is email-only).
    email_enabled = Column(Boolean, nullable=False, default=True)
    dashboard_enabled = Column(Boolean, nullable=False, default=False)
    sms_enabled = Column(Boolean, nullable=False, default=False)

    # Optional per-channel content overrides:
    #   {"email": {"subject": "...", "body": "..."}, "sms": {"body": "..."},
    #    "dashboard": {"subject": "...", "body": "..."}}
    # Absent / null keys fall back to the static template registry.
    content_overrides = Column(JSONB, nullable=False, default=dict)

    enabled = Column(Boolean, nullable=False, default=True)

    # WS-F notification matrix: per-audience × per-channel cells
    #   {"borrower": {"email": true, "sms": false, "dashboard": true}, ...}
    # Audiences: borrower/vendor/staff/admin. Missing cells fall back to the
    # legacy per-channel flags above (see app.services.notification_matrix);
    # dashboard is forced always-on by the accessor (Dave).
    audience_channels = Column(JSONB, nullable=False, default=dict)

    # WS-F: TL attachment picker — list of attachment keys to include on the
    # email channel (loan_agreement, terms_conditions, privacy_policy,
    # loan_statements, amortization_schedule).
    attachments = Column(JSONB, nullable=False, default=list)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"<PlatformNotificationRule(notification_type={self.notification_type!r}, "
            f"offsets={self.offsets}, email={self.email_enabled}, "
            f"dashboard={self.dashboard_enabled}, sms={self.sms_enabled})>"
        )
