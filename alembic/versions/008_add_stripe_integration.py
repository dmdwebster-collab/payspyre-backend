"""Add Stripe payment integration

Revision ID: 008_add_stripe_integration
Revises: 007_add_document_storage
Create Date: 2026-05-14

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '008_add_stripe_integration'
down_revision = '007_add_document_storage'
branch_labels = None
depends_on = None


def upgrade():
    # Skip - depends on payments/refunds tables that don't exist yet
    # TODO: Create payments/refunds tables first or remove these foreign keys
    return
    # Payment methods table
    op.create_table(
        'payment_methods',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('borrower_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('borrowers.id'), nullable=False),
        sa.Column('stripe_payment_method_id', sa.String(255), nullable=True),
        sa.Column('stripe_customer_id', sa.String(255), nullable=True),
        sa.Column('payment_method_type', sa.Enum('card', 'us_bank_account', 'pad', name='payment_method_type'), nullable=False),
        sa.Column('card_last_4', sa.String(4), nullable=True),
        sa.Column('card_brand', sa.String(50), nullable=True),
        sa.Column('card_exp_month', sa.Numeric(2, 0), nullable=True),
        sa.Column('card_exp_year', sa.Numeric(4, 0), nullable=True),
        sa.Column('bank_account_last_4', sa.String(4), nullable=True),
        sa.Column('bank_account_bank_name', sa.String(255), nullable=True),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('is_verified', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('status', sa.Enum('active', 'inactive', 'expired', 'failed', name='payment_method_status'), nullable=False, server_default='active'),
        sa.Column('stripe_response', postgresql.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), onupdate=sa.text('now()'), nullable=False),
    )
    op.create_index('idx_payment_method_borrower', 'payment_methods', ['borrower_id'])
    op.create_index('idx_payment_method_stripe', 'payment_methods', ['stripe_payment_method_id'])
    op.create_index('idx_payment_method_status', 'payment_methods', ['status'])
    op.create_index('idx_payment_method_default', 'payment_methods', ['is_default'])

    # Stripe accounts table
    op.create_table(
        'stripe_accounts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('vendor_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('vendors.id', ondelete='CASCADE'), nullable=False),
        sa.Column('stripe_account_id', sa.String(255), nullable=False, unique=True),
        sa.Column('stripe_account_type', sa.Enum('express', 'standard', 'custom', name='stripe_account_type'), nullable=False, server_default='express'),
        sa.Column('onboarding_status', sa.Enum('not_started', 'pending', 'completed', 'rejected', name='onboarding_status'), nullable=False, server_default='not_started'),
        sa.Column('onboarding_completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('charges_enabled', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('payouts_enabled', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('details_submitted', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('default_payout_schedule', sa.Enum('manual', 'daily', 'weekly', 'monthly', name='payout_schedule'), nullable=False, server_default='manual'),
        sa.Column('status', sa.Enum('active', 'restricted', 'suspended', 'closed', name='stripe_account_status'), nullable=False, server_default='active'),
        sa.Column('stripe_account_data', postgresql.JSON(), nullable=True),
        sa.Column('onboarding_url', sa.String(500), nullable=True),
        sa.Column('onboarding_url_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), onupdate=sa.text('now()'), nullable=False),
    )
    op.create_index('idx_stripe_account_vendor', 'stripe_accounts', ['vendor_id'])
    op.create_index('idx_stripe_account_stripe_id', 'stripe_accounts', ['stripe_account_id'])
    op.create_index('idx_stripe_account_status', 'stripe_accounts', ['status'])

    # Stripe transactions table
    op.create_table(
        'stripe_transactions',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('application_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('loan_applications.id'), nullable=True),
        sa.Column('payment_method_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('payment_methods.id'), nullable=True),
        sa.Column('stripe_account_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('stripe_accounts.id'), nullable=True),
        sa.Column('payment_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('payments.id'), nullable=True),
        sa.Column('refund_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('refunds.id'), nullable=True),
        sa.Column('stripe_payment_intent_id', sa.String(255), nullable=True),
        sa.Column('stripe_transfer_id', sa.String(255), nullable=True),
        sa.Column('stripe_payout_id', sa.String(255), nullable=True),
        sa.Column('stripe_charge_id', sa.String(255), nullable=True),
        sa.Column('stripe_refund_id', sa.String(255), nullable=True),
        sa.Column('transaction_type', sa.Enum('payment', 'disbursement', 'payout', 'refund', 'transfer', 'fee', name='transaction_type'), nullable=False),
        sa.Column('amount', sa.Numeric(10, 2), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False, server_default='cad'),
        sa.Column('status', sa.Enum('pending', 'succeeded', 'failed', 'canceled', 'processing', name='stripe_transaction_status'), nullable=False, server_default='pending'),
        sa.Column('stripe_fee', sa.Numeric(10, 2), nullable=True),
        sa.Column('application_fee', sa.Numeric(10, 2), nullable=True),
        sa.Column('transfer_group', sa.String(100), nullable=True),
        sa.Column('failure_code', sa.String(50), nullable=True),
        sa.Column('failure_message', sa.Text(), nullable=True),
        sa.Column('stripe_response', postgresql.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), onupdate=sa.text('now()'), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('idx_stripe_transaction_application', 'stripe_transactions', ['application_id'])
    op.create_index('idx_stripe_transaction_type', 'stripe_transactions', ['transaction_type'])
    op.create_index('idx_stripe_transaction_status', 'stripe_transactions', ['status'])
    op.create_index('idx_stripe_transaction_payment_intent', 'stripe_transactions', ['stripe_payment_intent_id'])
    op.create_index('idx_stripe_transaction_transfer', 'stripe_transactions', ['stripe_transfer_id'])
    op.create_index('idx_stripe_transaction_group', 'stripe_transactions', ['transfer_group'])

    # Stripe webhook events table
    op.create_table(
        'stripe_webhook_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('stripe_event_id', sa.String(255), nullable=False, unique=True),
        sa.Column('event_type', sa.String(100), nullable=False),
        sa.Column('api_version', sa.String(50), nullable=True),
        sa.Column('event_data', postgresql.JSON(), nullable=False),
        sa.Column('processed', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('processing_error', sa.Text(), nullable=True),
        sa.Column('related_transaction_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('stripe_transactions.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )
    op.create_index('idx_stripe_webhook_event_id', 'stripe_webhook_events', ['stripe_event_id'])
    op.create_index('idx_stripe_webhook_type', 'stripe_webhook_events', ['event_type'])
    op.create_index('idx_stripe_webhook_processed', 'stripe_webhook_events', ['processed'])
    op.create_index('idx_stripe_webhook_created', 'stripe_webhook_events', ['created_at'])

    # Stripe payouts table
    op.create_table(
        'stripe_payouts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('stripe_account_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('stripe_accounts.id'), nullable=False),
        sa.Column('stripe_payout_id', sa.String(255), nullable=False, unique=True),
        sa.Column('amount', sa.Numeric(10, 2), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False, server_default='cad'),
        sa.Column('arrival_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.Enum('pending', 'in_transit', 'paid', 'failed', 'canceled', name='payout_status'), nullable=False, server_default='pending'),
        sa.Column('failure_code', sa.String(50), nullable=True),
        sa.Column('failure_message', sa.Text(), nullable=True),
        sa.Column('destination_bank_name', sa.String(255), nullable=True),
        sa.Column('destination_last_4', sa.String(4), nullable=True),
        sa.Column('stripe_response', postgresql.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), onupdate=sa.text('now()'), nullable=False),
    )
    op.create_index('idx_stripe_payout_account', 'stripe_payouts', ['stripe_account_id'])
    op.create_index('idx_stripe_payout_stripe_id', 'stripe_payouts', ['stripe_payout_id'])
    op.create_index('idx_stripe_payout_status', 'stripe_payouts', ['status'])

    # Add stripe_transfer_id column to funding table
    op.add_column('funding', sa.Column('stripe_transfer_id', sa.String(255), nullable=True))


def downgrade():
    return
    # Remove stripe_transfer_id from funding table
    op.drop_column('funding', 'stripe_transfer_id')

    # Drop tables
    op.drop_index('idx_stripe_payout_status', 'stripe_payouts')
    op.drop_index('idx_stripe_payout_stripe_id', 'stripe_payouts')
    op.drop_index('idx_stripe_payout_account', 'stripe_payouts')
    op.drop_table('stripe_payouts')

    op.drop_index('idx_stripe_webhook_created', 'stripe_webhook_events')
    op.drop_index('idx_stripe_webhook_processed', 'stripe_webhook_events')
    op.drop_index('idx_stripe_webhook_type', 'stripe_webhook_events')
    op.drop_index('idx_stripe_webhook_event_id', 'stripe_webhook_events')
    op.drop_table('stripe_webhook_events')

    op.drop_index('idx_stripe_transaction_group', 'stripe_transactions')
    op.drop_index('idx_stripe_transaction_transfer', 'stripe_transactions')
    op.drop_index('idx_stripe_transaction_payment_intent', 'stripe_transactions')
    op.drop_index('idx_stripe_transaction_status', 'stripe_transactions')
    op.drop_index('idx_stripe_transaction_type', 'stripe_transactions')
    op.drop_index('idx_stripe_transaction_application', 'stripe_transactions')
    op.drop_table('stripe_transactions')

    op.drop_index('idx_stripe_account_status', 'stripe_accounts')
    op.drop_index('idx_stripe_account_stripe_id', 'stripe_accounts')
    op.drop_index('idx_stripe_account_vendor', 'stripe_accounts')
    op.drop_table('stripe_accounts')

    op.drop_index('idx_payment_method_default', 'payment_methods')
    op.drop_index('idx_payment_method_status', 'payment_methods')
    op.drop_index('idx_payment_method_stripe', 'payment_methods')
    op.drop_index('idx_payment_method_borrower', 'payment_methods')
    op.drop_table('payment_methods')

    # Drop enums
    op.execute('DROP TYPE IF EXISTS payout_status CASCADE')
    op.execute('DROP TYPE IF EXISTS stripe_transaction_status CASCADE')
    op.execute('DROP TYPE IF EXISTS transaction_type CASCADE')
    op.execute('DROP TYPE IF EXISTS stripe_account_status CASCADE')
    op.execute('DROP TYPE IF EXISTS stripe_account_type CASCADE')
    op.execute('DROP TYPE IF EXISTS onboarding_status CASCADE')
    op.execute('DROP TYPE IF EXISTS payout_schedule CASCADE')
    op.execute('DROP TYPE IF EXISTS payment_method_status CASCADE')
    op.execute('DROP TYPE IF EXISTS payment_method_type CASCADE')