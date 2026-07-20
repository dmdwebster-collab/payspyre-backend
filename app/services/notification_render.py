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

from app.services.notification_internal import INTERNAL_NOTICES

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


def _global_context() -> dict:
    """Company-level fields available to EVERY template render.

    Mirrors Turnkey's "System > Company Settings" merge fields (CompanyName,
    SupportEmail, ...) from Dave's Mergefields doc — callers never need to pass
    these, and any explicit context key with the same name wins. Computed per
    call (not module-level) so tests can monkeypatch settings.
    """
    from app.core.config import settings

    base = settings.BORROWER_PORTAL_BASE_URL.rstrip("/")
    return {
        "company_name": "PaySpyre Financial Inc.",
        "support_email": getattr(settings, "SUPPORT_EMAIL", "support@payspyre.com"),
        "company_phone": getattr(settings, "COMPANY_PHONE", ""),
        "website_url": "https://www.payspyre.com",
        "dashboard_url": f"{base}/account",
        "account_url": f"{base}/account",
        "terms_url": "https://www.payspyre.com/terms-and-conditions/",
        "privacy_url": "https://www.payspyre.com/privacy-policy/",
        "nominate_url": "https://www.payspyre.com/nominate-your-provider/",
    }


# Registry. Add a row here (and a mapping in the WS2 processor) to wire a new
# notification. Keys are stable identifiers also written into the
# ``notification_sent`` event payload as ``notification_type``.
NOTIFICATION_TYPES: dict[str, NotificationSpec] = {
    # -- application decisions -------------------------------------------
    # Fires on decision_made=approved: the borrower has a pre-approved offer
    # to review and accept. Copy = Dave's "Offer confirmation" (Customer v1.0).
    "application_approved": NotificationSpec(
        email_template="offer_confirmation.html",
        email_subject='PaySpyre - Loan Pre-Approved - Acceptance Required',
        sms_template='PaySpyre: Great news {{ full_name }} - your loan application {{ loan_id }} is pre-approved! Sign in to review and accept your offer (expires in 10 days): {{ dashboard_url }}',
    ),
    # Declines go out as the adverse-action notice (compliance-critical delivery).
    "application_declined": NotificationSpec(
        email_template="adverse_action_notice.html",
        email_subject="Your PaySpyre application decision",
        sms_template=None,  # adverse-action content does not fit / belong in SMS
    ),
    # Registered for cockpit use; NOT wired to decisions (adverse_action owns
    # declines). Dave's softer "Rejected" copy, pending counsel alignment.
    "application_rejected": NotificationSpec(
        email_template="application_rejected_v2.html",
        email_subject='PaySpyre - Application Rejection',
        sms_template=None,
    ),
    "application_under_review": NotificationSpec(
        email_template="application_under_review.html",
        email_subject="Your PaySpyre application is under review",
        sms_template=(
            "PaySpyre: your application is under review. We'll be in touch — "
            "{{ account_url }}"
        ),
    ),
    # WS-E: non-credit cancellation (staff Cancel action). Deliberately NOT the
    # adverse-action template — cancellation is not a credit decision. Distinct
    # from "loan_cancelled" below (booked-loan cancellation, Dave Customer v1.0).
    "application_cancelled": NotificationSpec(
        email_template="application_cancelled.html",
        email_subject="Your PaySpyre application has been cancelled",
        sms_template=None,  # email-only; the notice carries reason wording
    ),
    # -- dunning (offsets & channels via platform_notification_rules) -----
    "payment_due_reminder": NotificationSpec(
        email_template="payment_due_reminder.html",
        email_subject='PaySpyre - Payment Reminder: {% if days_until_due <= 2 %}Autopay in less than 48 hours{% else %}Autopay in {{ days_until_due }} Days{% endif %}',
        sms_template='PaySpyre: your autopayment of {{ payment_amount }} is scheduled for {{ due_date }}. View your account: {{ payment_url }}',
    ),
    "payment_overdue": NotificationSpec(
        email_template="payment_overdue.html",
        email_subject='{% if days_overdue >= 90 %}PaySpyre - ACCOUNT IN DEFAULT: Loan Past Due {{ days_overdue }} Days!{% else %}PaySpyre - Action Required: Loan Past Due {{ days_overdue }} Days{% endif %}',
        sms_template='PaySpyre: your loan is {{ days_overdue }} day(s) past due. Amount due: {{ payment_amount }}. Make a payment: {{ payment_url }}',
    ),
    # -- customer lifecycle & servicing (Dave Customer v1.0) ---------------
    "welcome": NotificationSpec(
        email_template="welcome.html",
        email_subject='Welcome to PaySpyre',
        sms_template=None,
    ),
    "registration_completed": NotificationSpec(
        email_template="registration_completed.html",
        email_subject='PaySpyre - Registration Completed',
        sms_template=None,
    ),
    "login_link_request": NotificationSpec(
        email_template="login_link_request.html",
        email_subject='PaySpyre - Your Sign-In Link',
        sms_template=None,
    ),
    "offer_accepted_signing": NotificationSpec(
        email_template="offer_accepted_signing.html",
        email_subject='PaySpyre - Loan Offer Accepted - Signing Required',
        sms_template='PaySpyre: Offer accepted for application {{ loan_id }}. Check your email for the e-signature request to sign your loan agreement. Details: {{ dashboard_url }}',
    ),
    "offer_expired": NotificationSpec(
        email_template="offer_expired.html",
        email_subject='PaySpyre - {{ vendor_name }} Loan Offer Expired',
        sms_template=None,
    ),
    "loan_activated": NotificationSpec(
        email_template="loan_activated.html",
        email_subject='PaySpyre - Application Approved & Activated',
        sms_template='PaySpyre: Your loan {{ loan_id }} is now active. First payment of {{ first_payment_amount }} is due {{ first_payment_date }}. View your schedule: {{ dashboard_url }}',
    ),
    "loan_repaid": NotificationSpec(
        email_template="loan_repaid.html",
        email_subject='PaySpyre - Loan Paid in Full',
        sms_template=None,
    ),
    "loan_cancelled": NotificationSpec(
        email_template="loan_cancelled.html",
        email_subject='PaySpyre - Loan Application Cancelled',
        sms_template=None,
    ),
    "loan_written_off": NotificationSpec(
        email_template="loan_written_off.html",
        email_subject='PaySpyre - Loan Written Off',
        sms_template=None,
    ),
    "application_waiting_decision": NotificationSpec(
        email_template="application_waiting_decision.html",
        email_subject='PaySpyre - Loan Application Submitted',
        sms_template=None,
    ),
    "co_applicant_added": NotificationSpec(
        email_template="co_applicant_added.html",
        email_subject='Co-Applicant for Loan {{ loan_id }}',
        sms_template=None,
    ),
    "payment_received": NotificationSpec(
        email_template="payment_received.html",
        email_subject='PaySpyre - Payment Initiated',
        sms_template=None,
    ),
    "payment_nsf": NotificationSpec(
        email_template="payment_nsf.html",
        email_subject='PaySpyre - Dishonoured Payment - Action Required',
        sms_template='PaySpyre: a recent payment on loan {{ loan_id }} was returned by your bank ({{ past_due_amount }} past due). Please sign in to make a payment: {{ dashboard_url }}',
    ),
    "payment_error": NotificationSpec(
        email_template="payment_error.html",
        email_subject='PaySpyre - Payment Not Processed (Error)',
        sms_template=None,
    ),
    "ptp_recorded": NotificationSpec(
        email_template="ptp_recorded.html",
        email_subject='PaySpyre - Promise to Pay Recorded',
        sms_template=None,
    ),
    "ptp_broken": NotificationSpec(
        email_template="ptp_broken.html",
        email_subject='PaySpyre - Broken Promise to Pay',
        sms_template='PaySpyre: we have not received the {{ ptp_amount }} payment promised for {{ ptp_date }} on loan {{ loan_id }}. Please pay now to avoid late fees: {{ dashboard_url }}',
    ),
    "deferment_approved": NotificationSpec(
        email_template="deferment_approved.html",
        email_subject='PaySpyre - Payment Deferment Request Approved',
        sms_template=None,
    ),
    "deferment_rejected": NotificationSpec(
        email_template="deferment_rejected.html",
        email_subject='PaySpyre - Payment Deferment Declined',
        sms_template=None,
    ),
    "agreement_signed": NotificationSpec(
        email_template="agreement_signed.html",
        email_subject='PaySpyre - Loan Agreement Signed',
        sms_template=None,
    ),
    "agreement_signature_reminder": NotificationSpec(
        email_template="agreement_signature_reminder.html",
        email_subject='PaySpyre - Loan Agreement Signature Reminder',
        sms_template=None,
    ),
    "agreement_signature_expired": NotificationSpec(
        email_template="agreement_signature_expired.html",
        email_subject='PaySpyre - Loan Agreement Signature Period Expired',
        sms_template=None,
    ),
    "bank_verification_request": NotificationSpec(
        email_template="bank_verification_request.html",
        email_subject='PaySpyre - Bank Account Verification Required',
        sms_template='PaySpyre: your loan application {{ loan_id }} requires bank account verification to move forward. Please sign in to complete it: {{ dashboard_url }}',
    ),
    "bank_verification_reminder": NotificationSpec(
        email_template="bank_verification_reminder.html",
        email_subject='PaySpyre - Bank Account Verification Reminder',
        sms_template=None,
    ),
    "bank_verification_expired": NotificationSpec(
        email_template="bank_verification_expired.html",
        email_subject='PaySpyre - Bank Account Verification Expired',
        sms_template=None,
    ),
}

