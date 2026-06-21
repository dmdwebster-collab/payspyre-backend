"""Payment idempotency index + platform_events containment index

Revision ID: 033_payment_idempotency_and_event_index
Revises: 032_loan_application_unique
Create Date: 2026-06-21

Pre-handoff verification findings:
- **record_payment money-safety**: a payment rail can deliver the same receipt
  twice (at-least-once webhooks). A partial UNIQUE index on
  (loan_id, external_ref) makes a concurrent duplicate fail fast instead of
  double-applying cash to principal (the service also SELECT-dedupes serially).
- **platform_events idempotency perf**: the orchestrator's verification-replay
  check runs ``payload @> :key`` on the append-only (unbounded) events table with
  NO supporting index → a sequential scan on the hot decisioning path. A partial
  GIN index on ``payload`` (scoped to the high-volume event type) makes the
  containment lookup index-backed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "033_payment_idempotency_and_event_index"
down_revision: Union[str, None] = "032_loan_application_unique"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_platform_loan_payments_loan_external_ref",
        "platform_loan_payments",
        ["loan_id", "external_ref"],
        unique=True,
        postgresql_where=sa.text("external_ref IS NOT NULL"),
    )
    op.execute(
        """
        CREATE INDEX idx_platform_events_verification_payload
        ON platform_events USING gin (payload)
        WHERE event_type = 'verification_completed'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_platform_events_verification_payload")
    op.drop_index(
        "uq_platform_loan_payments_loan_external_ref",
        table_name="platform_loan_payments",
    )
