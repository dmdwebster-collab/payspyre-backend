"""Communications hub — append-only per-account communications log (WS-A)

Revision ID: 055_communications_log
Revises: 054_hardship
Create Date: 2026-07-20

Dave's mandate #4 (videos 01/02, GAP_ANALYSIS "Communications hub" +
"Full comms log w/ message bodies" rows): "Communications log = legal
evidence. Every email/SMS/dashboard notification stored with the exact
message body, per account, forever — used in collections and court."

One table: ``platform_communications_log`` — one row per communication a
borrower actually received (or an offline contact staff had with them):

* channel ``email`` / ``sms`` — vendor sends, recorded by the notification
  dispatchers at send time with the FULL rendered subject + body (bodies
  carrying credentials, e.g. magic-link codes, are stored as
  ``<Hidden for privacy purposes>`` — the on-camera Turnkey behavior).
* channel ``dashboard`` — in-app notifications recorded by the processor's
  dashboard lane with the rendered card content.
* channel ``offline`` — staff-logged phone calls / in-person contacts
  (mandatory comment), either direction.

Append-only at the DATABASE level: the same trigger pattern as
``platform_events`` (migration 021) blocks every UPDATE and DELETE — there is
deliberately no application code path that mutates a row, and a raw SQL
statement can't either. Related FKs are plain (no ON DELETE action), so a
parent hard-delete is refused rather than silently orphaning/erasing legal
evidence.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "055_communications_log"
down_revision: Union[str, None] = "054_hardship"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FUNCTION = "prevent_platform_communications_log_modification"
_TRIGGER = "platform_communications_log_append_only"


def upgrade() -> None:
    op.create_table(
        "platform_communications_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # 'email' | 'sms' | 'dashboard' | 'offline' (closed set, validated at
        # the API/service layer — String-status convention of this codebase).
        sa.Column("channel", sa.String(), nullable=False),
        # 'outbound' | 'inbound' (inbound only ever for offline contacts —
        # e.g. the borrower called us asking for a payout figure).
        sa.Column("direction", sa.String(), nullable=False, server_default="outbound"),
        # Raw recipient (email address / E.164 phone). NULL for dashboard and
        # offline rows. This table is the legal-evidence record — unlike
        # platform_events (which stores only a recipient hash) the actual
        # recipient is part of the evidence.
        sa.Column("recipient", sa.String(), nullable=True),
        sa.Column("subject", sa.String(), nullable=True),
        # FULL rendered message body (HTML for email, plain text for SMS /
        # dashboard, staff narrative for offline). Never truncated.
        sa.Column("body", sa.Text(), nullable=False),
        # Template / registry reference (notification_render.NOTIFICATION_TYPES
        # key) for system + templated-staff sends; NULL for offline contacts.
        sa.Column("notification_type", sa.String(), nullable=True),
        # Related account anchors — plain FKs (no ON DELETE): evidence rows
        # must never be cascade-deleted or nulled out.
        sa.Column(
            "patient_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_patients.id"),
            nullable=True,
        ),
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_credit_applications.id"),
            nullable=True,
        ),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loans.id"),
            nullable=True,
        ),
        # 'system' for automated sends, else the acting staff user's email/id.
        sa.Column("sent_by", sa.String(), nullable=False, server_default="system"),
        sa.Column(
            "sent_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        # Internal staff note ("why this was sent"); MANDATORY for offline
        # contacts (enforced at the API layer).
        sa.Column("comment", sa.Text(), nullable=True),
        # Offline-contact fields (TL "Log message" dialog: Date / Method /
        # Purpose / Result / Comment).
        sa.Column("contact_method", sa.String(), nullable=True),
        sa.Column("purpose", sa.String(), nullable=True),
        sa.Column("result", sa.String(), nullable=True),
        # Send/record status: 'sent' | 'queued' | 'recorded'.
        sa.Column("status", sa.String(), nullable=False, server_default="recorded"),
        sa.Column("vendor", sa.String(), nullable=True),
        sa.Column("vendor_message_id", sa.String(), nullable=True),
        # The platform_events row (notification_sent / dashboard_notification /
        # magic_link_issued) this communication corresponds to, when any.
        sa.Column("source_event_id", sa.BigInteger(), nullable=True),
        # When the communication happened (staff-supplied for offline logs;
        # send time otherwise).
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_platform_communications_log_patient",
        "platform_communications_log",
        ["patient_id", sa.text("occurred_at DESC")],
    )
    op.create_index(
        "ix_platform_communications_log_application",
        "platform_communications_log",
        ["application_id", sa.text("occurred_at DESC")],
    )
    op.create_index(
        "ix_platform_communications_log_loan",
        "platform_communications_log",
        ["loan_id", sa.text("occurred_at DESC")],
    )

    # Append-only enforcement at the database level (same pattern as
    # platform_events, migration 021): no UPDATE, no DELETE, ever.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_FUNCTION}()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'Cannot modify platform_communications_log - append-only communications evidence (Dave mandate #4: stored per account, forever, for collections and court)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_TRIGGER}
        BEFORE UPDATE OR DELETE ON platform_communications_log
        FOR EACH ROW
        EXECUTE FUNCTION {_FUNCTION}();
        """
    )
    op.execute(
        f"""
        COMMENT ON FUNCTION {_FUNCTION}() IS
        'Security function: blocks ALL UPDATEs and DELETEs on platform_communications_log. The communications log is legal evidence (exact message bodies, per account, forever). Do NOT drop trigger {_TRIGGER}.';
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON platform_communications_log")
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION}()")
    op.drop_index(
        "ix_platform_communications_log_loan",
        table_name="platform_communications_log",
    )
    op.drop_index(
        "ix_platform_communications_log_application",
        table_name="platform_communications_log",
    )
    op.drop_index(
        "ix_platform_communications_log_patient",
        table_name="platform_communications_log",
    )
    op.drop_table("platform_communications_log")
