"""initial kyc schema

Revision ID: 001
Revises:
Create Date: 2026-05-14 17:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enable UUID extension
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')

    # Create enum types using raw SQL
    op.execute("CREATE TYPE kyc_session_status AS ENUM ('pending', 'in_progress', 'completed', 'expired', 'failed')")
    op.execute("CREATE TYPE kyc_overall_status AS ENUM ('pass', 'fail', 'review_required')")
    op.execute("CREATE TYPE kyc_check_type AS ENUM ('identity', 'liveness', 'aml', 'sanctions')")
    op.execute("CREATE TYPE kyc_check_status AS ENUM ('pass', 'fail', 'pending', 'skip')")
    op.execute("CREATE TYPE co_borrower_role AS ENUM ('guarantor', 'joint_applicant')")
    op.execute("CREATE TYPE business_structure AS ENUM ('sole_proprietor', 'partnership', 'corporation', 'other')")
    op.execute("CREATE TYPE kyb_review_status AS ENUM ('pending_submission', 'under_review', 'approved', 'rejected')")

    # Create enum types for use in columns (with create_type=False)
    from sqlalchemy.dialects import postgresql
    kyc_session_status = postgresql.ENUM(name='kyc_session_status', create_type=False)
    kyc_overall_status = postgresql.ENUM(name='kyc_overall_status', create_type=False)
    kyc_check_type = postgresql.ENUM(name='kyc_check_type', create_type=False)
    kyc_check_status = postgresql.ENUM(name='kyc_check_status', create_type=False)
    co_borrower_role = postgresql.ENUM(name='co_borrower_role', create_type=False)
    business_structure = postgresql.ENUM(name='business_structure', create_type=False)
    kyb_review_status = postgresql.ENUM(name='kyb_review_status', create_type=False)

    # Stub tables
    op.create_table(
        'loan_applications',
        sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v4()'), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table(
        'borrowers',
        sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v4()'), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table(
        'vendors',
        sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v4()'), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table(
        'users',
        sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v4()'), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    # KYC tables
    op.create_table(
        'kyc_sessions',
        sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v4()'), nullable=False),
        sa.Column('loan_application_id', sa.UUID(), nullable=False),
        sa.Column('borrower_id', sa.UUID(), nullable=False),
        sa.Column('vendor', sa.String(length=50), nullable=False),
        sa.Column('vendor_session_id', sa.String(length=255), nullable=True),
        sa.Column('verification_url', sa.Text(), nullable=True),
        sa.Column('status', kyc_session_status, nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('expires_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('metadata', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['borrower_id'], ['borrowers.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['loan_application_id'], ['loan_applications.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_kyc_sessions_loan_app', 'kyc_sessions', ['loan_application_id'])
    op.create_index('idx_kyc_sessions_status', 'kyc_sessions', ['status'])
    op.create_index('idx_kyc_sessions_vendor', 'kyc_sessions', ['vendor'])

    op.create_table(
        'kyc_results',
        sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v4()'), nullable=False),
        sa.Column('kyc_session_id', sa.UUID(), nullable=False),
        sa.Column('vendor', sa.String(length=50), nullable=False),
        sa.Column('overall_status', kyc_overall_status, nullable=False),
        sa.Column('check_type', kyc_check_type, nullable=False),
        sa.Column('check_status', kyc_check_status, nullable=False),
        sa.Column('check_details', sa.JSON(), nullable=True),
        sa.Column('score', sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column('flags', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.ForeignKeyConstraint(['kyc_session_id'], ['kyc_sessions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_kyc_results_session', 'kyc_results', ['kyc_session_id'])
    op.create_index('idx_kcy_results_status', 'kyc_results', ['overall_status'])

    op.create_table(
        'kyc_events',
        sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v4()'), nullable=False),
        sa.Column('kyc_session_id', sa.UUID(), nullable=False),
        sa.Column('event_type', sa.String(length=100), nullable=False),
        sa.Column('vendor_event_id', sa.String(length=255), nullable=True),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('processed_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.ForeignKeyConstraint(['kyc_session_id'], ['kyc_sessions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_kyc_events_session', 'kyc_events', ['kyc_session_id'])
    op.create_index('idx_kyc_events_type', 'kyc_events', ['event_type'])

    op.create_table(
        'kyc_co_borrower_links',
        sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v4()'), nullable=False),
        sa.Column('loan_application_id', sa.UUID(), nullable=False),
        sa.Column('primary_kyc_session_id', sa.UUID(), nullable=False),
        sa.Column('co_borrower_kyc_session_id', sa.UUID(), nullable=False),
        sa.Column('co_borrower_role', co_borrower_role, nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.ForeignKeyConstraint(['co_borrower_kyc_session_id'], ['kyc_sessions.id']),
        sa.ForeignKeyConstraint(['loan_application_id'], ['loan_applications.id']),
        sa.ForeignKeyConstraint(['primary_kyc_session_id'], ['kyc_sessions.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_kyc_co_borrower_loan', 'kyc_co_borrower_links', ['loan_application_id'])

    op.create_table(
        'manual_kyb_reviews',
        sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v4()'), nullable=False),
        sa.Column('vendor_id', sa.UUID(), nullable=False),
        sa.Column('business_name', sa.String(length=255), nullable=False),
        sa.Column('business_structure', business_structure, nullable=False),
        sa.Column('business_registration_number', sa.String(length=100), nullable=True),
        sa.Column('status', kyb_review_status, nullable=False),
        sa.Column('submitted_by', sa.UUID(), nullable=True),
        sa.Column('reviewed_by', sa.UUID(), nullable=True),
        sa.Column('documents', sa.JSON(), nullable=True),
        sa.Column('beneficial_owners', sa.JSON(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.ForeignKeyConstraint(['reviewed_by'], ['users.id']),
        sa.ForeignKeyConstraint(['submitted_by'], ['users.id']),
        sa.ForeignKeyConstraint(['vendor_id'], ['vendors.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_manual_kyb_vendor', 'manual_kyb_reviews', ['vendor_id'])
    op.create_index('idx_manual_kyb_status', 'manual_kyb_reviews', ['status'])


def downgrade() -> None:
    op.drop_index('idx_manual_kyb_status', table_name='manual_kyb_reviews')
    op.drop_index('idx_manual_kyb_vendor', table_name='manual_kyb_reviews')
    op.drop_table('manual_kyb_reviews')

    op.drop_index('idx_kyc_co_borrower_loan', table_name='kyc_co_borrower_links')
    op.drop_table('kyc_co_borrower_links')

    op.drop_index('idx_kyc_events_type', table_name='kyc_events')
    op.drop_index('idx_kyc_events_session', table_name='kyc_events')
    op.drop_table('kyc_events')

    op.drop_index('idx_kcy_results_status', table_name='kyc_results')
    op.drop_index('idx_kyc_results_session', table_name='kyc_results')
    op.drop_table('kyc_results')

    op.drop_index('idx_kyc_sessions_vendor', table_name='kyc_sessions')
    op.drop_index('idx_kyc_sessions_status', table_name='kyc_sessions')
    op.drop_index('idx_kyc_sessions_loan_app', table_name='kyc_sessions')
    op.drop_table('kyc_sessions')

    op.drop_table('users')
    op.drop_table('vendors')
    op.drop_table('borrowers')
    op.drop_table('loan_applications')

    sa.Enum(name='kyb_review_status').drop(op.get_bind())
    sa.Enum(name='business_structure').drop(op.get_bind())
    sa.Enum(name='co_borrower_role').drop(op.get_bind())
    sa.Enum(name='kyc_check_status').drop(op.get_bind())
    sa.Enum(name='kyc_check_type').drop(op.get_bind())
    sa.Enum(name='kyc_overall_status').drop(op.get_bind())
    sa.Enum(name='kyc_session_status').drop(op.get_bind())