"""Clinic memberships — map platform users to their clinic (vendor) for scoped
access to the /api/clinic/v1 API. Clinics ARE the existing ``vendors`` table
(spec §13). A user with no membership row gets 403 from the clinic API.

Revision ID: 028_clinic_memberships
Revises: 027_loan_lifecycle
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "028_clinic_memberships"
down_revision: str = "027_loan_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_clinic_memberships",
        sa.Column("id", UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("vendor_id", UUID(as_uuid=True),
                  sa.ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(length=50), nullable=False, server_default="staff"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_platform_clinic_memberships_user",
                    "platform_clinic_memberships", ["user_id"])
    op.create_index("idx_platform_clinic_memberships_vendor",
                    "platform_clinic_memberships", ["vendor_id"])
    op.create_index("idx_platform_clinic_memberships_user_vendor",
                    "platform_clinic_memberships", ["user_id", "vendor_id"], unique=True)


def downgrade() -> None:
    op.drop_index("idx_platform_clinic_memberships_user_vendor",
                  table_name="platform_clinic_memberships")
    op.drop_index("idx_platform_clinic_memberships_vendor",
                  table_name="platform_clinic_memberships")
    op.drop_index("idx_platform_clinic_memberships_user",
                  table_name="platform_clinic_memberships")
    op.drop_table("platform_clinic_memberships")
