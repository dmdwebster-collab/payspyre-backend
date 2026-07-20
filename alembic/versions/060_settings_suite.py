"""Settings suite (Workstream F — Turnkey Settings parity, videos 07-08)

Revision ID: 060_settings_suite
Revises: 054_hardship
Create Date: 2026-07-20

NOTE (merge train): down_revision pinned to the current single head
``054_hardship``. If a parallel workstream lands first, re-chain this
down_revision to the new head (one-line change).

Four additive pieces, no behaviour change on their own:

1. ``platform_decision_rules`` — admin EDITS over the decision-rules directory
   (code registry in app/services/decision_rules.py holds the defaults, seeded
   from the Turnkey video-08 values; no row → shipped default → decision path
   unchanged).
2. ``platform_company_info`` — single-row company configuration (names, logo /
   favicon refs, multi-contact list) consumed by documents + notifications via
   app/services/company_info.py.
3. ``platform_business_calendar_overrides`` — admin add/remove business-closure
   dates layered over the computed Canadian statutory-holiday calendar.
4. ``platform_notification_rules`` gains ``audience_channels`` (4-audience ×
   3-channel matrix cells) + ``attachments`` (TL attachment picker) — same row
   the template editor already uses; storage not forked.

Plus the WS-F granular permissions (hardship.manage / transactions.backdate /
decisions.override), seeded idempotently and granted to the ``admin`` role
(static SQL; admins are implicitly allowed anyway — the grant keeps the
role→permission table self-describing).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "060_settings_suite"
down_revision: Union[str, None] = "054_hardship"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_PERMISSIONS = (
    ("hardship.manage", "Manage hardship requests (create/apply/cancel)", "hardship", "manage"),
    ("transactions.backdate", "Backdate loan transactions within the configured window", "transactions", "backdate"),
    ("decisions.override", "Override an existing automated/staff credit decision", "decisions", "override"),
)


def upgrade() -> None:
    # 1. Decision-rules directory (edits only; registry holds defaults).
    op.create_table(
        "platform_decision_rules",
        sa.Column("rule_key", sa.String(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=True),
        sa.Column(
            "params", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    # 2. Company info (single row, id forced to 1).
    op.create_table(
        "platform_company_info",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("legal_name", sa.String(), nullable=False),
        sa.Column("operating_name", sa.String(), nullable=False),
        sa.Column("brand_name", sa.String(), nullable=True),
        sa.Column("lending_type", sa.String(), nullable=True),
        sa.Column("logo_ref", sa.String(), nullable=True),
        sa.Column("favicon_ref", sa.String(), nullable=True),
        sa.Column(
            "contacts", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name="ck_platform_company_info_single_row"),
    )

    # 3. Business-calendar overrides.
    op.create_table(
        "platform_business_calendar_overrides",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("province", sa.String(3), nullable=False, server_default="ALL"),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("label", sa.String(200), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "kind IN ('closure', 'business_day')",
            name="ck_business_calendar_override_kind",
        ),
    )
    op.create_index(
        "uq_business_calendar_override",
        "platform_business_calendar_overrides",
        ["date", "province"],
        unique=True,
    )
    op.create_index(
        "idx_business_calendar_override_date",
        "platform_business_calendar_overrides",
        ["date"],
    )

    # 4. Notification matrix columns on the EXISTING rules table.
    op.add_column(
        "platform_notification_rules",
        sa.Column(
            "audience_channels",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "platform_notification_rules",
        sa.Column(
            "attachments",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    # 5. Granular permissions (idempotent static SQL; mirror 009's conventions).
    connection = op.get_bind()
    for name, description, resource, action in _NEW_PERMISSIONS:
        connection.execute(
            sa.text(
                """
                INSERT INTO permissions (id, name, description, resource, action, created_at)
                VALUES (uuid_generate_v4(), :name, :description, :resource, :action, now())
                ON CONFLICT (name) DO NOTHING
                """
            ),
            {"name": name, "description": description, "resource": resource, "action": action},
        )
    # Grant to the admin role (self-describing; admins are implicitly allowed).
    connection.execute(
        sa.text(
            """
            INSERT INTO role_permissions (id, role_id, permission_id, created_at)
            SELECT uuid_generate_v4(), r.id, p.id, now()
            FROM roles r
            JOIN permissions p ON p.name IN ('hardship.manage', 'transactions.backdate', 'decisions.override')
            WHERE r.name = 'admin'
            ON CONFLICT (role_id, permission_id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            DELETE FROM role_permissions rp
            USING permissions p
            WHERE rp.permission_id = p.id
              AND p.name IN ('hardship.manage', 'transactions.backdate', 'decisions.override')
            """
        )
    )
    connection.execute(
        sa.text(
            """
            DELETE FROM permissions
            WHERE name IN ('hardship.manage', 'transactions.backdate', 'decisions.override')
            """
        )
    )
    op.drop_column("platform_notification_rules", "attachments")
    op.drop_column("platform_notification_rules", "audience_channels")
    op.drop_index(
        "idx_business_calendar_override_date",
        table_name="platform_business_calendar_overrides",
    )
    op.drop_index(
        "uq_business_calendar_override",
        table_name="platform_business_calendar_overrides",
    )
    op.drop_table("platform_business_calendar_overrides")
    op.drop_table("platform_company_info")
    op.drop_table("platform_decision_rules")
