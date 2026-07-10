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
from typing import Iterable, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.platform.decision_reason import PlatformDecisionReason

logger = get_logger(__name__)

REASON_KINDS = ("reject", "cancel")

# Stable slug: lowercase snake_case, must start with a letter.
CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


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
