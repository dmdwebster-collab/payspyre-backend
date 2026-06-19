"""Create platform_integration_settings table

Revision ID: 025_create_platform_integration_settings
Revises: 024_p7_5_manual_review_enum
Create Date: 2026-06-19

Part of P8.2: Platform Integration Settings (admin-managed credentials/config).

Goal (from the business owner): integrations must be configurable through a
platform settings/admin area by the team itself — rather than the developer
hardcoding API keys/creds in env files or source. This table backs that admin
surface: one row per external provider (Didit, Flinks, SendGrid, Twilio,
Zumrails, SignNow, Equifax, Google Analytics).

Columns:
- provider:  unique slug, e.g. 'sendgrid', 'flinks', 'zumrails'
- config:    JSONB, NON-secret configuration (region, sender ids, base urls,
             feature flags, etc.) — safe to return in API responses.
- secrets:   JSONB, credential VALUES (api keys, tokens, client secrets).

SECURITY — ENCRYPTION-AT-REST GAP (documented, not yet closed):
  The `secrets` column stores credential values as plaintext JSONB for now.
  This mirrors the *intended* approach for SIN on platform_patients
  (see platform_patients.sin_encrypted — "pgcrypto encrypted"): secret
  material must be encrypted at rest. The integration-settings secrets are NOT
  yet encrypted — this is a known gap to close (pgcrypto / app-layer envelope
  encryption) before real production credentials are stored. Until then:
    * the service layer NEVER returns raw secret values in API output
      (only redacted "which keys are set" indicators), and
    * secret values must never be logged or written to platform_events.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "025_create_platform_integration_settings"
down_revision: Union[str, None] = "024_p7_5_manual_review_enum"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_integration_settings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Provider slug — one row per external integration.
        # e.g. 'didit', 'flinks', 'sendgrid', 'twilio', 'zumrails',
        #      'signnow', 'equifax', 'google_analytics'
        sa.Column("provider", sa.String(), nullable=False, unique=True),

        # Non-secret configuration — safe to return in API responses.
        sa.Column(
            "config",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),

        # SECRET credential values. ENCRYPTION-AT-REST GAP: stored as plaintext
        # JSONB for now; must be encrypted at rest (mirrors the intended SIN
        # pgcrypto approach on platform_patients.sin_encrypted). NEVER returned
        # raw by the API — the service redacts to key names only.
        sa.Column(
            "secrets",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),

        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),

        # Admin user who last wrote this row (auth user id). Nullable for
        # system/seed writes.
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )

    # Lookup / upsert by provider slug (unique already enforced above).
    op.create_index(
        "idx_platform_integration_settings_provider",
        "platform_integration_settings",
        ["provider"],
        unique=True,
    )

    # keep updated_at fresh on UPDATE
    op.execute("""
        CREATE OR REPLACE FUNCTION update_platform_integration_settings_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("""
        CREATE TRIGGER platform_integration_settings_updated_at
        BEFORE UPDATE ON platform_integration_settings
        FOR EACH ROW
        EXECUTE FUNCTION update_platform_integration_settings_updated_at();
    """)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS platform_integration_settings_updated_at "
        "ON platform_integration_settings"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS update_platform_integration_settings_updated_at()"
    )
    op.drop_index(
        "idx_platform_integration_settings_provider",
        table_name="platform_integration_settings",
    )
    op.drop_table("platform_integration_settings")
