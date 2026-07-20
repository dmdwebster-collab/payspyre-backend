"""Collections work-surface (WS-C, Turnkey full parity)

Revision ID: 057_collections_worksurface
Revises: 054_hardship
Create Date: 2026-07-20

NOTE ON CHAINING: origin/main's single migration head was ``054_hardship``
when this branch was cut; the orchestrator re-chains down_revisions if
sibling workstreams land first (P0_BUILD_PLAN convention).

Dave's collections modernization (04__WP_Collections), building ON the
existing delinquency-bucket state machine (053) and actuals ledger (049):

  * ``platform_collector_assignments`` — bulk-assignable collector queues
    with junior/senior tiers ("assign to me one-at-a-time is just not
    viable"). One ACTIVE assignment per loan (partial unique index);
    history kept by deactivation.
  * ``platform_collection_action_types`` — settings-definable directory of
    action-plan types (admin CRUD, soft-deactivate only).
  * ``platform_collection_actions`` — per-loan action-plan entries (type,
    due date, assignee, mandatory comment, outcome), full history.
  * ``platform_promises_to_pay`` — amount / date / mandatory comment /
    no-late-fee flag; open → kept | broken evaluated against the actual
    payment ledger.
  * ``platform_insolvency_maintenance_fees`` — idempotency marker (one per
    loan per month) for the insolvency-portfolio maintenance fee; the money
    is an immutable add-on-bucket ledger ``fee`` row.

No behaviour change on its own: rows only exist once staff use the new
endpoints. record_payment and the interest engine are untouched. Money is
integer cents (repo convention).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "057_collections_worksurface"
down_revision: Union[str, None] = "054_hardship"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    tier = postgresql.ENUM("junior", "senior", name="platform_collector_tier")
    tier.create(bind, checkfirst=True)

    action_status = postgresql.ENUM(
        "planned",
        "completed",
        "cancelled",
        name="platform_collection_action_status",
    )
    action_status.create(bind, checkfirst=True)

    ptp_status = postgresql.ENUM(
        "open", "kept", "broken", "cancelled", name="platform_ptp_status"
    )
    ptp_status.create(bind, checkfirst=True)

    # ---- Collector assignments -------------------------------------------
    op.create_table(
        "platform_collector_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "collector_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tier",
            postgresql.ENUM(name="platform_collector_tier", create_type=False),
            nullable=False,
        ),
        sa.Column("method", sa.String(), nullable=False, server_default="manual"),
        sa.Column(
            "active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("assigned_by", sa.String(), nullable=False),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("unassigned_by", sa.String(), nullable=True),
        sa.Column("unassigned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_platform_collector_assignment_active_loan",
        "platform_collector_assignments",
        ["loan_id"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_index(
        "ix_platform_collector_assignments_collector_active",
        "platform_collector_assignments",
        ["collector_user_id", "active"],
    )

    # ---- Action-type directory (settings) --------------------------------
    op.create_table(
        "platform_collection_action_types",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(), nullable=False, unique=True),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column(
            "active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
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

    # ---- Per-loan action plans -------------------------------------------
    op.create_table(
        "platform_collection_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "action_type_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_collection_action_types.id"),
            nullable=False,
        ),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column(
            "assignee_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                name="platform_collection_action_status", create_type=False
            ),
            nullable=False,
            server_default="planned",
        ),
        sa.Column("comment", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("completed_by", sa.String(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_by", sa.String(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index(
        "ix_platform_collection_actions_loan_due",
        "platform_collection_actions",
        ["loan_id", "due_date"],
    )
    op.create_index(
        "ix_platform_collection_actions_assignee_status",
        "platform_collection_actions",
        ["assignee_user_id", "status", "due_date"],
    )

    # ---- Promise to pay ---------------------------------------------------
    op.create_table(
        "platform_promises_to_pay",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("promised_date", sa.Date(), nullable=False),
        sa.Column("comment", sa.String(), nullable=False),
        sa.Column(
            "no_late_fees",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="platform_ptp_status", create_type=False),
            nullable=False,
            server_default="open",
        ),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("covered_cents", sa.BigInteger(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("cancelled_by", sa.String(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("amount_cents > 0", name="ck_platform_ptp_amount_positive"),
    )
    op.create_index(
        "ix_platform_ptp_loan_status",
        "platform_promises_to_pay",
        ["loan_id", "status"],
    )
    op.create_index(
        "ix_platform_ptp_status_date",
        "platform_promises_to_pay",
        ["status", "promised_date"],
    )

    # ---- Insolvency maintenance-fee idempotency markers ------------------
    op.create_table(
        "platform_insolvency_maintenance_fees",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fee_month", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loan_transactions.id"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "loan_id", "fee_month", name="uq_platform_insolvency_fee_loan_month"
        ),
        sa.CheckConstraint(
            "amount_cents > 0", name="ck_platform_insolvency_fee_amount_positive"
        ),
    )


def downgrade() -> None:
    op.drop_table("platform_insolvency_maintenance_fees")
    op.drop_index("ix_platform_ptp_status_date", table_name="platform_promises_to_pay")
    op.drop_index("ix_platform_ptp_loan_status", table_name="platform_promises_to_pay")
    op.drop_table("platform_promises_to_pay")
    op.drop_index(
        "ix_platform_collection_actions_assignee_status",
        table_name="platform_collection_actions",
    )
    op.drop_index(
        "ix_platform_collection_actions_loan_due",
        table_name="platform_collection_actions",
    )
    op.drop_table("platform_collection_actions")
    op.drop_table("platform_collection_action_types")
    op.drop_index(
        "ix_platform_collector_assignments_collector_active",
        table_name="platform_collector_assignments",
    )
    op.drop_index(
        "uq_platform_collector_assignment_active_loan",
        table_name="platform_collector_assignments",
    )
    op.drop_table("platform_collector_assignments")

    bind = op.get_bind()
    postgresql.ENUM(name="platform_ptp_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="platform_collection_action_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="platform_collector_tier").drop(bind, checkfirst=True)
