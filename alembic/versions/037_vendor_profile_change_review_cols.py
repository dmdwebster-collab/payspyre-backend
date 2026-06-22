"""Add admin-review audit columns to platform_vendor_profile_change_requests

Revision ID: 037_vendor_profile_change_review_cols
Revises: 036_vendor_profile_change_requests
Create Date: 2026-06-22

Admin-side approval flow (spec §3.6). When an admin approves/rejects a vendor
profile change request we record WHO decided, WHEN, and an optional decision
note. Approval applies the whitelisted ``requested_changes`` to the ``vendors``
row (done in the API layer, not here); these columns are the durable audit trail.

Additive only. All three columns are nullable (NULL = still pending / never
reviewed). Downgrade drops them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "037_vendor_profile_change_review_cols"
down_revision: Union[str, None] = "036_vendor_profile_change_requests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "platform_vendor_profile_change_requests"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("reviewed_by", sa.String(), nullable=True))
    op.add_column(
        _TABLE, sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(_TABLE, sa.Column("review_note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, "review_note")
    op.drop_column(_TABLE, "reviewed_at")
    op.drop_column(_TABLE, "reviewed_by")
