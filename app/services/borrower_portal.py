"""Borrower-portal pure helpers (WS-J) — kept side-effect-free for DB-free tests.

* Status-banner derivation (video 11: "message dialog box at the top that
  updates as their applications or accounts go through different stages").
* Initial-vs-current schedule shaping + closed-payments toggle + next-payment
  widget math.
* Payout-request date-window validation (≤30 days ahead, never in the past).
* New-loan (re-origination) prefill: which canonical application fields carry
  over from the borrower's latest verified application file.
* Display masking for bank-account identifiers.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Status banner
# ---------------------------------------------------------------------------

# Application-status → (stage, message). Loan status refines this once a loan
# exists (see banner_for). Copy mirrors the TL banner Dave called out as a
# "good thing", reworded for PaySpyre.
_APPLICATION_BANNERS: dict[str, tuple[str, str]] = {
    "started": (
        "in_progress",
        "Your application is in progress. Complete the remaining steps to submit it for review.",
    ),
    "origination": (
        "in_progress",
        "Your application is in progress. Complete the remaining steps to submit it for review.",
    ),
    "verifying": (
        "verifying",
        "We are verifying your information. You will be notified when verification completes.",
    ),
    "pre_qualified": (
        "under_review",
        "Your application has been submitted for approval. An account representative may "
        "contact you to clarify some information if necessary. Thank you for your patience.",
    ),
    "awaiting_hard_pull": (
        "under_review",
        "Your application has been submitted for approval. An account representative may "
        "contact you to clarify some information if necessary. Thank you for your patience.",
    ),
    "underwriting": (
        "under_review",
        "Your application has been submitted for approval. An account representative may "
        "contact you to clarify some information if necessary. Thank you for your patience.",
    ),
    "under_review": (
        "under_review",
        "Your application has been submitted for approval. An account representative may "
        "contact you to clarify some information if necessary. Thank you for your patience.",
    ),
    # --- Dave's Status Flow v1.00 (migration 068) --------------------------
    "credit_report": (
        "verifying",
        "We are verifying your information. You will be notified when verification completes.",
    ),
    "bank_verification": (
        "verifying",
        "We are verifying your information. You will be notified when verification completes.",
    ),
    "application_verification": (
        "verifying",
        "We are verifying your information. You will be notified when verification completes.",
    ),
    "offer_acceptance": (
        "approved",
        "Your application has been approved. Please review and accept your offer to continue.",
    ),
    "agreement_signature": (
        "approved",
        "Your application has been approved. Please review and sign your loan agreement to "
        "complete the process.",
    ),
    "approved": (
        "approved",
        "Your application has been approved. We are preparing your loan agreement and funding.",
    ),
    "active": (
        "active",
        "Thank you for using our services! Please make timely payments as per your loan "
        "agreement and get in touch in case you have any questions.",
    ),
    "repaid": ("paid_off", "Congratulations — this loan is paid in full. Thank you for "
                           "choosing PaySpyre."),
    "renewed": ("closed", "This loan has been closed and renewed."),
    "refinanced": ("closed", "This loan has been closed and refinanced."),
    "transferred": ("closed", "This loan has been transferred."),
    "settlement": ("closed", "This account is closed. Please contact us if you have any "
                             "questions."),
    "written_off": ("closed", "This account is closed. Please contact us if you have any "
                              "questions."),
    "declined": (
        "declined",
        "We were unable to approve your application at this time. Please contact us if you "
        "have any questions about this decision.",
    ),
    "withdrawn": ("closed", "This application has been withdrawn."),
    "expired": ("closed", "This application has expired."),
}

_LOAN_BANNERS: dict[str, tuple[str, str]] = {
    "pending_disbursement": (
        "funding",
        "Your loan has been approved and funding is on its way. You will be notified when "
        "your funds are disbursed.",
    ),
    "active": (
        "active",
        "Thank you for using our services! Please make timely payments as per your loan "
        "agreement and get in touch in case you have any questions.",
    ),
    "delinquent": (
        "past_due",
        "Your account has a past-due payment. Please make a payment or get in touch with "
        "us as soon as possible.",
    ),
    "paid_off": (
        "paid_off",
        "Congratulations — this loan is paid in full. Thank you for choosing PaySpyre.",
    ),
    "charged_off": (
        "closed",
        "This account is closed. Please contact us if you have any questions.",
    ),
    "cancelled": ("closed", "This loan was cancelled."),
}

_DEFAULT_BANNER = (
    "welcome",
    "Welcome to your PaySpyre borrower portal.",
)


def banner_for(application_status: Optional[str], loan_status: Optional[str]) -> dict:
    """Stage-driven banner for the portal header.

    A loan's lifecycle message wins over its application's (once funded, the
    application status is history); with no loan, the application drives it;
    with neither, a generic welcome.
    """
    if loan_status is not None and loan_status in _LOAN_BANNERS:
        stage, message = _LOAN_BANNERS[loan_status]
    elif application_status is not None and application_status in _APPLICATION_BANNERS:
        stage, message = _APPLICATION_BANNERS[application_status]
    else:
        stage, message = _DEFAULT_BANNER
    return {"stage": stage, "message": message}


# ---------------------------------------------------------------------------
# Schedule views (initial vs current) + next-payment widget
# ---------------------------------------------------------------------------

# A schedule row is CLOSED once nothing further will be collected against it.
CLOSED_STATUSES = ("paid", "waived")


def shape_schedule_rows(
    schedule_items: Iterable,
    view: str,
    include_closed: bool,
) -> list[dict]:
    """Shape ``PlatformLoanScheduleItem``-like rows for the portal schedule tab.

    * ``initial`` — the as-agreed amortization plan: planned amounts only, no
      payment overlay (every row presented as ``scheduled``; nothing filtered —
      the initial plan doesn't change because payments happened).
    * ``current`` — the actuals-adjusted live view: per-row status, paid and
      remaining amounts; ``include_closed=False`` hides paid/waived rows
      (TL's "Show closed payments" toggle).
    """
    rows = sorted(schedule_items, key=lambda s: s.installment_number)
    out: list[dict] = []
    for r in rows:
        if view == "initial":
            out.append(
                {
                    "installment_number": r.installment_number,
                    "due_date": r.due_date.isoformat(),
                    "principal_cents": r.principal_cents,
                    "interest_cents": r.interest_cents,
                    "total_cents": r.total_cents,
                    "status": "scheduled",
                    "paid_cents": 0,
                    "remaining_cents": r.total_cents,
                }
            )
            continue
        if not include_closed and r.status in CLOSED_STATUSES:
            continue
        out.append(
            {
                "installment_number": r.installment_number,
                "due_date": r.due_date.isoformat(),
                "principal_cents": r.principal_cents,
                "interest_cents": r.interest_cents,
                "total_cents": r.total_cents,
                "status": r.status,
                "paid_cents": r.paid_cents,
                "remaining_cents": max(0, r.total_cents - r.paid_cents),
            }
        )
    return out


def next_payment_widget(schedule_items: Iterable, as_of: date) -> Optional[dict]:
    """Next-payment widget data (date, amount, countdown) or None when done.

    Next payment = earliest open installment by installment_number (mirrors the
    loans endpoint's ``_next_due_item`` definition — suspended rows are staff-
    parked and skipped).
    """
    open_rows = [
        s
        for s in schedule_items
        if s.status not in CLOSED_STATUSES and s.status != "suspended"
    ]
    if not open_rows:
        return None
    nxt = min(open_rows, key=lambda s: s.installment_number)
    days_until = (nxt.due_date - as_of).days
    return {
        "due_date": nxt.due_date.isoformat(),
        "amount_cents": max(0, nxt.total_cents - nxt.paid_cents),
        "days_until": max(0, days_until),
        "overdue": days_until < 0,
        "installment_number": nxt.installment_number,
    }


# ---------------------------------------------------------------------------
# Payout requests
# ---------------------------------------------------------------------------

PAYOUT_MAX_DAYS_AHEAD = 30

# Dave's explicit rule — surfaced verbatim on every payout-request response.
PAYOUT_DISCLAIMER = (
    "A payout inquiry does not suspend your scheduled payments. Payments are only "
    "suspended once the account is actually paid out."
)


def validate_payout_date(requested: date, today: date) -> Optional[str]:
    """None when valid; else the human-readable refusal reason.

    Policy window: today ≤ requested ≤ today + 30 days.
    """
    if requested < today:
        return "The payout date cannot be in the past."
    if requested > today + timedelta(days=PAYOUT_MAX_DAYS_AHEAD):
        return f"A payout can only be calculated up to {PAYOUT_MAX_DAYS_AHEAD} days in advance."
    return None


# ---------------------------------------------------------------------------
# New-loan (re-origination) prefill
# ---------------------------------------------------------------------------

# Canonical application columns copied from the borrower's most recent
# application file into a re-origination seed (their verified profile). SIN is
# deliberately absent (lives encrypted on the patient, never copied around);
# id_number is carried so staff can cross-check, matching the canonical set.
PREFILL_FIELDS = (
    # personal
    "first_name",
    "middle_name",
    "last_name",
    "date_of_birth",
    "marital_status",
    "number_of_dependents",
    "citizenship",
    "education",
    "main_phone",
    "alternative_phone",
    "email",
    # id
    "id_type",
    "id_number",
    "id_province_of_issue",
    "id_expiry",
    # residence
    "residence_street",
    "residence_unit",
    "residence_city",
    "residence_province",
    "residence_postal_code",
    "time_at_address_years",
    "time_at_address_months",
    "residential_status",
    "monthly_housing_payment_cents",
    # income
    "income_type",
    "net_monthly_income_cents",
    "next_pay_date",
    "pay_frequency",
    "employer_name",
    "hire_date",
    "job_title",
    "work_phone",
    "work_phone_ext",
    "ok_to_contact_at_work",
    # financial
    "number_of_credit_accounts",
    "car_ownership",
    "monthly_car_payment_cents",
    "non_discretionary_expenses_cents",
)


def prefill_from_application(source_app) -> dict:
    """Extract the carry-over field values from a prior application object.

    Pure attribute reads — works on any object exposing the canonical columns;
    None values are skipped (nothing to carry)."""
    out: dict = {}
    for field in PREFILL_FIELDS:
        value = getattr(source_app, field, None)
        if value is not None:
            out[field] = value
    return out


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


def mask_identifier(value: Optional[str], keep: int = 4) -> Optional[str]:
    """Mask all but the last ``keep`` characters for display ("••••1000")."""
    if not value:
        return None
    tail = value[-keep:] if len(value) > keep else value
    return "•" * 4 + tail
