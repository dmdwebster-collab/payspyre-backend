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
    # Application list queries (admin dashboard)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_loan_apps_status_created
        ON loan_applications (status, created_at DESC)
    """)

    # KYC sessions pending review
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_kyc_pending_review
        ON kyc_sessions (status, created_at DESC)
    """)

    # KYC results by overall status
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_kyc_results_overall_status
        ON kyc_results (overall_status, created_at DESC)
    """)

    # Vendors pending activation
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_vendor_pending
        ON vendors (status, created_at DESC)
    """)

    # Vendor compliance score queries
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_vendor_compliance
        ON vendors (status, compliance_score)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_vendor_compliance")
    op.execute("DROP INDEX IF EXISTS idx_vendor_pending")
    op.execute("DROP INDEX IF EXISTS idx_kyc_results_overall_status")
    op.execute("DROP INDEX IF EXISTS idx_kyc_pending_review")
    op.execute("DROP INDEX IF EXISTS idx_loan_apps_status_created")
