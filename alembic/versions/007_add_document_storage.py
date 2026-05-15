"""add document storage

Revision ID: 007_add_document_storage
Revises: 002
Create Date: 2026-05-14 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '007_add_document_storage'
down_revision: Union[str, None] = '006_add_row_level_security'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create enum types using raw SQL (idempotent)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE document_type AS ENUM (
            'id_front', 'id_back', 'selfie', 'proof_of_income', 'bank_statement',
            'tax_return', 'utility_bill', 'business_license', 'incorporation_document',
            'shareholder_agreement', 'other'
        );
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE document_status AS ENUM ('uploading', 'uploaded', 'verified', 'rejected');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)

    # Create enum types for use in columns (with create_type=False)
    from sqlalchemy.dialects import postgresql
    document_type = postgresql.ENUM(name='document_type', create_type=False)
    document_status = postgresql.ENUM(name='document_status', create_type=False)

    # Create documents table
    op.create_table(
        'documents',
        sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v4()'), nullable=False),
        sa.Column('loan_application_id', sa.UUID(), nullable=True),
        sa.Column('borrower_id', sa.UUID(), nullable=True),
        sa.Column('vendor_id', sa.UUID(), nullable=True),
        sa.Column('document_type', document_type, nullable=False),
        sa.Column('document_subtype', sa.String(length=100), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('status', document_status, nullable=False),
        sa.Column('s3_object_key', sa.String(length=500), nullable=False),
        sa.Column('s3_bucket', sa.String(length=255), nullable=False),
        sa.Column('s3_version_id', sa.String(length=255), nullable=True),
        sa.Column('file_name', sa.String(length=255), nullable=False),
        sa.Column('file_size_bytes', sa.Integer(), nullable=True),
        sa.Column('file_content_type', sa.String(length=100), nullable=True),
        sa.Column('file_hash', sa.String(length=64), nullable=True),
        sa.Column('storage_class', sa.String(length=50), nullable=False),
        sa.Column('retention_period_days', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('uploaded_by', sa.UUID(), nullable=True),
        sa.Column('uploaded_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('verified_by', sa.UUID(), nullable=True),
        sa.Column('verified_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('verification_notes', sa.Text(), nullable=True),
        sa.Column('metadata', sa.JSON(), nullable=True),
        sa.Column('tags', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.ForeignKeyConstraint(['borrower_id'], ['borrowers.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['loan_application_id'], ['loan_applications.id']),
        sa.ForeignKeyConstraint(['uploaded_by'], ['users.id']),
        sa.ForeignKeyConstraint(['vendor_id'], ['vendors.id']),
        sa.ForeignKeyConstraint(['verified_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('s3_object_key')
    )
    op.create_index('idx_documents_loan_app', 'documents', ['loan_application_id'])
    op.create_index('idx_documents_borrower', 'documents', ['borrower_id'])
    op.create_index('idx_documents_vendor', 'documents', ['vendor_id'])
    op.create_index('idx_documents_type', 'documents', ['document_type'])
    op.create_index('idx_documents_status', 'documents', ['status'])
    op.create_index('idx_documents_s3_key', 'documents', ['s3_object_key'])
    op.create_index('idx_documents_expires', 'documents', ['expires_at'])

    # Create document_versions table
    op.create_table(
        'document_versions',
        sa.Column('id', sa.UUID(), server_default=sa.text('uuid_generate_v4()'), nullable=False),
        sa.Column('document_id', sa.UUID(), nullable=False),
        sa.Column('version_number', sa.Integer(), nullable=False),
        sa.Column('s3_object_key', sa.String(length=500), nullable=False),
        sa.Column('s3_version_id', sa.String(length=255), nullable=True),
        sa.Column('file_name', sa.String(length=255), nullable=False),
        sa.Column('file_size_bytes', sa.Integer(), nullable=True),
        sa.Column('file_content_type', sa.String(length=100), nullable=True),
        sa.Column('file_hash', sa.String(length=64), nullable=True),
        sa.Column('storage_class', sa.String(length=50), nullable=False),
        sa.Column('change_reason', sa.Text(), nullable=True),
        sa.Column('created_by', sa.UUID(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('metadata', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_document_versions_doc', 'document_versions', ['document_id'])
    op.create_index('idx_document_versions_version', 'document_versions', ['document_id', 'version_number'])

    # ========================================================================
    # ROW LEVEL SECURITY FOR DOCUMENTS
    # ========================================================================
    # RLS policies for documents are created here since the table is created here

    op.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS admin_full_access_documents ON documents")
    op.execute("""
        CREATE POLICY admin_full_access_documents ON documents
        FOR ALL
        USING (is_admin_user())
        WITH CHECK (is_admin_user());
    """)

    op.execute("DROP POLICY IF EXISTS vendor_isolation_documents ON documents")
    op.execute("""
        CREATE POLICY vendor_isolation_documents ON documents
        FOR SELECT
        USING (
            is_admin_user()
            OR vendor_id = get_current_vendor_id()
            OR (
                SELECT vendor_id FROM loan_applications WHERE id = documents.loan_application_id
            ) = get_current_vendor_id()
        );
    """)

    op.execute("DROP POLICY IF EXISTS borrower_isolation_documents ON documents")
    op.execute("""
        CREATE POLICY borrower_isolation_documents ON documents
        FOR SELECT
        USING (
            is_admin_user()
            OR borrower_id = get_current_borrower_id()
            OR (
                SELECT borrower_id FROM loan_applications WHERE id = documents.loan_application_id
            ) = get_current_borrower_id()
        );
    """)

    # Vendor can update documents for their applications
    op.execute("DROP POLICY IF EXISTS vendor_update_documents ON documents")
    op.execute("""
        CREATE POLICY vendor_update_documents ON documents
        FOR UPDATE
        USING (
            is_admin_user()
            OR vendor_id = get_current_vendor_id()
        )
        WITH CHECK (
            is_admin_user()
            OR vendor_id = get_current_vendor_id()
        );
    """)


def downgrade() -> None:
    op.drop_index('idx_document_versions_version', table_name='document_versions')
    op.drop_index('idx_document_versions_doc', table_name='document_versions')
    op.drop_table('document_versions')

    # Drop RLS policies and disable RLS
    op.execute("DROP POLICY IF EXISTS vendor_update_documents ON documents")
    op.execute("DROP POLICY IF EXISTS borrower_isolation_documents ON documents")
    op.execute("DROP POLICY IF EXISTS vendor_isolation_documents ON documents")
    op.execute("DROP POLICY IF EXISTS admin_full_access_documents ON documents")
    op.execute("ALTER TABLE documents DISABLE ROW LEVEL SECURITY")

    op.drop_index('idx_documents_expires', table_name='documents')
    op.drop_index('idx_documents_s3_key', table_name='documents')
    op.drop_index('idx_documents_status', table_name='documents')
    op.drop_index('idx_documents_type', table_name='documents')
    op.drop_index('idx_documents_vendor', table_name='documents')
    op.drop_index('idx_documents_borrower', table_name='documents')
    op.drop_index('idx_documents_loan_app', table_name='documents')
    op.drop_table('documents')

    sa.Enum(name='document_status').drop(op.get_bind())
    sa.Enum(name='document_type').drop(op.get_bind())