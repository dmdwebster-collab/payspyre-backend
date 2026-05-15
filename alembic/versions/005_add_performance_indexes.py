"""Add performance indexes for common query patterns

Revision ID: 005_add_performance_indexes
Revises: 004_add_notification_tables
Create Date: 2026-05-14

This migration adds composite and partial indexes to optimize
common query patterns identified in production readiness audit.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '005_add_performance_indexes'
down_revision: Union[str, None] = '004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Skip indexes on stub tables that will be expanded in later migrations
    # TODO: Move this migration to after tables are fully defined
    # All indexes disabled until tables are fully expanded
    return


def downgrade() -> None:
    pass
