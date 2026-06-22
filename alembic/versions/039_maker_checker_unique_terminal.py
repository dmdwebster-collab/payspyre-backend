"""Enforce one terminal (approved/rejected) event per maker-checker request

Revision ID: 039_maker_checker_unique_terminal
Revises: 038_notification_outbox
Create Date: 2026-06-22

The lender/admin maker-checker (charge-off / disbursement approvals) is recorded
on the append-only platform_events log: a request is an `admin_action_requested`
event, and a SECOND admin's `admin_action_approved` / `admin_action_rejected`
event (carrying `payload.request_event_id`) executes or kills it.

Without a DB constraint, two concurrent approvers could both pass the
application-level "already decided?" check and both write a terminal event —
double-executing the action (e.g. a double disbursement). The endpoint now also
takes a row lock (SELECT … FOR UPDATE on the request row), but this unique
partial index is the authoritative guard: AT MOST ONE terminal event may exist
per request_event_id, across approved + rejected, enforced by Postgres.

Additive, index-only. Safe on existing data (the feature is new — no terminal
events exist yet). Downgrade drops the index.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "039_maker_checker_unique_terminal"
down_revision: Union[str, None] = "038_notification_outbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "uq_admin_action_terminal_per_request"


def upgrade() -> None:
    # One terminal decision per request: a unique index on the request_event_id
    # carried in the JSONB payload, scoped to the two terminal event types.
    op.execute(
        f"""
        CREATE UNIQUE INDEX {_INDEX}
        ON platform_events ((payload ->> 'request_event_id'))
        WHERE event_type IN ('admin_action_approved', 'admin_action_rejected')
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
