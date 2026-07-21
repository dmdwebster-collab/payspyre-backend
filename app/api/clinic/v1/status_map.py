"""Collapse the 9-value platform application status enum into the 4 buckets the
clinic console understands (clinicMock.ts ``ClinicApplicationStatus``).

Platform enum (platform_application_status):
    started, verifying, pre_qualified, awaiting_hard_pull, under_review,
    approved, declined, withdrawn, expired

Clinic buckets:
    started | approved | declined | manual_review

Mapping rationale:
    - approved / declined map 1:1 (terminal decisions the practice cares about).
    - under_review -> manual_review (a human is looking at it).
    - everything still in-flight (started, verifying, pre_qualified,
      awaiting_hard_pull) -> started (the patient hasn't finished yet).
    - withdrawn / expired -> declined (dead from the practice's POV — no
      financing will happen). This is a pragmatic default; if the console later
      grows a dedicated "closed" bucket, split these out.
"""
from __future__ import annotations

_CLINIC_STATUS: dict[str, str] = {
    "started": "started",
    "origination": "started",       # migration 043 — being filled out
    "verifying": "started",
    "pre_qualified": "started",
    "awaiting_hard_pull": "started",
    # Dave's Status Flow v1.00 (migration 068). The three parallel verification
    # gates are still "the file is in flight" from the practice's POV; the two
    # post-underwriting steps are a human/applicant action away from a decision,
    # so they read as manual_review rather than a premature "approved".
    "credit_report": "started",
    "bank_verification": "started",
    "application_verification": "started",
    "underwriting": "manual_review",  # migration 043 — adjudication in progress
    "under_review": "manual_review",
    "offer_acceptance": "manual_review",
    "agreement_signature": "manual_review",
    "approved": "approved",
    # An activated (and later closed) loan was approved — the practice's question
    # is "did this patient get financing?", and the answer stays yes.
    "active": "approved",
    "repaid": "approved",
    "renewed": "approved",
    "refinanced": "approved",
    "transferred": "approved",
    "settlement": "approved",
    "written_off": "approved",
    "declined": "declined",
    "withdrawn": "declined",
    "expired": "declined",
}


def to_clinic_status(platform_status: str) -> str:
    """Map a platform application status to a clinic-console bucket.

    Unknown values fall back to ``started`` (treat as in-flight) so an enum
    addition never 500s the dashboard.
    """
    return _CLINIC_STATUS.get(platform_status, "started")


def to_vendor_visible_status(platform_status: str, decision_by: str | None = None) -> str:
    """The status a VENDOR is allowed to see (WS-I, Dave's silent-escalation rule).

    10__Vendor_Access.md: "if an automated response is an automated rejection
    then instead of being hard rejected it would get submitted to a human
    underwriter for review" — the vendor must NOT be told an AUTO decline is
    final while that human escalation is pending. An application that is
    ``declined`` with ``decision_by == 'auto'`` therefore surfaces as
    ``manual_review`` ("in review"). Once a human confirms or overturns the
    decline (the admin decision path stamps ``decision_by`` with the human
    actor), the real bucket shows.

    Every clinic/vendor response that carries an application status MUST go
    through this function, never ``to_clinic_status`` directly.
    """
    if platform_status == "declined" and decision_by == "auto":
        return "manual_review"
    return to_clinic_status(platform_status)
