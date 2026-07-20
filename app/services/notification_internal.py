"""Internal (back-office / vendor-user) notification catalog.

Source: Dave's "PaySpyre Notifications_Back Office v1.0" and "_Vendor v1.0"
docs (docs/notifications_source/). All 17 internal notices share one shape —
an intro sentence plus a short key/value detail list — so they all render
through ``app/templates/emails/internal_notice.html``; this module holds the
per-type copy (subject / title / intro / which detail fields to show).

The registry in :mod:`app.services.notification_render` derives a
``NotificationSpec`` per key from this catalog, so these types appear in the
cockpit's notification-rules list like any customer type. Event WIRING for
most of these arrives with the staff-assignment feature; the catalog ships
first so the copy is versioned and the cockpit shows the full picture.

``build_internal_context`` turns a flat field dict into the template context
(labels in display order); ``detail_fields`` names are context keys per
docs/notifications_source/MERGEFIELD_MAP.md.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InternalNotice:
    """Copy + layout data for one internal notification type."""
    subject: str                    # Jinja string
    notice_title: str               # header line in internal_notice.html
    intro: str                      # lead sentence
    detail_fields: tuple[str, ...]  # context keys shown in the detail box, in order
    audience: str                   # "back_office" | "vendor"
    outro: str | None = None        # optional trailing paragraph


# Display labels for detail fields (fallback: title-cased key).
_FIELD_LABELS = {
    "loan_id": "Loan ID",
    "loan_status": "Loan status",
    "customer_name": "Customer name",
    "payment_amount": "Transaction amount",
    "due_date": "Next payment date",
    "outstanding_principal": "Outstanding principal",
    "outstanding_interest": "Outstanding interest",
    "ptp_amount": "Promised amount",
    "ptp_date": "Expected date of promised payment",
    "comment_text": "Comment",
    "comment_author": "Author",
    "contract_date": "Signed on",
    "past_due_amount": "Amount past due",
}


INTERNAL_NOTICES: dict[str, InternalNotice] = {
    "bo_loan_unassigned": InternalNotice(
        subject='New unassigned loan in the system',
        notice_title='New unassigned loan',
        intro="There is a loan that doesn't have a staff member assigned to it.",
        detail_fields=('loan_id', 'loan_status', 'customer_name'),
        audience='back_office',
    ),
    "bo_loan_assigned": InternalNotice(
        subject='Loan #{{ loan_id }} was assigned to you',
        notice_title='Loan assigned to you',
        intro='You were assigned a loan. To see if any particular actions need to be taken, open the loan application in one of the workplaces of your dashboard.',
        detail_fields=('loan_id', 'loan_status', 'customer_name'),
        audience='back_office',
    ),
    "bo_repayment_failed": InternalNotice(
        subject='Unsuccessful loan repayment. Loan ID: {{ loan_id }}',
        notice_title='Loan repayment failed',
        intro='Please be informed that the loan repayment has failed.',
        detail_fields=('loan_id', 'loan_status', 'customer_name', 'payment_amount'),
        audience='back_office',
        outro='Please investigate further if this loan is assigned to you.',
    ),
    "bo_deferment_requested": InternalNotice(
        subject='New payment deferment request on Loan ID: {{ loan_id }}',
        notice_title='Payment deferment request',
        intro='Please be informed that there is a loan payment deferment request created by the customer.',
        detail_fields=('loan_id', 'loan_status', 'customer_name', 'due_date', 'outstanding_principal', 'outstanding_interest'),
        audience='back_office',
    ),
    "bo_ptp_fulfilled": InternalNotice(
        subject='Promise to pay fulfilled. Loan ID: {{ loan_id }}',
        notice_title='Promise to pay fulfilled',
        intro='The promised payment has been received.',
        detail_fields=('loan_id', 'loan_status', 'customer_name', 'ptp_amount', 'ptp_date'),
        audience='back_office',
    ),
    "bo_ptp_broken": InternalNotice(
        subject='A broken promise to pay. Loan ID: {{ loan_id }}',
        notice_title='Promise to pay broken',
        intro='The promised payment has failed.',
        detail_fields=('loan_id', 'loan_status', 'customer_name', 'ptp_amount', 'ptp_date'),
        audience='back_office',
    ),
    "bo_comment_added": InternalNotice(
        subject='New comment on a loan. Loan ID: {{ loan_id }}',
        notice_title='New loan comment',
        intro='A new internal comment has been added to a loan.',
        detail_fields=('loan_id', 'customer_name', 'comment_author', 'comment_text'),
        audience='back_office',
    ),
    "bo_comment_edited": InternalNotice(
        subject='Edited comment on a loan. Loan ID: {{ loan_id }}',
        notice_title='Loan comment edited',
        intro='An internal comment on a loan has been edited by its author.',
        detail_fields=('loan_id', 'customer_name', 'comment_author', 'comment_text'),
        audience='back_office',
    ),
    "bo_agreement_declined": InternalNotice(
        subject='Loan agreement declined - Loan ID: {{ loan_id }}',
        notice_title='Loan agreement declined',
        intro='The customer has declined the loan agreement. The loan application is moved to the archive.',
        detail_fields=('loan_id', 'loan_status', 'customer_name'),
        audience='back_office',
    ),
    "bo_offer_declined": InternalNotice(
        subject='Loan offer declined - Loan ID: {{ loan_id }}',
        notice_title='Loan offer declined',
        intro='The customer has declined the loan offer. The loan application is moved to the archive.',
        detail_fields=('loan_id', 'loan_status', 'customer_name'),
        audience='back_office',
    ),
    "bo_loan_past_due": InternalNotice(
        subject='A loan is past due. Loan ID: {{ loan_id }}',
        notice_title='Loan past due',
        intro='There is a loan that has just become past due.',
        detail_fields=('loan_id', 'customer_name', 'due_date', 'past_due_amount'),
        audience='back_office',
    ),
    "bo_agreement_signed": InternalNotice(
        subject='Loan agreement signed. Loan ID: {{ loan_id }}',
        notice_title='Loan agreement signed',
        intro='Please be informed that a new loan agreement has been signed.',
        detail_fields=('loan_id', 'loan_status', 'customer_name', 'contract_date'),
        audience='back_office',
    ),
    "bo_agreement_expired": InternalNotice(
        subject='Loan agreement expired. Loan ID: {{ loan_id }}',
        notice_title='Loan agreement expired',
        intro='Please be informed that a loan agreement has expired. The loan application is sent to the archive.',
        detail_fields=('loan_id', 'loan_status', 'customer_name'),
        audience='back_office',
    ),
    "bo_offer_accepted": InternalNotice(
        subject='Loan offer accepted. Loan ID: {{ loan_id }}',
        notice_title='Loan offer accepted',
        intro='Please be informed that the customer has accepted the offer.',
        detail_fields=('loan_id', 'loan_status', 'customer_name'),
        audience='back_office',
    ),
    "bo_offer_expired": InternalNotice(
        subject='Loan offer expired. Loan ID: {{ loan_id }}',
        notice_title='Loan offer expired',
        intro='Please be informed that the loan offer has expired. The loan application is sent to the archive.',
        detail_fields=('loan_id', 'loan_status', 'customer_name'),
        audience='back_office',
    ),
    "vendor_loan_unassigned": InternalNotice(
        subject='Loan generated by vendor is unassigned. Loan ID: {{ loan_id }}',
        notice_title='Vendor loan unassigned',
        intro='Please be informed that there is a loan that needs to have a staff member assigned to it.',
        detail_fields=('loan_id', 'loan_status', 'customer_name'),
        audience='vendor',
    ),
    "vendor_loan_assigned": InternalNotice(
        subject='You were assigned a loan. Loan ID: {{ loan_id }}',
        notice_title='You were assigned a loan',
        intro='Please be informed that you have been assigned a loan.',
        detail_fields=('loan_id', 'loan_status', 'customer_name'),
        audience='vendor',
    ),}


def build_internal_context(key: str, fields: dict) -> dict:
    """Build the ``internal_notice.html`` context for one notice.

    ``fields`` must contain every key in the notice's ``detail_fields`` (the
    render is StrictUndefined — a missing field fails loudly in tests, not in
    production sends). Extra keys pass through untouched so subjects like
    ``Loan #{{ loan_id }}`` can render from the same dict.
    """
    notice = INTERNAL_NOTICES[key]
    details = [
        {"label": _FIELD_LABELS.get(f, f.replace("_", " ").capitalize()), "value": fields[f]}
        for f in notice.detail_fields
    ]
    ctx = {
        "notice_title": notice.notice_title,
        "intro": notice.intro,
        "details": details,
        **fields,
    }
    if notice.outro is not None:
        ctx["outro"] = notice.outro
    return ctx
