"""Create platform_credit_products table

Revision ID: 017_create_platform_credit_products
Revises: 016_create_platform_patient_fields
Create Date: 2026-05-21

Part of PR P1: Platform Foundation Migrations

This migration creates the platform_credit_products table, which stores the
configurable credit product. This is the table Dave's toggle matrix lives in.

Key Features:
- Credit product configuration with verification_matrix JSONB
- Per-amount-bracket verification requirements (Dave's toggle config)
- Decision ruleset references to YAML rule files
- Pricing configuration (term options, APR range, etc.)
- Funding model configuration
- Version tracking for product snapshot on application creation

Security Notes:
- verification_matrix validated against JSON Schema on write (service layer)
- Decision ruleset references only; actual rules in YAML config files
- Product version snapshot on application creation prevents mid-flow changes

Hard Rules:
- All thresholds (verification matrix, decision rules) from YAML/DB config
- Never hardcoded in service code
- Product version changes don't affect in-flight applications

Open Questions Referenced:
- Q6 (funding source): payspyre_capital vs partner_lender vs hybrid — config only
- Q10 (co-applicant default combination_strategy): average per spec, configurable
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '017_create_platform_credit_products'
down_revision: Union[str, None] = '016_create_platform_patient_fields'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ===========================================================================
    # CREATE NEW ENUM TYPES
    # ===========================================================================

    # platform_credit_products.status enum
    op.execute("""
        CREATE TYPE platform_credit_product_status AS ENUM (
            'draft',
            'active',
            'archived'
        )
    """)

    # platform_credit_products.vertical enum
    op.execute("""
        CREATE TYPE platform_vertical AS ENUM (
            'dental',
            'auto',
            'veterinary'
        )
    """)

    # platform_credit_products.funding_source enum
    op.execute("""
        CREATE TYPE platform_funding_source AS ENUM (
            'payspyre_capital',
            'partner_lender',
            'hybrid',
            'clinic_self'
        )
    """)

    # Create enum types for use in columns
    status_enum = postgresql.ENUM(name='platform_credit_product_status', create_type=False)
    vertical_enum = postgresql.ENUM(name='platform_vertical', create_type=False)
    funding_source_enum = postgresql.ENUM(name='platform_funding_source', create_type=False)

    # ===========================================================================
    # CREATE platform_credit_products TABLE
    # ===========================================================================

    op.create_table(
        'platform_credit_products',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('code', sa.String(), nullable=False, unique=True),
        # 'dental_small_v1', 'dental_full_arch_v1', 'auto_dealer_v1'
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('vertical', vertical_enum, nullable=False),
        sa.Column('status', status_enum, nullable=False, server_default='draft'),

        sa.Column('min_amount_cents', sa.BigInteger(), nullable=False),
        sa.Column('max_amount_cents', sa.BigInteger(), nullable=False),
        sa.Column('currency', sa.String(), nullable=False, server_default='CAD'),

        # The verification matrix: per amount bracket, which verifications run
        # Stored as JSONB for flexibility; validated against a JSON Schema on write
        sa.Column('verification_matrix', postgresql.JSONB(), nullable=False),

        # Decision rules — references to YAML rule files in /config/decision_rules/
        sa.Column('decision_ruleset', sa.String(), nullable=False),
        # e.g. 'dental_small_v1.yaml'

        # Pricing
        sa.Column('pricing_config', postgresql.JSONB(), nullable=False),
        # {term_options: [12, 24, 36], apr_range: [9.99, 29.99], ...}

        # Funding model
        sa.Column('funding_source', funding_source_enum, nullable=False),

        sa.Column('created_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
    )

    # ===========================================================================
    # CREATE INDEXES
    # ===========================================================================

    # Lookup by code (unique already enforced)
    op.create_index('idx_platform_credit_products_code', 'platform_credit_products', ['code'])

    # Filter by vertical and status
    op.create_index('idx_platform_credit_products_vertical_status', 'platform_credit_products', ['vertical', 'status'])

    # ===========================================================================
    # CREATE TRIGGER FOR UPDATED_AT
    # ===========================================================================

    op.execute("""
        CREATE OR REPLACE FUNCTION update_platform_credit_products_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("""
        CREATE TRIGGER platform_credit_products_updated_at
        BEFORE UPDATE ON platform_credit_products
        FOR EACH ROW
        EXECUTE FUNCTION update_platform_credit_products_updated_at();
    """)


def downgrade() -> None:
    # Drop trigger and function
    op.execute("DROP TRIGGER IF EXISTS platform_credit_products_updated_at ON platform_credit_products")
    op.execute("DROP FUNCTION IF EXISTS update_platform_credit_products_updated_at()")

    # Drop indexes
    op.drop_index('idx_platform_credit_products_vertical_status', table_name='platform_credit_products')
    op.drop_index('idx_platform_credit_products_code', table_name='platform_credit_products')

    # Drop table
    op.drop_table('platform_credit_products')

    # Drop enum types
    op.execute("DROP TYPE IF EXISTS platform_funding_source")
    op.execute("DROP TYPE IF EXISTS platform_vertical")
    op.execute("DROP TYPE IF EXISTS platform_credit_product_status")
