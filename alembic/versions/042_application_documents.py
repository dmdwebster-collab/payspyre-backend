"""KYC application documents (manual-fallback uploads)

Revision ID: 042_application_documents
Revises: 041_notification_rules
Create Date: 2026-06-26

Creates ``platform_application_documents`` — metadata rows for KYC documents
(ID front/back, selfie) uploaded against an application on the manual-fallback
path. The bytes live in object storage (DigitalOcean Spaces); this table holds
only the object key + status. See app/services/storage/document_storage.py.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "042_application_documents"
down_revision: Union[str, None] = "041_notification_rules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_application_documents",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "application_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_credit_applications.id"),
            nullable=False,
        ),
        sa.Column("doc_type", sa.String(), nullable=False),
        sa.Column("object_key", sa.String(), nullable=False),
        sa.Column("content_type", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_platform_application_documents_application_id",
        "platform_application_documents",
        ["application_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_platform_application_documents_application_id", "platform_application_documents")
    op.drop_table("platform_application_documents")
