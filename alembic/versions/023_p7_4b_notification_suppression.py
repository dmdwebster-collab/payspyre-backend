"""Create notification_suppression table — P7.4b inbound notification webhooks.

Revision ID: 023_p7_4b_notification_suppression
Revises: 022_seed_dental_full_arch_v1
Create Date: 2026-05-31

Holds the per-recipient suppression list populated by the Twilio + Resend
status-update webhooks (hard bounces, spam complaints, carrier opt-outs).
Consulted at send time by ``RealNotificationDispatcher`` before any vendor
HTTP call, gated by ``settings.USE_SUPPRESSION_CHECK``.

The recipient is stored as ``recipient_redacted`` (the same SHA-256 hash the
dispatcher uses in the ``notification_sent`` event payload — Hard Rule #6,
no raw phone / email in queryable tables). Unique constraint on
``(contact_method, recipient_redacted)`` makes ``upsert`` idempotent across
duplicate vendor deliveries.

Pure schema. No data migration. Downgrade drops table + index.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "023_p7_4b_notification_suppression"
down_revision: Union[str, None] = "022_seed_dental_full_arch_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notification_suppression",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("contact_method", sa.Text, nullable=False),
        sa.Column("recipient_redacted", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("vendor", sa.Text, nullable=False),
        sa.Column("vendor_event_id", sa.Text, nullable=True),
        sa.Column(
            "suppressed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "contact_method", "recipient_redacted",
            name="uq_notification_suppression_recipient",
        ),
    )
    op.create_index(
        "ix_notification_suppression_recipient",
        "notification_suppression",
        ["recipient_redacted"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_suppression_recipient",
        table_name="notification_suppression",
    )
    op.drop_table("notification_suppression")
