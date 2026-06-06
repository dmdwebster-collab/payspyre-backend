"""Add processed_webhooks table for race-safe webhook idempotency

Revision ID: 027_processed_webhooks_idempotency
Revises: 026_application_product_config_snapshot
Create Date: 2026-06-06

Closes security-review finding #3 (review C1): webhook replay protection was a
check-then-insert race. ``SignatureVerifier._check_nonce`` did a SELECT against
``platform_events`` (event_type='webhook_received') and the anchor row was
inserted later — with no unique constraint, two concurrent deliveries of the same
signed webhook both passed the SELECT and both processed (→ double
``decision_made``).

This adds a dedicated ``processed_webhooks`` table whose PRIMARY KEY on
``vendor_event_id`` makes the claim atomic: ``INSERT ... ON CONFLICT DO NOTHING``
lets exactly one concurrent claim win; the rest conflict and are rejected as
replays. A NEW table (rather than a unique index on ``platform_events``) is used
deliberately so the migration cannot fail on any pre-existing duplicate rows
produced by the old racy behavior.

The claim is made inside the webhook's unit-of-work transaction, so it persists
iff processing commits — a webhook whose processing fails/rolls back can still be
retried (the claim rolls back with it), while a truly-processed nonce is
permanently rejected.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "027_processed_webhooks_idempotency"
down_revision: Union[str, None] = "026_application_product_config_snapshot"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "processed_webhooks",
        sa.Column("vendor_event_id", sa.Text(), primary_key=True),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )
    op.execute(
        """
        COMMENT ON TABLE processed_webhooks IS
        'Webhook idempotency claims (security finding #3). PRIMARY KEY on
        vendor_event_id serializes concurrent duplicate deliveries via
        INSERT ... ON CONFLICT DO NOTHING. The claim is the authoritative replay
        guard, replacing the prior racy SELECT against platform_events.';
        """
    )


def downgrade() -> None:
    op.drop_table("processed_webhooks")
