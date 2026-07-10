"""Config-driven intake policy checks (P0 schema pack, Dave's mandates).

Income-type policy: Dave removed "EI" ("Employment insurance is not a valid
source of income... I wouldn't even have that") and "Student" ("not a valid
income source so I would remove that") as acceptable income types for NEW
intake. Enforcement happens at REQUEST VALIDATION time only — storage stays
permissive (the ``platform_income_type`` DB enum keeps ``employment_insurance``
so legacy rows keep round-tripping).

Two flavours, both driven by ``Settings``:

* ``check_canonical_income_type`` — for the canonical enum-valued fields
  (finalize body, secondary incomes, employment history): the value must be in
  the config allowlist (``INTAKE_INCOME_TYPE_ALLOWLIST``).
* ``check_free_text_income_type`` — for the manual-application path, which
  accepts human labels ("Employed"): the normalized label must not be in the
  config blocklist (``INTAKE_INCOME_TYPE_BLOCKLIST``).

Pure functions returning an error string (or None) so they are unit-testable
without a DB and usable from both pydantic validators and endpoints.
"""
from __future__ import annotations

import re
from typing import Optional


def _csv(raw: str) -> frozenset[str]:
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def allowed_income_types() -> frozenset[str]:
    """The canonical income_type values acceptable on new intake (config)."""
    from app.core.config import settings

    return _csv(settings.INTAKE_INCOME_TYPE_ALLOWLIST)


def blocked_income_labels() -> frozenset[str]:
    """Normalized free-text income labels rejected on new intake (config)."""
    from app.core.config import settings

    return _csv(settings.INTAKE_INCOME_TYPE_BLOCKLIST)


def normalize_income_label(value: str) -> str:
    """Lowercase and collapse runs of non-alphanumerics to '_' for comparison:
    "Employment Insurance / Disability" -> "employment_insurance_disability"."""
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def check_canonical_income_type(value: Optional[str]) -> Optional[str]:
    """Validate a canonical (enum-valued) income_type for NEW intake.

    Returns an error message, or None when acceptable. None/empty input is
    acceptable (field optionality is decided by the calling schema).
    """
    if value is None or value == "":
        return None
    if value not in allowed_income_types():
        return (
            f"income_type '{value}' is not an accepted income type for new "
            f"applications (accepted: {', '.join(sorted(allowed_income_types()))})"
        )
    return None


def check_free_text_income_type(value: Optional[str]) -> Optional[str]:
    """Validate a free-text income label (manual-application path) for NEW intake.

    Rejects labels whose normalized form is (or starts with) a blocked label —
    e.g. "EI", "Student", "Employment Insurance", "employment insurance /
    disability" are all rejected. Returns an error message, or None.
    """
    if value is None or value == "":
        return None
    normalized = normalize_income_label(value)
    for blocked in blocked_income_labels():
        if normalized == blocked or normalized.startswith(blocked + "_"):
            return (
                f"income_type '{value}' is not an accepted income type for new "
                "applications"
            )
    return None
