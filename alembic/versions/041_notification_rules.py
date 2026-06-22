"""System-configurable notification rules + send-window scaffolding

Revision ID: 041_notification_rules
Revises: 040_notification_cursor
Create Date: 2026-06-22

Creates ``platform_notification_rules`` — the DB-backed config that replaces the
hardcoded notification cadence/channels (``DunningPolicy`` dataclass). Seeds
David's CURRENT cadence so behaviour is unchanged on first deploy:

* payment_due_reminder — offsets [7, 2] (days BEFORE due), email on, SMS +
  dashboard present but DISABLED.
* payment_overdue — offsets [7, 14, 21, 30, 60, 90] (days AFTER due), email on,
  SMS + dashboard present but DISABLED.

Borrower province: a ``province`` column was NOT added — the borrower's province
already lives in ``platform_patient_fields`` (field_key='province', JSONB
field_value), confirmed by grep + app/services/flow_orchestrator.py:764
("province lives in platform_patient_fields"). The send-window mechanism reads
it from there; no schema change needed.

Per-province communication windows are seeded as constants in
``app.services.notification_config`` (PLACEHOLDER hours — see that module's
LEGAL comment), not a DB table, since they are a compliance MECHANISM that legal
must confirm before launch, not an admin-tunable knob.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "041_notification_rules"
down_revision: Union[str, None] = "040_notification_cursor"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_notification_rules",
        sa.Column("notification_type", sa.String(), primary_key=True),
        sa.Column("offsets", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("email_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("dashboard_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sms_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "content_overrides", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Seed David's CURRENT cadence (confirmed 2026-06-22). Email-only today; SMS
    # + dashboard rows exist but are DISABLED so the team can flip them on later
    # without a code change. Offsets are days-before (reminder) / days-after
    # (overdue).
    rules = sa.table(
        "platform_notification_rules",
        sa.column("notification_type", sa.String),
        sa.column("offsets", JSONB),
        sa.column("email_enabled", sa.Boolean),
        sa.column("dashboard_enabled", sa.Boolean),
        sa.column("sms_enabled", sa.Boolean),
        sa.column("content_overrides", JSONB),
        sa.column("enabled", sa.Boolean),
    )
    op.bulk_insert(
        rules,
        [
            {
                "notification_type": "payment_due_reminder",
                "offsets": [7, 2],
                "email_enabled": True,
                "dashboard_enabled": False,
                "sms_enabled": False,
                "content_overrides": {},
                "enabled": True,
            },
            {
                "notification_type": "payment_overdue",
                "offsets": [7, 14, 21, 30, 60, 90],
                "email_enabled": True,
                "dashboard_enabled": False,
                "sms_enabled": False,
                "content_overrides": {},
                "enabled": True,
            },
        ],
    )


def downgrade() -> None:
    op.drop_table("platform_notification_rules")
