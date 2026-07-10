"""Decision-reason directory service (WS-E underwriting ops).

Pure validation helpers + the DB lookups the decision endpoints use. The
directory itself is ``platform_decision_reasons`` (migration 048): admin-editable
REJECT and CANCEL reasons, soft-deactivated only.

Compliance intent (Dave, 02__WP_Underwriting.md §5): rejection reasons must be
"standardized ... compliant with all regulations ... not discriminatory and
defensible" — so staff decisions can only carry codes from the vetted directory,
and the directory's ``borrower_facing_text`` is the single source of the wording
the applicant reads (adverse-action notice for rejects, cancellation notice for
cancels).
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional
from uuid import uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.platform.decision_reason import PlatformDecisionReason

logger = get_logger(__name__)

REASON_KINDS = ("reject", "cancel")

# Stable slug: lowercase snake_case, must start with a letter.
CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


# ---------------------------------------------------------------------------
# Default directory contents — the SINGLE source of truth for the seeds.
#
# Reject codes intentionally MATCH the engine's stable decision_reasons codes
# where one exists, so the directory's borrower_facing_text can override the
# adverse-action notice wording for both automated and staff declines. Wording
# mirrors adverse_action._REASON_TEXT so seeding changes no applicant-facing
# text until an admin edits the directory.
#
# Consumed by migration 048 AND the test-DB fixture (tests/conftest.py) via
# :func:`seed_defaults` — the conftest TRUNCATE wipes migration-inserted rows
# before every test, so both paths must share one idempotent seeder.
#
# FLAGGED FOR DAVE: borrower_facing_text wordings require his (and counsel's)
# review before launch — editable in the directory afterwards.
# ---------------------------------------------------------------------------

REJECT_REASON_SEEDS: list[tuple[str, str, str]] = [
    # (code, internal_label, borrower_facing_text)
    (
        "bureau_below_minimum",
        "Credit score below minimum requirement",
        "Your credit score did not meet our minimum requirement.",
    ),
    (
        "insufficient_income",
        "Income below minimum requirement",
        "Your income did not meet our minimum requirement for the amount requested.",
    ),
    (
        "excessive_debt_ratio",
        "Debt-to-income ratio too high",
        "Your existing debt obligations are too high relative to your income.",
    ),
    (
        "active_bankruptcy",
        "Active or undischarged bankruptcy",
        "Our records indicate an active or recent bankruptcy.",
    ),
    (
        "bankruptcy_discharge_recent",
        "Bankruptcy discharged too recently",
        "Our records indicate a bankruptcy that was discharged too recently to "
        "meet our lending criteria.",
    ),
    (
        "fraud_signal_review",
        "Information could not be verified (fraud signal)",
        "We were unable to verify the information provided.",
    ),
    (
        "identity_manual_review",
        "Identity could not be fully verified",
        "We were unable to fully verify your identity.",
    ),
    (
        "many_active_loans",
        "Too many active credit obligations",
        "You have too many active credit obligations at this time.",
    ),
    (
        "non_resident",
        "Not a resident of Canada",
        "Applicants must be residents of Canada.",
    ),
    (
        "quebec_coming_soon",
        "Province not yet serviced (Quebec)",
        "Service is not yet available in your province.",
    ),
]

CANCEL_REASON_SEEDS: list[tuple[str, str, str]] = [
    (
        "customer_request",
        "Customer requested cancellation",
        "Your application was cancelled at your request.",
    ),
    (
        "duplicate_application",
        "Duplicate application",
        "Your application was cancelled because it duplicates another "
        "application on file.",
    ),
    (
        "vendor_request",
        "Vendor requested cancellation",
        "Your application was cancelled at the request of your treatment provider.",
    ),
    (
        "offer_expired",
        "Offer expired",
        "Your application was cancelled because the associated offer expired.",
    ),
    (
        "bank_verification_expired",
        "Bank verification expired",
        "Your application was cancelled because your bank verification expired "
        "before it could be completed.",
    ),
    (
        "other",
        "Other (see comment)",
        "Your application has been cancelled.",
    ),
]

_SEED_INSERT = text(
    """
    INSERT INTO platform_decision_reasons
        (id, kind, code, internal_label, borrower_facing_text, active, sort_order,
         created_at, updated_at)
    VALUES
        (:id, CAST(:kind AS platform_decision_reason_kind), :code, :label, :text,
         TRUE, :sort, now(), now())
    ON CONFLICT (kind, code) DO NOTHING
    """
)


def seed_defaults(conn: Any) -> int:
    """Idempotently insert the default reject/cancel reasons.

    ``conn`` may be a SQLAlchemy Session or Connection (both expose
    ``execute``). Existing (kind, code) rows are left untouched — admin edits
    and soft-deactivations are never overwritten (ON CONFLICT DO NOTHING on the
    unique (kind, code) index). Returns the number of rows inserted; the caller
    owns the transaction/commit. Called by migration 048 and by the test-DB
    fixture (which truncates the table before every test).
    """
    inserted = 0
    for kind, seeds in (("reject", REJECT_REASON_SEEDS), ("cancel", CANCEL_REASON_SEEDS)):
        for sort, (code, label, text_) in enumerate(seeds):
            result = conn.execute(
                _SEED_INSERT,
                {
                    "id": str(uuid4()), "kind": kind, "code": code,
                    "label": label, "text": text_, "sort": sort,
                },
            )
            inserted += result.rowcount or 0
    return inserted


def validate_reason_fields(
    *,
    kind: str,
    code: str,
    internal_label: str,
    borrower_facing_text: str,
) -> None:
    """Pure field validation for a directory row. Raises ValueError on the first
    violation (the endpoint maps it to a 422)."""
    if kind not in REASON_KINDS:
        raise ValueError(f"kind must be one of {REASON_KINDS}, got {kind!r}")
    if not CODE_RE.match(code or ""):
        raise ValueError(
            "code must be a stable slug: lowercase letters/digits/underscores, "
            "starting with a letter (max 64 chars)"
        )
    if not (internal_label or "").strip():
        raise ValueError("internal_label must not be empty")
    if not (borrower_facing_text or "").strip():
        raise ValueError("borrower_facing_text must not be empty")


def get_active_reason(db: Session, kind: str, code: str) -> Optional[PlatformDecisionReason]:
    """The active directory row for (kind, code), or None."""
    return (
        db.query(PlatformDecisionReason)
        .filter(
            PlatformDecisionReason.kind == kind,
            PlatformDecisionReason.code == code,
            PlatformDecisionReason.active.is_(True),
        )
        .first()
    )


def invalid_reject_codes(db: Session, codes: Iterable[str]) -> list[str]:
    """The subset of ``codes`` that are NOT active reject-directory codes.

    Used by the staff decline flow: every principal reason on a staff decline
    must come from the vetted directory (empty return = all valid).
    """
    codes = [c for c in codes if c]
    if not codes:
        return []
    rows = db.execute(
        text(
            "SELECT code FROM platform_decision_reasons "
            "WHERE kind = 'reject' AND active = TRUE AND code IN :codes"
        ).bindparams(bindparam("codes", expanding=True)),
        {"codes": codes},
    ).fetchall()
    known = {r[0] for r in rows}
    return [c for c in codes if c not in known]


def reject_reason_texts(db: Session, codes: Iterable[str]) -> dict[str, str]:
    """{code: borrower_facing_text} for the given reject codes.

    Feeds the adverse-action notice so the directory's vetted wording overrides
    the static fallback map. DEFENSIVE: the notice path must never break because
    the directory is unreadable (e.g. an environment that has not applied
    migration 048) — on any error, return {} and let the static fallback wording
    apply.
    """
    codes = [c for c in codes or [] if c]
    if not codes:
        return {}
    try:
        rows = db.execute(
            text(
                "SELECT code, borrower_facing_text FROM platform_decision_reasons "
                "WHERE kind = 'reject' AND active = TRUE AND code IN :codes"
            ).bindparams(bindparam("codes", expanding=True)),
            {"codes": codes},
        ).fetchall()
        return {code: text_ for code, text_ in rows if (text_ or "").strip()}
    except Exception as exc:  # noqa: BLE001 — notice wording falls back, never breaks
        logger.warning("decision_reason_lookup_failed", error=str(exc))
        return {}
