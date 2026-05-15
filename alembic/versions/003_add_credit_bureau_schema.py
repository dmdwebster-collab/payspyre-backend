"""Add credit bureau schema

Revision ID: 003
Revises: 002
Create Date: 2026-05-14 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '003'
down_revision = '002'
branch_labels = None
depends_on = None


def upgrade():
    # Note: credit_history_months and credit_utilization columns
    # are now created in migration 001 as part of the full borrowers schema

    # Create credit_inquiries table
    op.create_table(
        'credit_inquiries',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('borrower_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('borrowers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('loan_application_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('loan_applications.id'), nullable=True),
        sa.Column('sin_last_3', sa.String(3), nullable=False),
        sa.Column('date_of_birth', sa.DateTime(), nullable=False),
        sa.Column('postal_code', sa.String(10), nullable=False),
        sa.Column('first_name', sa.String(100), nullable=False),
        sa.Column('last_name', sa.String(100), nullable=False),
        sa.Column('bureaus_queried', sa.String(100), nullable=False),
        sa.Column('use_cache', sa.String(10), nullable=False, server_default='true'),
        sa.Column('initiated_by', sa.String(100), nullable=True),
        sa.Column('status', sa.Enum('pending', 'success', 'partial', 'failed', name='inquiry_status'), nullable=False, server_default='pending'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('cached', sa.String(10), nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    op.create_index('idx_credit_inquiry_borrower', 'credit_inquiries', ['borrower_id'])
    op.create_index('idx_credit_inquiry_app', 'credit_inquiries', ['loan_application_id'])
    op.create_index('idx_credit_inquiry_status', 'credit_inquiries', ['status'])
    op.create_index('idx_credit_inquiry_created', 'credit_inquiries', ['created_at'])

    # Create credit_reports table
    op.create_table(
        'credit_reports',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('inquiry_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('credit_inquiries.id', ondelete='CASCADE'), nullable=False),
        sa.Column('bureau', sa.String(50), nullable=False),
        sa.Column('score', sa.Numeric(5, 0), nullable=True),
        sa.Column('score_min', sa.Numeric(5, 0), nullable=True),
        sa.Column('score_max', sa.Numeric(5, 0), nullable=True),
        sa.Column('utilization_percent', sa.Numeric(5, 2), nullable=True),
        sa.Column('delinquency_count', sa.Numeric(5, 0), nullable=True),
        sa.Column('credit_history_months', sa.Numeric(5, 0), nullable=True),
        sa.Column('trade_count', sa.Numeric(5, 0), nullable=True),
        sa.Column('inquiry_count_6m', sa.Numeric(5, 0), nullable=True),
        sa.Column('inquiry_count_12m', sa.Numeric(5, 0), nullable=True),
        sa.Column('has_bankruptcy', sa.String(10), nullable=False, server_default='false'),
        sa.Column('has_collections', sa.String(10), nullable=False, server_default='false'),
        sa.Column('raw_response', postgresql.JSON(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_index('idx_credit_report_inquiry', 'credit_reports', ['inquiry_id'])
    op.create_index('idx_credit_report_bureau', 'credit_reports', ['bureau'])
    op.create_index('idx_credit_report_score', 'credit_reports', ['score'])


def downgrade():
    op.drop_index('idx_credit_report_score', 'credit_reports')
    op.drop_index('idx_credit_report_bureau', 'credit_reports')
    op.drop_index('idx_credit_report_inquiry', 'credit_reports')
    op.drop_table('credit_reports')

    op.drop_index('idx_credit_inquiry_created', 'credit_inquiries')
    op.drop_index('idx_credit_inquiry_status', 'credit_inquiries')
    op.drop_index('idx_credit_inquiry_app', 'credit_inquiries')
    op.drop_index('idx_credit_inquiry_borrower', 'credit_inquiries')
    op.drop_table('credit_inquiries')

    # Note: credit_history_months and credit_utilization columns
    # are dropped as part of migration 001 downgrade