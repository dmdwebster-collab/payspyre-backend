"""Customer/loan flag queries (WS-E originations admin).

The one behavioral hook flags have today: a customer or loan carrying an
ACTIVE flag whose definition has ``suppress_notifications=True`` must not
receive vendor (email/SMS) notifications — the notification processor skips
the send but still writes its ``notification_skipped`` audit row
("suppress sends, still log").

Deliberately NOT consulted by:
  * the adverse-action notice path (legally required credit-decision notice);
  * magic-link auth sends (the borrower is actively trying to sign in);
  * in-app dashboard notifications (a record, not an outbound send — and
    itself the "still log" trail the borrower's file keeps).
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

#: Reason string recorded on the ``notification_skipped`` audit row.
SUPPRESSION_SKIP_REASON = "suppressed_by_flag: active notification-suppression flag on customer/loan"

# Static SQL (bandit B608: no interpolation, bound params only).
_SUPPRESSED_SQL = text(
    """
    SELECT 1
    FROM platform_flag_assignments a
    JOIN platform_flag_definitions d ON d.id = a.flag_id
    WHERE a.cleared_at IS NULL
      AND d.is_active
      AND d.suppress_notifications
      AND (
            (CAST(:patient_id AS uuid) IS NOT NULL AND a.patient_id = CAST(:patient_id AS uuid))
         OR (CAST(:loan_id AS uuid) IS NOT NULL AND a.loan_id = CAST(:loan_id AS uuid))
      )
    LIMIT 1
    """
)


def _as_uuid_str(value: Optional[object]) -> Optional[str]:
    """Coerce an id to its canonical UUID string, or None if it isn't a UUID.

    Flag subjects (``patient_id`` / ``loan_id``) are UUID columns, but callers
    like the dunning passthrough may carry a display/business identifier (e.g.
    ``"L-1"``). A non-UUID value can never match a UUID column, so it normalizes
    to None rather than reaching the ``CAST(... AS uuid)`` and raising DataError.
    """
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def notification_suppressed(
    db: Session,
    *,
    patient_id: Optional[object] = None,
    loan_id: Optional[object] = None,
) -> bool:
    """True when an active suppress-notifications flag covers this customer
    OR this loan. Either subject may be None; both None → False."""
    patient_uuid = _as_uuid_str(patient_id)
    loan_uuid = _as_uuid_str(loan_id)
    if patient_uuid is None and loan_uuid is None:
        return False
    row = db.execute(
        _SUPPRESSED_SQL,
        {"patient_id": patient_uuid, "loan_id": loan_uuid},
    ).first()
    return row is not None
