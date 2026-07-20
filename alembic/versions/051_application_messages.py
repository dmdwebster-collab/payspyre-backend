"""Application message threads (vendor ⇄ PaySpyre back-and-forth)

Revision ID: 051_application_messages
Revises: 050_repayment_modes
Create Date: 2026-07-19

Creates the in-app messaging surface that replaces the ad-hoc Slack channel
clinics (MSO vendors) used to talk to PaySpyre about applications:

* ``platform_application_messages`` — one row per message posted on an
  application thread. ``sender_kind`` is 'vendor' (a clinic staffer) or 'admin'
  (a PaySpyre operator); stored as text (a closed 2-value set validated at the
  API layer) mirroring the String-status convention of
  ``platform_application_documents`` rather than a PG enum.
* ``platform_application_message_reads`` — per-(application, user) last-read
  watermark. Unread = messages from the OTHER side newer than the watermark.
  A read receipt updated in place (unique on application_id+user_id), not an
  event — keeps unread-count / badge queries cheap.

Borrower/applicant is deliberately NOT a participant (spec: internal ops only).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "051_application_messages"
down_revision: Union[str, None] = "050_repayment_modes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_application_messages",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "application_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_credit_applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sender_kind", sa.String(), nullable=False),  # 'vendor' | 'admin'
        sa.Column(
            "sender_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # Thread reads (list one application's messages, ordered) + the unread scan
    # (join applications, filter by created_at) both hit (application_id, created_at).
    op.create_index(
        "ix_platform_application_messages_application_id_created_at",
        "platform_application_messages",
        ["application_id", "created_at"],
    )

    op.create_table(
        "platform_application_message_reads",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "application_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_credit_applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "last_read_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "application_id", "user_id", name="uq_app_message_read_app_user"
        ),
    )


def downgrade() -> None:
    op.drop_table("platform_application_message_reads")
    op.drop_index(
        "ix_platform_application_messages_application_id_created_at",
        "platform_application_messages",
    )
    op.drop_table("platform_application_messages")
