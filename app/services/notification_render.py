"""Notification rendering (WS1) — turns a notification_type + context into a
ready-to-send subject/body for email or SMS.

Flat module (not a ``notifications/`` package) for the same reason the mock /
real dispatchers are flat: avoid shadowing sibling service modules and keep the
cold-import path cheap.

Two things live here, deliberately together so there is ONE place that answers
"what does notification X look like":

1. ``NOTIFICATION_TYPES`` — the registry mapping a logical notification type
   (e.g. ``"payment_due_reminder"``) to its email template file + subject and
   its inline SMS template. The WS2 processor maps ``platform_events`` →
   notification_type; this maps notification_type → rendered message.
2. ``render_email`` / ``render_sms`` — pure functions that render a spec with a
   context dict. No I/O beyond reading the (cached) Jinja templates from
   ``app/templates/emails/``.

The HTML templates already exist under ``app/templates/emails/`` (drafted before
any send path was wired). Subjects were never captured in those files, so they
live here as short Jinja strings rendered against the same context.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, Template, select_autoescape

# app/services/notification_render.py -> parents[1] == app/
_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates" / "emails"

# HTML email environment. StrictUndefined so a missing context key fails loudly
# at render time (in a test) rather than silently shipping "None" to a borrower.
_email_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
    undefined=StrictUndefined,
)


@dataclass(frozen=True)
class NotificationSpec:
    """How one notification type renders on each channel."""
    email_template: str          # filename under app/templates/emails/
    email_subject: str           # Jinja string, rendered against the context
    sms_template: str | None     # inline Jinja string, or None if email-only


# Registry. Add a row here (and a mapping in the WS2 processor) to wire a new
# notification. Keys are stable identifiers also written into the
# ``notification_sent`` event payload as ``notification_type``.
NOTIFICATION_TYPES: dict[str, NotificationSpec] = {
    "application_approved": NotificationSpec(
        email_template="application_approved.html",
        email_subject="Your PaySpyre application is approved",
        sms_template=(
            "PaySpyre: good news, {{ borrower_name }} — your application is "
            "approved. Next steps: {{ account_url }}"
        ),
    ),
    # Declines go out as the adverse-action notice (compliance-critical delivery).
    "application_declined": NotificationSpec(
        email_template="adverse_action_notice.html",
        email_subject="Your PaySpyre application decision",
        sms_template=None,  # adverse-action content does not fit / belong in SMS
    ),
    "application_under_review": NotificationSpec(
        email_template="application_under_review.html",
        email_subject="Your PaySpyre application is under review",
        sms_template=(
            "PaySpyre: your application is under review. We'll be in touch — "
            "{{ account_url }}"
        ),
    ),
    "payment_due_reminder": NotificationSpec(
        email_template="payment_due_reminder.html",
        email_subject="Payment reminder — {{ payment_amount }} due {{ due_date }}",
        sms_template=(
            "PaySpyre: payment of {{ payment_amount }} is due {{ due_date }}. "
            "View your account: {{ payment_url }}"
        ),
    ),
    "payment_overdue": NotificationSpec(
        email_template="payment_overdue.html",
        email_subject="Your PaySpyre payment is overdue",
        sms_template=(
            "PaySpyre: your payment of {{ payment_amount }} is {{ days_overdue }} day(s) "
            "overdue. Please make your payment: {{ payment_url }}"
        ),
    ),
}


class UnknownNotificationType(KeyError):
    """Raised when a notification_type has no registry entry."""


def get_spec(notification_type: str) -> NotificationSpec:
    try:
        return NOTIFICATION_TYPES[notification_type]
    except KeyError as exc:  # pragma: no cover - trivial
        raise UnknownNotificationType(notification_type) from exc


def render_email(notification_type: str, context: dict) -> tuple[str, str]:
    """Return ``(subject, html_body)`` for an email notification."""
    spec = get_spec(notification_type)
    template = _email_env.get_template(spec.email_template)
    html = template.render(**context)
    subject = Template(spec.email_subject, undefined=StrictUndefined).render(**context)
    return subject, html


def render_sms(notification_type: str, context: dict) -> str:
    """Return the rendered SMS body, or raise if the type is email-only."""
    spec = get_spec(notification_type)
    if not spec.sms_template:
        raise ValueError(f"notification_type '{notification_type}' has no SMS template")
    return Template(spec.sms_template, undefined=StrictUndefined).render(**context)


def render_string(template_str: str, context: dict) -> str:
    """Render an inline Jinja string against ``context``.

    Used by the system-configurable notification subsystem to render per-channel
    content overrides (subject/body) supplied from
    ``platform_notification_rules``. StrictUndefined so a typo'd field fails
    loudly rather than shipping ``None`` to a borrower."""
    return Template(template_str, undefined=StrictUndefined).render(**context)


def render_dashboard(notification_type: str, context: dict, override: dict | None = None) -> tuple[str, str]:
    """Render the in-app (dashboard) channel content as ``(subject, body)``.

    The dashboard channel has no static template file of its own; it derives its
    content from a per-channel content override when configured, otherwise it
    reuses the email subject + the SMS body (a compact, plain summary that suits
    an in-app card). ``override`` is the optional
    ``{"subject": .., "body": ..}`` dict from the notification rule.
    """
    spec = get_spec(notification_type)
    if override:
        subj_tpl = override.get("subject") or spec.email_subject
        body_tpl = override.get("body") or spec.sms_template or subj_tpl
        return render_string(subj_tpl, context), render_string(body_tpl, context)
    subject = render_string(spec.email_subject, context)
    body_tpl = spec.sms_template or spec.email_subject
    return subject, render_string(body_tpl, context)
