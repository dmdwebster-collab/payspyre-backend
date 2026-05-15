"""add notification tables

Revision ID: 004
Revises: 003
Create Date: 2026-05-14 18:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '004'
down_revision: Union[str, None] = '003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = '003'


def upgrade() -> None:
    # Notification templates table
    op.create_table(
        'notification_templates',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('type', sa.Enum('email', 'sms', name='notification_type'), nullable=False),
        sa.Column('category', sa.Enum('application_status', 'payment_reminder', 'statement', 'urgent', 'marketing', 'system', name='template_category'), nullable=False),
        sa.Column('subject', sa.String(length=255), nullable=True),
        sa.Column('body_template', sa.Text(), nullable=False),
        sa.Column('variables', postgresql.JSONB(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name')
    )
    op.create_index('idx_template_type', 'notification_templates', ['type'])
    op.create_index('idx_template_category', 'notification_templates', ['category'])

    # Notifications table
    op.create_table(
        'notifications',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('loan_application_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('vendor_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('webhook_url', sa.Text(), nullable=True),
        sa.Column('type', sa.Enum('email', 'sms', 'webhook', name='notification_type'), nullable=False),
        sa.Column('template_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('priority', sa.Enum('low', 'normal', 'high', 'urgent', name='notification_priority'), nullable=False, server_default='normal'),
        sa.Column('status', sa.Enum('queued', 'processing', 'sent', 'delivered', 'failed', 'retrying', name='notification_status'), nullable=False, server_default='queued'),
        sa.Column('recipient', sa.String(length=255), nullable=False),
        sa.Column('subject', sa.String(length=255), nullable=True),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('variables', postgresql.JSONB(), nullable=True),
        sa.Column('scheduled_for', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['loan_application_id'], ['loan_applications.id'], ),
        sa.ForeignKeyConstraint(['template_id'], ['notification_templates.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['vendor_id'], ['vendors.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_notification_user', 'notifications', ['user_id'])
    op.create_index('idx_notification_status', 'notifications', ['status'])
    op.create_index('idx_notification_priority', 'notifications', ['priority'])
    op.create_index('idx_notification_type', 'notifications', ['type'])
    op.create_index('idx_notification_scheduled', 'notifications', ['scheduled_for'])
    op.create_index('idx_notification_loan_app', 'notifications', ['loan_application_id'])
    op.create_index('idx_notification_vendor', 'notifications', ['vendor_id'])

    # Deliveries table
    op.create_table(
        'deliveries',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('notification_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('attempt_number', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('status', sa.Enum('pending', 'sent', 'delivered', 'failed', name='delivery_status'), nullable=False, server_default='pending'),
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('provider_message_id', sa.String(length=255), nullable=True),
        sa.Column('response', postgresql.JSONB(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['notification_id'], ['notifications.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_delivery_notification', 'deliveries', ['notification_id'])
    op.create_index('idx_delivery_status', 'deliveries', ['status'])

    # Notification preferences table
    op.create_table(
        'notification_preferences',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('vendor_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('email_enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('sms_enabled', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('application_status_enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('payment_reminders_enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('statements_enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('urgent_notifications_enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('marketing_enabled', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('daily_digest_enabled', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('quiet_hours_start', sa.String(length=5), nullable=True),
        sa.Column('quiet_hours_end', sa.String(length=5), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['vendor_id'], ['vendors.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_notification_prefs_user', 'notification_preferences', ['user_id'])
    op.create_index('idx_notification_prefs_vendor', 'notification_preferences', ['vendor_id'])

    # Webhook deliveries table
    op.create_table(
        'webhook_deliveries',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('vendor_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('event_type', sa.String(length=100), nullable=False),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('payload', postgresql.JSONB(), nullable=False),
        sa.Column('status', sa.Enum('pending', 'sent', 'failed', name='webhook_status'), nullable=False, server_default='pending'),
        sa.Column('response_code', sa.Integer(), nullable=True),
        sa.Column('response_body', sa.Text(), nullable=True),
        sa.Column('attempt_number', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('retry_after', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['vendor_id'], ['vendors.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_webhook_vendor', 'webhook_deliveries', ['vendor_id'])
    op.create_index('idx_webhook_event', 'webhook_deliveries', ['event_type'])
    op.create_index('idx_webhook_status', 'webhook_deliveries', ['status'])


def downgrade() -> None:
    op.drop_index('idx_webhook_status', table_name='webhook_deliveries')
    op.drop_index('idx_webhook_event', table_name='webhook_deliveries')
    op.drop_index('idx_webhook_vendor', table_name='webhook_deliveries')
    op.drop_table('webhook_deliveries')

    op.drop_index('idx_notification_prefs_vendor', table_name='notification_preferences')
    op.drop_index('idx_notification_prefs_user', table_name='notification_preferences')
    op.drop_table('notification_preferences')

    op.drop_index('idx_delivery_status', table_name='deliveries')
    op.drop_index('idx_delivery_notification', table_name='deliveries')
    op.drop_table('deliveries')

    op.drop_index('idx_notification_vendor', table_name='notifications')
    op.drop_index('idx_notification_loan_app', table_name='notifications')
    op.drop_index('idx_notification_scheduled', table_name='notifications')
    op.drop_index('idx_notification_type', table_name='notifications')
    op.drop_index('idx_notification_priority', table_name='notifications')
    op.drop_index('idx_notification_status', table_name='notifications')
    op.drop_index('idx_notification_user', table_name='notifications')
    op.drop_table('notifications')

    op.drop_index('idx_template_category', table_name='notification_templates')
    op.drop_index('idx_template_type', table_name='notification_templates')
    op.drop_table('notification_templates')