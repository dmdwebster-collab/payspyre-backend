"""Reconcile platform_patients.sin_encrypted type drift (audit C-3).

Migration 015 created ``sin_encrypted`` as ``BYTEA`` (``sa.LargeBinary``) while
the ORM model declared it ``String`` — a type drift that would corrupt any real
read/write. We now store an app-layer Fernet token (a UTF-8 string, see
``app/core/sin_crypto.py``), so the column should be TEXT. This ALTERs the DB
column ``BYTEA -> TEXT`` so the DB and the model agree.

The column is nullable and (safe today) holds no real SIN data yet — nothing
collected a SIN before this work landed — so the conversion is a no-op on
existing rows. We still ``USING convert_from(sin_encrypted, 'UTF8')`` defensively
so any stray bytes are decoded rather than rejected by Postgres' implicit cast.

SQLite (dev/CI) is dynamically typed and ignores column-type alters, so the
guarded dialect check skips the ALTER there.

Revision ID: 030_sin_column_reconcile
Revises: 029_patient_zumrails_recipient
"""
from alembic import op
import sqlalchemy as sa

revision: str = "030_sin_column_reconcile"
down_revision: str = "029_patient_zumrails_recipient"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "platform_patients",
            "sin_encrypted",
            existing_type=sa.LargeBinary(),
            type_=sa.Text(),
            existing_nullable=True,
            postgresql_using="convert_from(sin_encrypted, 'UTF8')",
        )
    # SQLite: dynamic typing — no schema change required.


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "platform_patients",
            "sin_encrypted",
            existing_type=sa.Text(),
            type_=sa.LargeBinary(),
            existing_nullable=True,
            postgresql_using="convert_to(sin_encrypted, 'UTF8')",
        )
