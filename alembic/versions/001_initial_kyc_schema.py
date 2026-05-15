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

    # Additional enum types for expanded schema
    op.execute("CREATE TYPE application_status AS ENUM ('draft', 'pending_documents', 'underwriting', 'approved', 'rejected', 'funded', 'active', 'closed')")
    op.execute("CREATE TYPE payment_frequency AS ENUM ('weekly', 'bi_weekly', 'semi_monthly', 'monthly')")
    op.execute("CREATE TYPE decision_type AS ENUM ('approved', 'rejected', 'manual_review')")
    op.execute("CREATE TYPE employment_status AS ENUM ('employed', 'self_employed', 'unemployed', 'retired', 'student')")
    op.execute("CREATE TYPE business_type AS ENUM ('corporation', 'partnership', 'sole_proprietor')")
    op.execute("CREATE TYPE vendor_status AS ENUM ('pending', 'active', 'suspended', 'terminated')")

    # Create enum types for use in columns (with create_type=False)
    application_status = postgresql.ENUM(name='application_status', create_type=False)
    payment_frequency = postgresql.ENUM(name='payment_frequency', create_type=False)
    decision_type = postgresql.ENUM(name='decision_type', create_type=False)
    employment_status = postgresql.ENUM(name='employment_status', create_type=False)
    business_type = postgresql.ENUM(name='business_type', create_type=False)
    vendor_status = postgresql.ENUM(name='vendor_status', create_type=False)

    # Borrowers table - full schema
    op.create_table(
        'borrowers',
        sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v4()'), nullable=False),
        # Personal info
        sa.Column('first_name', sa.String(length=100), nullable=False),
        sa.Column('last_name', sa.String(length=100), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('phone', sa.String(length=20), nullable=False),
        sa.Column('date_of_birth', sa.DateTime(), nullable=False),
        # Address
        sa.Column('address_line1', sa.String(length=255), nullable=False),
        sa.Column('address_line2', sa.String(length=255), nullable=True),
        sa.Column('city', sa.String(length=100), nullable=False),
        sa.Column('province', sa.String(length=50), nullable=False),
        sa.Column('postal_code', sa.String(length=10), nullable=False),
        sa.Column('country', sa.String(length=2), nullable=False, server_default='CA'),
        # Employment
        sa.Column('employment_status', employment_status, nullable=True),
        sa.Column('employer_name', sa.String(length=255), nullable=True),
        sa.Column('employment_income', sa.Numeric(precision=10, scale=2), nullable=True),
        # Credit
        sa.Column('sin_last_3', sa.String(length=3), nullable=True),
        sa.Column('credit_score', sa.Numeric(precision=5, scale=0), nullable=True),
        sa.Column('credit_history_months', sa.Numeric(precision=5, scale=0), nullable=True),
        sa.Column('credit_utilization', sa.Numeric(precision=5, scale=2), nullable=True),
        # Bank verification
        sa.Column('bank_account_verified', sa.String(length=50), nullable=True),
        sa.Column('bank_account_last_4', sa.String(length=4), nullable=True),
        # Timestamps
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_borrowers_email', 'borrowers', ['email'], unique=True)

    # Vendors table - full schema
    op.create_table(
        'vendors',
        sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v4()'), nullable=False),
        # Business info
        sa.Column('business_name', sa.String(length=255), nullable=False),
        sa.Column('dba_name', sa.String(length=255), nullable=True),
        sa.Column('business_type', business_type, nullable=False),
        # Contact
        sa.Column('contact_name', sa.String(length=255), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('phone', sa.String(length=20), nullable=False),
        # Address
        sa.Column('address_line1', sa.String(length=255), nullable=False),
        sa.Column('address_line2', sa.String(length=255), nullable=True),
        sa.Column('city', sa.String(length=100), nullable=False),
        sa.Column('province', sa.String(length=50), nullable=False),
        sa.Column('postal_code', sa.String(length=10), nullable=False),
        # Licensing
        sa.Column('license_number', sa.String(length=100), nullable=True),
        sa.Column('license_expiry', sa.DateTime(), nullable=True),
        # Status
        sa.Column('status', vendor_status, nullable=False, server_default='pending'),
        # Compliance tracking
        sa.Column('compliance_score', sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column('last_reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('next_review_due', sa.DateTime(timezone=True), nullable=True),
        # Timestamps
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_vendors_email', 'vendors', ['email'], unique=True)

    # Loan applications table - full schema
    op.create_table(
        'loan_applications',
        sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v4()'), nullable=False),
        sa.Column('borrower_id', sa.UUID(), nullable=False),
        sa.Column('vendor_id', sa.UUID(), nullable=False),
        sa.Column('co_borrower_id', sa.UUID(), nullable=True),
        # Application details
        sa.Column('requested_amount', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('purpose', sa.String(length=255), nullable=True),
        sa.Column('treatment_description', sa.Text(), nullable=True),
        # Status
        sa.Column('status', application_status, nullable=False, server_default='draft'),
        # Credit product
        sa.Column('credit_product_code', sa.String(length=50), nullable=True),
        sa.Column('term_months', sa.Numeric(precision=5, scale=0), nullable=True),
        sa.Column('interest_rate', sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column('payment_frequency', payment_frequency, nullable=True),
        # Decision
        sa.Column('decision', decision_type, nullable=True),
        sa.Column('decision_reason', sa.Text(), nullable=True),
        sa.Column('decision_at', sa.DateTime(timezone=True), nullable=True),
        # Timestamps
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('funded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.ForeignKeyConstraint(['borrower_id'], ['borrowers.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['vendor_id'], ['vendors.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['co_borrower_id'], ['borrowers.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_loan_app_borrower', 'loan_applications', ['borrower_id'])
    op.create_index('idx_loan_app_vendor', 'loan_applications', ['vendor_id'])
    op.create_index('idx_loan_app_status', 'loan_applications', ['status'])

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
    # Note: KYC tables are dropped in migration 002 before users
    # to avoid foreign key constraint errors
    # op.drop_table('manual_kyb_reviews')
    # op.drop_table('kyc_co_borrower_links')
    # op.drop_table('kyc_events')
    # op.drop_table('kyc_results')
    # op.drop_table('kyc_sessions')
    # op.drop_table('users')  # dropped in migration 002

    # Drop loan_applications indexes and table
    op.drop_index('idx_loan_app_status', table_name='loan_applications')
    op.drop_index('idx_loan_app_vendor', table_name='loan_applications')
    op.drop_index('idx_loan_app_borrower', table_name='loan_applications')
    op.drop_table('loan_applications')

    # Drop vendors indexes and table
    op.drop_index('idx_vendors_email', table_name='vendors')
    op.drop_table('vendors')

    # Drop borrowers indexes and table
    op.drop_index('idx_borrowers_email', table_name='borrowers')
    op.drop_table('borrowers')

    sa.Enum(name='kyb_review_status').drop(op.get_bind())
    sa.Enum(name='business_structure').drop(op.get_bind())
    sa.Enum(name='co_borrower_role').drop(op.get_bind())
    sa.Enum(name='kyc_check_status').drop(op.get_bind())
    sa.Enum(name='kyc_check_type').drop(op.get_bind())
    sa.Enum(name='kyc_overall_status').drop(op.get_bind())
    sa.Enum(name='kyc_session_status').drop(op.get_bind())

    # Drop additional enums for expanded schema
    op.execute('DROP TYPE IF EXISTS vendor_status CASCADE')
    op.execute('DROP TYPE IF EXISTS business_type CASCADE')
    op.execute('DROP TYPE IF EXISTS employment_status CASCADE')
    op.execute('DROP TYPE IF EXISTS decision_type CASCADE')
    op.execute('DROP TYPE IF EXISTS payment_frequency CASCADE')
    op.execute('DROP TYPE IF EXISTS application_status CASCADE')