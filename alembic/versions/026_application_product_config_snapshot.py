"""Snapshot the product verification_matrix onto each application at creation

Revision ID: 026_application_product_config_snapshot
Revises: 025_consent_text_worm_trigger
Create Date: 2026-06-06

Closes security-review finding #6 (Hard Rule #7/#8): the decision must be made
against the credit-product config **as it was when the application was created**,
not the live product row.

Before this migration, ``credit_product_version`` was snapshotted as an integer
but the config itself was not — and ``update_credit_product`` mutates the product
row in place while bumping ``version``, so the old config is gone. The flow
orchestrator's ``_decide`` re-fetched the *live* product, so editing a product's
thresholds could change the outcome of already-in-flight applications.

This adds a nullable ``product_config_snapshot`` JSONB column. The orchestrator
populates it with the product's ``verification_matrix`` at application creation
and ``_decide`` reads from it. Nullable so existing rows (created before this
migration) are unaffected; the orchestrator falls back to the live product for
those legacy rows.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "026_application_product_config_snapshot"
down_revision: Union[str, None] = "025_consent_text_worm_trigger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "platform_credit_applications",
        sa.Column("product_config_snapshot", postgresql.JSONB(), nullable=True),
    )
    op.execute(
        """
        COMMENT ON COLUMN platform_credit_applications.product_config_snapshot IS
        'Immutable snapshot of the credit product verification_matrix as of application
        creation (security finding #6 / Hard Rule #7-8). The decision is made against
        this, not the live product row. NULL only for rows created before migration 026.';
        """
    )


def downgrade() -> None:
    op.drop_column("platform_credit_applications", "product_config_snapshot")
