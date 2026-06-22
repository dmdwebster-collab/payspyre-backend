"""Create platform_notification_outbox (durable notification retry queue, P7.4c)

Revision ID: 038_notification_outbox
Revises: 037_vendor_profile_change_review_cols
Create Date: 2026-06-22

Backs the async notification retry queue the NOTIFICATION_* settings always
described but never had storage for. A row is enqueued when a vendor send fails
transiently (gated by settings.NOTIFICATION_OUTBOX_ENABLED, default False) and
drained by scripts/process_notification_outbox.py honoring NOTIFICATION_MAX_RETRIES
+ the NOTIFICATION_RETRY_DELAYS backoff.

PII (Hard Rule #6): NO sensitive material is stored — no recipient and no token.
The worker re-resolves the recipient from patient_id and mints a fresh magic-link
token per retry. See the model docstring.

Additive only. The partial index on (status, next_attempt_at) is the worker's
hot path: "pending rows that are due". Downgrade drops the table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "038_notification_outbox"
down_revision: Union[str, None] = "037_vendor_profile_change_review_cols"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "platform_notification_outbox"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "patient_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_patients.id"),
            nullable=False,
        ),
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_credit_applications.id"),
            nullable=True,
        ),
        sa.Column(
            "notification_type",
            sa.Text(),
            nullable=False,
            server_default="magic_link",
        ),
        sa.Column("contact_method", sa.Text(), nullable=False),
        sa.Column("ttl_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error_class", sa.Text(), nullable=True),
        sa.Column("last_error_redacted", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Worker hot path: claim pending rows whose next_attempt_at is due.
    op.create_index(
        "ix_notification_outbox_due",
        _TABLE,
        ["status", "next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("ix_notification_outbox_due", table_name=_TABLE)
    op.drop_table(_TABLE)