# Internal (back-office / vendor-user) notices all render through the generic
# internal_notice.html; copy lives in the notification_internal catalog.
NOTIFICATION_TYPES.update({
    key: NotificationSpec(
        email_template="internal_notice.html",
        email_subject=notice.subject,
        sms_template=None,
    )
    for key, notice in INTERNAL_NOTICES.items()
})


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
    merged = {**_global_context(), **context}
    template = _email_env.get_template(spec.email_template)
    html = template.render(**merged)
    subject = Template(spec.email_subject, undefined=StrictUndefined).render(**merged)
    return subject, html


def render_sms(notification_type: str, context: dict) -> str:
    """Return the rendered SMS body, or raise if the type is email-only."""
    spec = get_spec(notification_type)
    if not spec.sms_template:
        raise ValueError(f"notification_type '{notification_type}' has no SMS template")
    merged = {**_global_context(), **context}
    return Template(spec.sms_template, undefined=StrictUndefined).render(**merged)


def render_string(template_str: str, context: dict) -> str:
    """Render an inline Jinja string against ``context``.

    Used by the system-configurable notification subsystem to render per-channel
    content overrides (subject/body) supplied from
    ``platform_notification_rules``. StrictUndefined so a typo'd field fails
    loudly rather than shipping ``None`` to a borrower. Global company fields
    are merged in so overrides may reference e.g. ``{{ support_email }}``."""
    return Template(template_str, undefined=StrictUndefined).render(
        **{**_global_context(), **context}
    )


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
