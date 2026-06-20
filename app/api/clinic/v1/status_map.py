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
    "verifying": "started",
    "pre_qualified": "started",
    "awaiting_hard_pull": "started",
    "under_review": "manual_review",
    "approved": "approved",
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
