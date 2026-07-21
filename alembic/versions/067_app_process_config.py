"""Application-process config + product-policy config (Wave 2 W2-APPCONFIG)

Revision ID: 067_app_process_config
Revises: 064_borrower_depth
Create Date: 2026-07-20

NOTE (merge train): down_revision pinned to the single head at branch time,
``064_borrower_depth``. Wave-2 workstreams branch from the same head in parallel;
if another Wave-2 migration lands first, re-chain this down_revision to the new
head (one-line change) — there is NO ordering dependency between them.

Two additive pieces, NO behaviour change on their own (readers fall back to the
shipped typed defaults, which reproduce current behaviour exactly):

1. ``platform_credit_products.policy_config`` (JSONB, nullable) — the typed
   product-policy tabs beyond pricing (grace period / due dates / due-date
   seasons / payoff / disbursement / approval / repayment modes). NULL on every
   existing row → ``ProductPolicyConfig`` defaults apply (engine behaviour is
   unchanged; the payoff config only DESCRIBES the fixed engine math).

2. ``platform_application_process_config`` — single-row (id=1) table holding the
   platform-wide application-process config document (flow confirmations, offer
   expiry 30d / max 3, dictionaries, disclaimer, co-applicant, form variants).
   No row → ``ApplicationProcessConfig`` defaults apply, and the offer engine's
   expiry/max-offers behaviour is preserved (defaults equal settings.OFFER_*).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "067_app_process_config"
down_revision: Union[str, None] = "064_borrower_depth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Per-product policy config (nullable → default ProductPolicyConfig).
    op.add_column(
        "platform_credit_products",
        sa.Column("policy_config", postgresql.JSONB(), nullable=True),
    )

    # 2. Single-row application-process config.
    op.create_table(
        "platform_application_process_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "config",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name="ck_platform_application_process_config_single_row"),
    )


def downgrade() -> None:
    op.drop_table("platform_application_process_config")
    op.drop_column("platform_credit_products", "policy_config")
