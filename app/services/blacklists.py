"""Blacklist screening (WS-I) — Turnkey Tools → Blacklists parity.

Staff-maintained lists of suspicious values, checked at application decision
time. THE RULE (Dave): a match FLAGS the file — an auto-APPROVE is downgraded
to MANUAL REVIEW so a human looks at it. A match NEVER auto-rejects, and a
rejected file stays rejected (the screen only ever moves approvals toward
review, nothing else).

Design:

* ``normalize_value``     — pure canonicalization per category (match key).
* ``apply_screen``        — pure decision-override policy (fully DB-free
                            testable; the never-auto-reject rule is pinned).
* ``screen_application``  — collects the application's screenable values and
                            checks them against active entries (ORM query,
                            static SQL).
* CRUD helpers            — add / deactivate (soft delete only), audited via
                            ``platform_events``.

Match-event payloads carry the blacklist entry id + category + a MASKED value
(never the full matched PII).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.platform.blacklist import PlatformBlacklistEntry
from app.models.platform.event import PlatformEvent

# Event types (audit trail).
ENTRY_ADDED_EVENT = "blacklist_entry_added"
ENTRY_DEACTIVATED_EVENT = "blacklist_entry_deactivated"
MATCH_FLAGGED_EVENT = "blacklist_match_flagged"

# Decision-reason code appended when a match downgrades an approval.
BLACKLIST_REVIEW_REASON = "blacklist_match_manual_review"

CATEGORIES = ("name", "sin", "phone", "email", "drivers_license", "account_number")

_DIGITS_ONLY = ("sin", "phone", "account_number")


class BlacklistError(ValueError):
    """A rule violation (unknown category / empty value) — maps to 4xx."""


def normalize_value(category: str, value: str) -> str:
    """Canonical match key for ``value`` under ``category``.

    * phone / sin / account_number → digits only (dashes, spaces, +1 noise
      stripped; a leading '1' country code on an 11-digit phone is dropped).
    * email  → lowercased, trimmed.
    * name / drivers_license → lowercased, whitespace collapsed, trimmed.
    """
    if category not in CATEGORIES:
        raise BlacklistError(f"unknown blacklist category {category!r}")
    raw = (value or "").strip()
    if not raw:
        raise BlacklistError("value must be non-empty")
    if category in _DIGITS_ONLY:
        digits = re.sub(r"\D", "", raw)
        if not digits:
            raise BlacklistError(f"{category} value must contain digits")
        if category == "phone" and len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        return digits
    if category == "email":
        return raw.lower()
    # name / drivers_license
    return re.sub(r"\s+", " ", raw).lower()


def mask_value(category: str, normalized: str) -> str:
    """Privacy-preserving rendering for event payloads / API rows: keep the
    first and last 2 chars (or the email domain), mask the middle."""
    if category == "email" and "@" in normalized:
        local, _, domain = normalized.partition("@")
        keep = local[:2]
        return f"{keep}{'*' * max(1, len(local) - len(keep))}@{domain}"
    if len(normalized) <= 4:
        return "*" * len(normalized)
    return f"{normalized[:2]}{'*' * (len(normalized) - 4)}{normalized[-2:]}"


@dataclass(frozen=True)
class BlacklistMatch:
    entry_id: str
    category: str
    masked_value: str
    reason: str


@dataclass(frozen=True)
class ScreenOutcome:
    """Result of applying the blacklist policy to a flow decision (pure)."""

    decision: str
    next_state: str
    decision_reasons: tuple
    flagged: bool
    downgraded: bool


def apply_screen(
    decision: str,
    next_state: str,
    decision_reasons: Sequence[str],
    matches: Sequence[BlacklistMatch],
) -> ScreenOutcome:
    """THE POLICY (pure, pinned by tests):

    * no matches            → decision unchanged.
    * match + APPROVED      → downgraded to manual review (``under_review``) —
                              a human must look before money moves.
    * match + anything else → flagged only; the decision is NEVER worsened.
      In particular a match never turns anything into a rejection (never
      auto-reject, per Dave), and a rejected file stays rejected.
    """
    reasons = tuple(decision_reasons)
    if not matches:
        return ScreenOutcome(decision, next_state, reasons, False, False)
    if decision == "approved":
        return ScreenOutcome(
            decision="manual_review",
            next_state="under_review",
            decision_reasons=reasons + (BLACKLIST_REVIEW_REASON,),
            flagged=True,
            downgraded=True,
        )
    return ScreenOutcome(decision, next_state, reasons, True, False)


def screenable_values(application, patient=None) -> dict[str, list[str]]:
    """The application's screenable raw values by category (pure collector).

    Sources: the canonical application columns (email, main/alternative phone,
    applicant name, driver's-license-style id number) plus the patient's
    account email. Bank account numbers are screened when present on the
    application (``account_number`` category is otherwise CRUD-only until bank
    details are captured at intake).
    """
    values: dict[str, list[str]] = {c: [] for c in CATEGORIES}

    def _add(category: str, raw) -> None:
        if raw and str(raw).strip():
            values[category].append(str(raw))

    _add("email", getattr(application, "email", None))
    if patient is not None:
        _add("email", getattr(patient, "email", None))
    _add("phone", getattr(application, "main_phone", None))
    _add("phone", getattr(application, "alternative_phone", None))
    first = getattr(application, "first_name", None)
    last = getattr(application, "last_name", None)
    if first and last:
        _add("name", f"{first} {last}")
    if (getattr(application, "id_type", None) or "").lower() in (
        "drivers_license",
        "driver_license",
        "driver's license",
        "dl",
    ):
        _add("drivers_license", getattr(application, "id_number", None))
    return {c: v for c, v in values.items() if v}


def check_values(db: Session, values_by_category: dict[str, list[str]]) -> list[BlacklistMatch]:
    """Check normalized values against ACTIVE entries. Static SQL (ORM filters)."""
    matches: list[BlacklistMatch] = []
    for category, raws in values_by_category.items():
        keys = set()
        for raw in raws:
            try:
                keys.add(normalize_value(category, raw))
            except BlacklistError:
                continue  # unscreenable junk never blocks intake
        if not keys:
            continue
        rows = (
            db.query(PlatformBlacklistEntry)
            .filter(
                PlatformBlacklistEntry.category == category,
                PlatformBlacklistEntry.value_normalized.in_(sorted(keys)),
                PlatformBlacklistEntry.active.is_(True),
            )
            .all()
        )
        for row in rows:
            matches.append(
                BlacklistMatch(
                    entry_id=str(row.id),
                    category=row.category,
                    masked_value=mask_value(row.category, row.value_normalized),
                    reason=row.reason,
                )
            )
    return matches


def screen_application(db: Session, application, patient=None) -> list[BlacklistMatch]:
    """Collect + check an application's screenable values (read-only)."""
    return check_values(db, screenable_values(application, patient))


def emit_match_event(
    db: Session, application, matches: Sequence[BlacklistMatch], *, downgraded: bool
) -> None:
    """Audit row for a screen hit — entry ids + categories + masked values only."""
    application_id = getattr(application, "id", None)
    db.add(
        PlatformEvent(
            event_type=MATCH_FLAGGED_EVENT,
            actor="system",
            application_id=application_id,
            payload={
                "v": 1,
                "actor": {"type": "system", "id": "blacklist_screen"},
                "application_id": str(application_id) if application_id else None,
                "downgraded_to_manual_review": downgraded,
                "matches": [
                    {
                        "entry_id": m.entry_id,
                        "category": m.category,
                        "masked_value": m.masked_value,
                        "reason": m.reason,
                    }
                    for m in matches
                ],
            },
        )
    )


# --- admin CRUD (soft delete only) ----------------------------------------


def add_entry(
    db: Session,
    *,
    category: str,
    value: str,
    reason: str,
    actor: str,
) -> PlatformBlacklistEntry:
    """Add an entry. Reason MANDATORY. Re-adding an active duplicate returns
    the existing row (idempotent). Emits ``blacklist_entry_added``."""
    cleaned_reason = (reason or "").strip()
    if not cleaned_reason:
        raise BlacklistError("a reason is required for every blacklist entry")
    normalized = normalize_value(category, value)
    existing = (
        db.query(PlatformBlacklistEntry)
        .filter(
            PlatformBlacklistEntry.category == category,
            PlatformBlacklistEntry.value_normalized == normalized,
            PlatformBlacklistEntry.active.is_(True),
        )
        .first()
    )
    if existing is not None:
        return existing
    row = PlatformBlacklistEntry(
        category=category,
        value=value.strip(),
        value_normalized=normalized,
        reason=cleaned_reason,
        active=True,
        created_by=actor,
    )
    db.add(row)
    db.flush()
    db.add(
        PlatformEvent(
            event_type=ENTRY_ADDED_EVENT,
            actor=actor,
            payload={
                "v": 1,
                "actor": {"type": "staff", "id": actor},
                "entry_id": str(row.id),
                "category": category,
                "masked_value": mask_value(category, normalized),
                "reason": cleaned_reason,
            },
        )
    )
    return row


def deactivate_entry(
    db: Session, entry_id: UUID, *, actor: str
) -> Optional[PlatformBlacklistEntry]:
    """Soft-delete an entry (``active`` → False; never a hard delete).
    Emits ``blacklist_entry_deactivated``. Returns None when not found."""
    row = (
        db.query(PlatformBlacklistEntry)
        .filter(PlatformBlacklistEntry.id == entry_id)
        .first()
    )
    if row is None:
        return None
    if row.active:
        row.active = False
        row.deactivated_by = actor
        row.deactivated_at = datetime.now(timezone.utc)
        db.add(
            PlatformEvent(
                event_type=ENTRY_DEACTIVATED_EVENT,
                actor=actor,
                payload={
                    "v": 1,
                    "actor": {"type": "staff", "id": actor},
                    "entry_id": str(row.id),
                    "category": row.category,
                    "masked_value": mask_value(row.category, row.value_normalized),
                },
            )
        )
    return row
