"""Notification processor cursor table (WS2)

Revision ID: 039_notification_cursor
Revises: 038_notification_outbox
Create Date: 2026-06-22

Single-row-per-lane low-water mark for the notification processor. New event
types written by the processor / dunning job (e.g. ``notification_skipped``,
``payment_due_reminder``) need NO migration because ``platform_events.event_type``
is a free-text String column, not an enum.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "039_notification_cursor"
down_revision: Union[str, None] = "038_notification_outbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_notification_cursor",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("last_event_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("platform_notification_cursor")
