"""Add funding and payment tables

Revision ID: 013_add_funding_tables
Revises: 012_seed_test_data
Create Date: 2026-05-15

This migration creates the funding and payment infrastructure:
- funding: Loan disbursements to vendors
- payments: Payment transactions from borrowers
- payment_schedule: Scheduled payment plan for loans
- statements: Monthly statements for borrowers
- refunds: Refund transactions for payments
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '013_add_funding_tables'
down_revision: Union[str, None] = '012_seed_test_data'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ========================================================================
    # CREATE ENUM TYPES
    # ========================================================================
    op.execute("CREATE TYPE IF NOT EXISTS disbursement_method AS ENUM ('etransfer', 'wire', 'cheque')")
    op.execute("CREATE TYPE IF NOT EXISTS funding_status AS ENUM ('pending', 'processing', 'completed', 'failed')")
    op.execute("CREATE TYPE IF NOT EXISTS payment_method AS ENUM ('pre_authorized_debit', 'etransfer', 'cheque')")
    op.execute("CREATE TYPE IF NOT EXISTS payment_status AS ENUM ('pending', 'processing', 'completed', 'failed', 'refunded')")
    op.execute("CREATE TYPE IF NOT EXISTS refund_method AS ENUM ('etransfer', 'wire', 'cheque', 'original_payment')")
    op.execute("CREATE TYPE IF NOT EXISTS refund_status AS ENUM ('pending', 'processing', 'completed', 'failed')")

    # ========================================================================
    # CREATE funding TABLE
    # ========================================================================
    op.create_table(
        'funding',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('application_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('loan_applications.id'), nullable=False),
        sa.Column('disbursement_amount', sa.Numeric(10, 2), nullable=False),
        sa.Column('disbursement_method', sa.Enum('etransfer', 'wire', 'cheque', name='disbursement_method'), nullable=False),
        sa.Column('vendor_account_number', sa.String(50), nullable=True),
        sa.Column('vendor_institution_number', sa.String(20), nullable=True),
        sa.Column('vendor_transit_number', sa.String(20), nullable=True),
        sa.Column('disbursement_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reference_number', sa.String(100), nullable=True),
        sa.Column('stripe_transfer_id', sa.String(255), nullable=True),
        sa.Column('status', sa.Enum('pending', 'processing', 'completed', 'failed', name='funding_status'), nullable=False, server_default='pending'),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), onupdate=sa.text('NOW()')),
    )
    op.create_index('idx_funding_application', 'funding', ['application_id'])
    op.create_index('idx_funding_status', 'funding', ['status'])

    # ========================================================================
    # CREATE payment_schedule TABLE
    # ========================================================================
    op.create_table(
        'payment_schedule',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('application_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('loan_applications.id'), nullable=False),
        sa.Column('payment_number', sa.Numeric(5, 0), nullable=False),
        sa.Column('due_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('payment_amount', sa.Numeric(10, 2), nullable=False),
        sa.Column('principal_amount', sa.Numeric(10, 2), nullable=False),
        sa.Column('interest_amount', sa.Numeric(10, 2), nullable=False),
        sa.Column('remaining_balance', sa.Numeric(10, 2), nullable=False),
        sa.Column('is_paid', sa.String(20), nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()')),
    )
    op.create_index('idx_schedule_application', 'payment_schedule', ['application_id'])
    op.create_index('idx_schedule_due_date', 'payment_schedule', ['due_date'])

    # ========================================================================
    # CREATE payments TABLE
    # ========================================================================
    op.create_table(
        'payments',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('application_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('loan_applications.id'), nullable=False),
        sa.Column('amount', sa.Numeric(10, 2), nullable=False),
        sa.Column('payment_method', sa.Enum('pre_authorized_debit', 'etransfer', 'cheque', name='payment_method'), nullable=False),
        sa.Column('payment_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('transaction_id', sa.String(100), nullable=True),
        sa.Column('status', sa.Enum('pending', 'processing', 'completed', 'failed', 'refunded', name='payment_status'), nullable=False, server_default='pending'),
        sa.Column('principal_amount', sa.Numeric(10, 2), nullable=False, server_default='0'),
        sa.Column('interest_amount', sa.Numeric(10, 2), nullable=False, server_default='0'),
        sa.Column('late_fee_amount', sa.Numeric(10, 2), nullable=False, server_default='0'),
        sa.Column('remaining_balance', sa.Numeric(10, 2), nullable=False),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('refunded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), onupdate=sa.text('NOW()')),
    )
    op.create_index('idx_payment_application', 'payments', ['application_id'])
    op.create_index('idx_payment_status', 'payments', ['status'])
    op.create_index('idx_payment_date', 'payments', ['payment_date'])

    # ========================================================================
    # CREATE statements TABLE
    # ========================================================================
    op.create_table(
        'statements',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('application_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('loan_applications.id'), nullable=False),
        sa.Column('statement_period_start', sa.DateTime(timezone=True), nullable=False),
        sa.Column('statement_period_end', sa.DateTime(timezone=True), nullable=False),
        sa.Column('total_balance', sa.Numeric(10, 2), nullable=False),
        sa.Column('payment_amount_due', sa.Numeric(10, 2), nullable=False),
        sa.Column('due_date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('payments_received', sa.Numeric(10, 2), nullable=False, server_default='0'),
        sa.Column('interest_accrued', sa.Numeric(10, 2), nullable=False, server_default='0'),
        sa.Column('late_fees', sa.Numeric(10, 2), nullable=False, server_default='0'),
        sa.Column('statement_url', sa.String(500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()')),
    )
    op.create_index('idx_statement_application', 'statements', ['application_id'])
    op.create_index('idx_statement_period', 'statements', ['statement_period_start', 'statement_period_end'])

    # ========================================================================
    # CREATE refunds TABLE
    # ========================================================================
    op.create_table(
        'refunds',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('payment_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('payments.id'), nullable=False),
        sa.Column('amount', sa.Numeric(10, 2), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('refund_method', sa.Enum('etransfer', 'wire', 'cheque', 'original_payment', name='refund_method'), nullable=False),
        sa.Column('status', sa.Enum('pending', 'processing', 'completed', 'failed', name='refund_status'), nullable=False, server_default='pending'),
        sa.Column('reference_number', sa.String(100), nullable=True),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()')),
    )
    op.create_index('idx_refund_payment', 'refunds', ['payment_id'])
    op.create_index('idx_refund_status', 'refunds', ['status'])


def downgrade() -> None:
    # Drop in reverse order of dependencies
    op.drop_table('refunds')
    op.drop_table('statements')
    op.drop_table('payments')
    op.drop_table('payment_schedule')
    op.drop_table('funding')

    # Drop enum types
    op.execute("DROP TYPE IF EXISTS refund_status")
    op.execute("DROP TYPE IF EXISTS refund_method")
    op.execute("DROP TYPE IF EXISTS payment_status")
    op.execute("DROP TYPE IF EXISTS payment_method")
    op.execute("DROP TYPE IF EXISTS funding_status")
    op.execute("DROP TYPE IF EXISTS disbursement_method")
