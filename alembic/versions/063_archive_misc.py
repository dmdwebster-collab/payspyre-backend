"""Archive + servicing misc (WS-I): blacklists, bureau batches, backdate permission.

Revision ID: 063_archive_misc
Revises: 054_hardship
Create Date: 2026-07-20

NOTE (merge train): down_revision chained onto ``054_hardship`` — the single
alembic head when this branch was cut. If a parallel workstream lands first,
the orchestrator re-chains this one line (plus the pin in
``tests/test_parity_i_archive_misc.py``).

Three additive pieces, no changes to any existing table:

1. ``platform_blacklist_entries`` — Turnkey Tools → Blacklists parity: staff
   lists of suspicious values (name / SIN / phone / email / driver's license /
   account number). Matches at intake FLAG to manual review — never
   auto-decline (Dave's rule). Soft-delete via ``active``.

2. ``platform_bureau_batches`` — Equifax month-end batch generation, TEST MODE
   ONLY: the Metro2-style flat file for Pot-60+ reportable accounts is stored
   in-row; nothing is transmitted (agreement pending). Idempotent per month.

3. Seeds the ``ledger.backdate`` permission — gates permission-bounded
   backdating of ledger effective dates and custom-transaction scheduled
   dates (turns PR #179's hard no-backdate block into a bounded, audited,
   permissioned path). Admin is implicitly allowed
   (``require_permission_or_admin``); staff need the explicit grant.

The Archive itself (terminal-record list + frozen polymorphic detail) is
read-only over EXISTING tables — no schema needed.
"""
from datetime import datetime, timezone
from typing import Sequence, Union
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "063_archive_misc"
down_revision: Union[str, None] = "062_reports_depth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    category = postgresql.ENUM(
        "name",
        "sin",
        "phone",
        "email",
        "drivers_license",
        "account_number",
        name="platform_blacklist_category",
    )
    category.create(bind, checkfirst=True)

    op.create_table(
        "platform_blacklist_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "category",
            postgresql.ENUM(name="platform_blacklist_category", create_type=False),
            nullable=False,
        ),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("value_normalized", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deactivated_by", sa.String(), nullable=True),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_platform_blacklist_cat_value_active",
        "platform_blacklist_entries",
        ["category", "value_normalized"],
        postgresql_where=sa.text("active"),
    )

    op.create_table(
        "platform_bureau_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("batch_month", sa.Date(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False, server_default="test"),
        sa.Column("file_name", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("account_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "total_balance_cents", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("generation_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("generated_by", sa.String(), nullable=False),
        sa.Column(
            "generated_at",
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
        sa.UniqueConstraint("batch_month", name="uq_platform_bureau_batch_month"),
    )

    # Seed the backdate permission (grantable via the existing RBAC admin
    # surface; admin role is implicitly allowed by require_permission_or_admin).
    bind.execute(
        sa.text(
            """
            INSERT INTO permissions (id, name, description, resource, action, created_at)
            VALUES (:id, :name, :description, :resource, :action, :now)
            ON CONFLICT (name) DO NOTHING
            """
        ),
        {
            "id": uuid4(),
            "name": "ledger.backdate",
            "description": (
                "Backdate ledger effective dates / custom-transaction dates "
                "(bounded window, mandatory comment, audited)"
            ),
            "resource": "ledger",
            "action": "backdate",
            "now": datetime.now(timezone.utc),
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            DELETE FROM role_permissions
            WHERE permission_id IN (
                SELECT id FROM permissions WHERE name = 'ledger.backdate'
            )
            """
        )
    )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE name = 'ledger.backdate'")
    )
    op.drop_table("platform_bureau_batches")
    op.drop_index(
        "ix_platform_blacklist_cat_value_active", table_name="platform_blacklist_entries"
    )
    op.drop_table("platform_blacklist_entries")
    postgresql.ENUM(name="platform_blacklist_category").drop(bind, checkfirst=True)
