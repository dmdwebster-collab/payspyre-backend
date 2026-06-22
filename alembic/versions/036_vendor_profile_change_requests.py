"""Create platform_vendor_profile_change_requests (spec §3.6)

Revision ID: 036_vendor_profile_change_requests
Revises: 035_turnkey_migration_fields
Create Date: 2026-06-22

Phase 4 (account self-service). Vendors CANNOT edit their profile directly — the
profile is read-only. Any requested change is persisted here as a PENDING,
admin-routed change request that does NOT mutate the ``vendors`` row; an admin
approves/rejects it out-of-band (separate admin surface).

``requested_changes`` is JSONB holding ONLY whitelisted vendor-editable keys
(contact_name, email, phone, address_line1, address_line2, city, province,
postal_code). The whitelist is enforced at the API layer.

Additive only. Downgrade drops the table then the enum type.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "036_vendor_profile_change_requests"
down_revision: Union[str, None] = "035_turnkey_migration_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    status_enum = postgresql.ENUM(
        "pending",
        "approved",
        "rejected",
        name="platform_vendor_profile_change_status",
    )
    status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "platform_vendor_profile_change_requests",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "vendor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vendors.id"),
            nullable=False,
        ),
        sa.Column(
            "requested_changes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending",
                "approved",
                "rejected",
                name="platform_vendor_profile_change_status",
                create_type=False,
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("note", sa.Text(), nullable=True),
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
    # Admin queue + vendor-scoped lookups filter on vendor_id (+ status).
    op.create_index(
        "idx_vendor_profile_change_requests_vendor",
        "platform_vendor_profile_change_requests",
        ["vendor_id"],
    )
    op.create_index(
        "idx_vendor_profile_change_requests_status",
        "platform_vendor_profile_change_requests",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_vendor_profile_change_requests_status",
        table_name="platform_vendor_profile_change_requests",
    )
    op.drop_index(
        "idx_vendor_profile_change_requests_vendor",
        table_name="platform_vendor_profile_change_requests",
    )
    op.drop_table("platform_vendor_profile_change_requests")
    postgresql.ENUM(
        name="platform_vendor_profile_change_status"
    ).drop(op.get_bind(), checkfirst=True)
