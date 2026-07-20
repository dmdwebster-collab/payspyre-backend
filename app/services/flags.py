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


def notification_suppressed(
    db: Session,
    *,
    patient_id: Optional[object] = None,
    loan_id: Optional[object] = None,
) -> bool:
    """True when an active suppress-notifications flag covers this customer
    OR this loan. Either subject may be None; both None → False."""
    if patient_id is None and loan_id is None:
        return False
    row = db.execute(
        _SUPPRESSED_SQL,
        {
            "patient_id": str(patient_id) if patient_id is not None else None,
            "loan_id": str(loan_id) if loan_id is not None else None,
        },
    ).first()
    return row is not None
