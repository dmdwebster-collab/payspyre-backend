"""Reports depth (WS-H, full-parity wave 1) — scheduled reports + Excel report builder

Revision ID: 062_reports_depth
Revises: 054_hardship
Create Date: 2026-07-20

NOTE (merge train): authored against head ``054_hardship`` in this worktree.
Wave-1 workstreams A..J each carry one migration in slots 055..064; the
orchestrator re-chains this ``down_revision`` to the final pre-merge head —
a one-line change here.

Three tables (video 05 items 6 + 7 of the WS-H build):

* ``platform_report_definitions``   — Excel report builder v1: saved report =
  base dataset + ordered column subset + filters, rendered via the existing
  report_exports XLSX machinery.
* ``platform_report_schedules``     — schedule = report (stock key OR saved
  definition) + cadence + recipients (or per-vendor fan-out for the monthly
  vendor statement); ``last_period_key`` is the idempotency cursor.
* ``platform_report_schedule_runs`` — append-only run log.

No behaviour change on its own: rows exist only once staff use the new
/admin/reports builder + schedule endpoints, and the runner cron is parked
(PR #178 pattern) until a real DB secret is wired.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "062_reports_depth"
down_revision: Union[str, None] = "061_crm"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_report_definitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("base_dataset", sa.String(), nullable=False),
        sa.Column("columns", postgresql.JSONB(), nullable=False),
        sa.Column(
            "filters", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "platform_report_schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("report_key", sa.String(), nullable=True),
        sa.Column(
            "definition_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_report_definitions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "params", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("cadence", sa.String(), nullable=False),
        sa.Column(
            "recipients", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("per_vendor", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_period_key", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_platform_report_schedules_active", "platform_report_schedules", ["active"]
    )

    op.create_table(
        "platform_report_schedule_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "schedule_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_report_schedules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("report_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("recipient_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "ran_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_platform_report_schedule_runs_schedule_period",
        "platform_report_schedule_runs",
        ["schedule_id", "period_key"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_report_schedule_runs_schedule_period",
        table_name="platform_report_schedule_runs",
    )
    op.drop_table("platform_report_schedule_runs")
    op.drop_index(
        "ix_platform_report_schedules_active", table_name="platform_report_schedules"
    )
    op.drop_table("platform_report_schedules")
    op.drop_table("platform_report_definitions")
