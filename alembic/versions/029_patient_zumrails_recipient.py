"""Add platform_patients.zumrails_recipient_id — the borrower's payee id at the
payments provider (Zumrails), read by loan_lifecycle to disburse a funded loan.

Revision ID: 029_patient_zumrails_recipient
Revises: 028_clinic_memberships
"""
from alembic import op
import sqlalchemy as sa

revision: str = "029_patient_zumrails_recipient"
down_revision: str = "028_clinic_memberships"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_patients",
        sa.Column("zumrails_recipient_id", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("platform_patients", "zumrails_recipient_id")
