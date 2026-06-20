"""Create marketplace lead-engine tables (spec §2.8, §2.9)

Revision ID: 031_marketplace
Revises: 030_sin_column_reconcile
Create Date: 2026-06-20

Part of Phase C (Marketplace). Adds the two-sided lead-engine tables:

  - platform_marketplace_listings        — a patient's opt-in financing listing.
  - platform_marketplace_vendor_interest — a vendor's interest + lead charge.

``treatment_categories`` is a native Postgres TEXT[]. status / urgency /
charge_trigger are plain TEXT (the value sets are small + documented on the
model, and we deliberately avoid new enum types so the lead-state / urgency
vocabularies can evolve via config without a migration).

Downgrade drops children first, then the parent. No enum types are created here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "031_marketplace"
down_revision: Union[str, None] = "030_sin_column_reconcile"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ===========================================================================
    # platform_marketplace_listings
    # ===========================================================================
    op.create_table(
        "platform_marketplace_listings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "patient_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_patients.id"),
            nullable=False,
        ),
        # 'active' | 'paused' | 'closed' | 'expired'
        sa.Column("status", sa.String(), nullable=False, server_default="active"),

        # What the patient needs.
        sa.Column("treatment_categories", postgresql.ARRAY(sa.String()), nullable=False),
        # 'immediate' | 'this_week' | 'this_month' | 'flexible'
        sa.Column("treatment_urgency", sa.String(), nullable=False),
        sa.Column("estimated_budget_cents", sa.BigInteger(), nullable=True),
        sa.Column("location_postal_code", sa.String(), nullable=False),
        sa.Column("max_travel_km", sa.Integer(), nullable=False, server_default=sa.text("25")),

        # Denormalized lead enrichment (snapshot from platform_patients).
        sa.Column("lead_state", sa.String(), nullable=False),
        sa.Column("verification_depth", sa.String(), nullable=False),

        # Marketplace economics (pricing engine fills base price at create).
        sa.Column("base_lead_price_cents", sa.Integer(), nullable=False),
        sa.Column("accepted_vendor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),

        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_marketplace_listings_patient", "platform_marketplace_listings", ["patient_id"]
    )
    # Vendor lead-discovery queries filter on status + lead_state.
    op.create_index(
        "idx_marketplace_listings_status_lead_state",
        "platform_marketplace_listings",
        ["status", "lead_state"],
    )

    # ===========================================================================
    # platform_marketplace_vendor_interest
    # ===========================================================================
    op.create_table(
        "platform_marketplace_vendor_interest",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "listing_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_marketplace_listings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=False),

        sa.Column("expressed_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("selected_by_patient_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("appointment_booked_at", sa.DateTime(timezone=True), nullable=True),

        sa.Column("lead_charge_cents", sa.Integer(), nullable=True),
        sa.Column("lead_charged_at", sa.DateTime(timezone=True), nullable=True),
        # 'expressed_interest' | 'patient_selected' | 'appointment_booked' | 'treatment_completed'
        sa.Column("charge_trigger", sa.String(), nullable=True),
    )
    op.create_index(
        "idx_marketplace_interest_listing", "platform_marketplace_vendor_interest", ["listing_id"]
    )
    op.create_index(
        "idx_marketplace_interest_vendor", "platform_marketplace_vendor_interest", ["vendor_id"]
    )
    # One interest row per (vendor, listing) — express_interest is idempotent.
    op.create_index(
        "idx_marketplace_interest_vendor_listing",
        "platform_marketplace_vendor_interest",
        ["vendor_id", "listing_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("idx_marketplace_interest_vendor_listing", table_name="platform_marketplace_vendor_interest")
    op.drop_index("idx_marketplace_interest_vendor", table_name="platform_marketplace_vendor_interest")
    op.drop_index("idx_marketplace_interest_listing", table_name="platform_marketplace_vendor_interest")
    op.drop_table("platform_marketplace_vendor_interest")

    op.drop_index("idx_marketplace_listings_status_lead_state", table_name="platform_marketplace_listings")
    op.drop_index("idx_marketplace_listings_patient", table_name="platform_marketplace_listings")
    op.drop_table("platform_marketplace_listings")
