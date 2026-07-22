"""Granular ``loan.write_off`` permission (Dave's 2026-07-21 Originations review).

Revision ID: 075_write_off_permission
Revises: 074_staff_comments
Create Date: 2026-07-22

Dave wants write-off gated by a SPECIFIC permission rather than by the ``admin``
role, so a senior collections user can be granted it without being handed the
whole platform. ``POST /admin/loans/{id}/charge-off`` now uses
``require_permission_or_admin("loan", "write_off")``.

NO DDL. This seeds one ``permissions`` row and grants it to the ``admin`` role,
exactly as migration 072 did for the workplace grid. It is therefore purely
additive and changes no existing authorization decision:

* ``require_permission_or_admin`` allows ``admin`` implicitly, so every user who
  could request a charge-off before still can, seeded row or not.
* The grant makes ``role_permissions`` self-describing (same rationale as 060/072).
* MAKER-CHECKER is untouched: approving a pending charge-off remains admin-only,
  and it is the approver — not this permission — that actually executes it.

Idempotent static SQL (bandit B608: no interpolation, bound parameters only).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "075_write_off_permission"
down_revision = "074_staff_comments"
branch_labels = None
depends_on = None


#: Must stay in sync with ``app.services.staff_accounts.GRANULAR_PERMISSIONS``.
_PERMISSION_NAME = "loan.write_off"
_PERMISSION_DESCRIPTION = (
    "Initiate a loan write-off (charge-off). The maker-checker second approver "
    "remains admin-only."
)
_PERMISSION_RESOURCE = "loan"
_PERMISSION_ACTION = "write_off"


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            INSERT INTO permissions (id, name, description, resource, action, created_at)
            VALUES (uuid_generate_v4(), :name, :description, :resource, :action, now())
            ON CONFLICT (name) DO NOTHING
            """
        ),
        {
            "name": _PERMISSION_NAME,
            "description": _PERMISSION_DESCRIPTION,
            "resource": _PERMISSION_RESOURCE,
            "action": _PERMISSION_ACTION,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO role_permissions (id, role_id, permission_id, created_at)
            SELECT uuid_generate_v4(), r.id, p.id, now()
            FROM roles r
            JOIN permissions p ON p.name = :name
            WHERE r.name = 'admin'
            ON CONFLICT (role_id, permission_id) DO NOTHING
            """
        ),
        {"name": _PERMISSION_NAME},
    )


def downgrade() -> None:
    connection = op.get_bind()
    # Drop the grants first (role_permissions / user_permissions FK the row).
    connection.execute(
        sa.text(
            """
            DELETE FROM role_permissions
            WHERE permission_id IN (SELECT id FROM permissions WHERE name = :name)
            """
        ),
        {"name": _PERMISSION_NAME},
    )
    connection.execute(
        sa.text(
            """
            DELETE FROM user_permissions
            WHERE permission_id IN (SELECT id FROM permissions WHERE name = :name)
            """
        ),
        {"name": _PERMISSION_NAME},
    )
    connection.execute(
        sa.text("DELETE FROM permissions WHERE name = :name"),
        {"name": _PERMISSION_NAME},
    )
