"""Real ``closed_at`` on platform_loans (Archive parity — video 06 gap 6)

Revision ID: 069_loan_closed_at
Revises: 068_dave_status_model
Create Date: 2026-07-22

``PlatformLoan`` has never carried a close timestamp. The Archive workplace has
been reporting ``updated_at`` as a proxy and flagging the dishonesty in the
payload (``closed_at_source``). ``updated_at`` is a *last touched* timestamp: a
bucket-snapshot pass, a bureau-reporting flag or a backfill script all move it,
so a loan repaid in March can sort as "closed yesterday". Dave's Archive sorts
and paginates on Close date, so the proxy had to go.

ADDS (both nullable — NULL means "not closed"):
    platform_loans.closed_at         timestamptz
    platform_loans.closed_at_source  text

``closed_at_source`` is the honesty flag, persisted per row:

    'transition'            stamped by the code path that closed the loan
                            (loan_servicing payoff, loan_lifecycle charge-off).
                            Exact.
    'backfill_last_payment' backfilled here: the loan was already ``paid_off``,
                            so the ledger's LAST payment/adjustment transaction
                            is what closed it. Accurate to the payment date.
    'backfill_updated_at'   backfilled here from ``updated_at`` — the old proxy,
                            kept ONLY where nothing better exists (charged-off
                            and cancelled loans, and paid-off loans with no
                            ledger rows, e.g. the Turnkey migration import).
                            Approximate; the flag says so.

BACKFILL RATIONALE
------------------
For ``paid_off`` loans there IS a better signal than ``updated_at``: closure is
caused by a payment (``loan_servicing.record_payment`` flips the status in the
same unit of work that appends the transaction), so the newest ledger row is the
closing event. We take ``MAX(created_at)`` of that loan's transactions — the row
insert time, i.e. when the closure actually happened — rather than
``effective_date``, which is a back-datable business date and would let a
back-dated payment claim a close date before the loan existed.

For ``charged_off`` / ``cancelled`` loans there is no equivalent causal row
(charge-off writes a ``PlatformEvent``, not a ledger transaction, and the event
table is polymorphic), so those keep ``updated_at`` — the same value the Archive
already showed, now frozen at backfill time instead of drifting on every future
write, and labelled ``backfill_updated_at``.

Live (non-terminal) loans get NULL/NULL: they are not closed.

Idempotent (``IF NOT EXISTS`` on the columns; the UPDATEs are guarded on
``closed_at IS NULL``). SQL is fully static (bandit B608).
"""
from typing import Sequence, Union

from alembic import op


revision: str = "069_loan_closed_at"
down_revision: Union[str, None] = "068_dave_status_model"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE platform_loans
            ADD COLUMN IF NOT EXISTS closed_at timestamptz,
            ADD COLUMN IF NOT EXISTS closed_at_source text
        """
    )

    # --- backfill 1: paid-off loans, from the closing ledger transaction -----
    op.execute(
        """
        UPDATE platform_loans AS l
           SET closed_at = t.last_txn_at,
               closed_at_source = 'backfill_last_payment'
          FROM (
                SELECT loan_id, MAX(created_at) AS last_txn_at
                  FROM platform_loan_transactions
                 GROUP BY loan_id
               ) AS t
         WHERE t.loan_id = l.id
           AND l.status = 'paid_off'
           AND l.closed_at IS NULL
        """
    )

    # --- backfill 2: everything else terminal, from the old updated_at proxy --
    op.execute(
        """
        UPDATE platform_loans
           SET closed_at = updated_at,
               closed_at_source = 'backfill_updated_at'
         WHERE status IN ('paid_off', 'charged_off', 'cancelled')
           AND closed_at IS NULL
        """
    )

    # Archive queue sorts and pages on close date.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_platform_loans_closed_at
            ON platform_loans (closed_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_platform_loans_closed_at")
    op.execute(
        """
        ALTER TABLE platform_loans
            DROP COLUMN IF EXISTS closed_at,
            DROP COLUMN IF EXISTS closed_at_source
        """
    )
