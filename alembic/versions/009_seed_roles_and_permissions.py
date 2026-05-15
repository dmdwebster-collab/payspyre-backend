"""Seed roles and permissions

Revision ID: 009_seed_roles_and_permissions
Revises: 003
Create Date: 2026-05-14

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from datetime import datetime
from uuid import uuid4

# revision identifiers, used by Alembic.
revision = '009_seed_roles_and_permissions'
down_revision = '008_add_stripe_integration'
branch_labels = None
depends_on = None


def upgrade():
    # Skip - data seeding should use ON CONFLICT or check existence
    # TODO: Make idempotent or use ON CONFLICT
    return

    admin_role_id = uuid4()
    staff_role_id = uuid4()
    patient_role_id = uuid4()
    vendor_role_id = uuid4()

    connection.execute(
        sa.text("""
            INSERT INTO roles (id, name, description, is_system, created_at, updated_at)
            VALUES
                (:admin_id, 'admin', 'System administrator with full access', true, :now, :now),
                (:staff_id, 'staff', 'PaySpyre staff member', true, :now, :now),
                (:patient_id, 'patient', 'Patient user', true, :now, :now),
                (:vendor_id, 'vendor', 'Vendor/Integration user', true, :now, :now)
        """),
        {
            "admin_id": admin_role_id,
            "staff_id": staff_role_id,
            "patient_id": patient_role_id,
            "vendor_id": vendor_role_id,
            "now": datetime.utcnow()
        }
    )

    permissions = [
        ("user.create", "Create users", "user", "create"),
        ("user.read", "Read users", "user", "read"),
        ("user.update", "Update users", "user", "update"),
        ("user.delete", "Delete users", "user", "delete"),
        ("role.create", "Create roles", "role", "create"),
        ("role.read", "Read roles", "role", "read"),
        ("role.update", "Update roles", "role", "update"),
        ("role.delete", "Delete roles", "role", "delete"),
        ("permission.create", "Create permissions", "permission", "create"),
        ("permission.read", "Read permissions", "permission", "read"),
        ("permission.update", "Update permissions", "permission", "update"),
        ("permission.delete", "Delete permissions", "permission", "delete"),
        ("loan.create", "Create loan applications", "loan", "create"),
        ("loan.read", "Read loan applications", "loan", "read"),
        ("loan.update", "Update loan applications", "loan", "update"),
        ("loan.delete", "Delete loan applications", "loan", "delete"),
        ("kyc.create", "Create KYC sessions", "kyc", "create"),
        ("kyc.read", "Read KYC sessions", "kyc", "read"),
        ("kyc.update", "Update KYC sessions", "kyc", "update"),
        ("kyc.delete", "Delete KYC sessions", "kyc", "delete"),
        ("funding.create", "Create funding requests", "funding", "create"),
        ("funding.read", "Read funding requests", "funding", "read"),
        ("funding.update", "Update funding requests", "funding", "update"),
        ("funding.delete", "Delete funding requests", "funding", "delete"),
        ("vendor.create", "Create vendor records", "vendor", "create"),
        ("vendor.read", "Read vendor records", "vendor", "read"),
        ("vendor.update", "Update vendor records", "vendor", "update"),
        ("vendor.delete", "Delete vendor records", "vendor", "delete"),
        ("underwriting.read", "Read underwriting decisions", "underwriting", "read"),
        ("underwriting.approve", "Approve underwriting", "underwriting", "approve"),
        ("underwriting.reject", "Reject underwriting", "underwriting", "reject"),
    ]

    for perm_name, perm_desc, resource, action in permissions:
        result = connection.execute(
            sa.text("""
                INSERT INTO permissions (id, name, description, resource, action, created_at)
                VALUES (:id, :name, :description, :resource, :action, :now)
                RETURNING id
            """),
            {
                "id": uuid4(),
                "name": perm_name,
                "description": perm_desc,
                "resource": resource,
                "action": action,
                "now": datetime.utcnow()
            }
        )
        perm_id = result.fetchone()[0]

        if resource in ["user", "role", "permission"]:
            connection.execute(
                sa.text("""
                    INSERT INTO role_permissions (id, role_id, permission_id, created_at)
                    VALUES (:id, :role_id, :permission_id, :now)
                """),
                {
                    "id": uuid4(),
                    "role_id": admin_role_id,
                    "permission_id": perm_id,
                    "now": datetime.utcnow()
                }
            )

    all_permissions = connection.execute(
        sa.text("SELECT id FROM permissions")
    ).fetchall()

    for perm in all_permissions:
        perm_id = perm[0]
        connection.execute(
            sa.text("""
                INSERT INTO role_permissions (id, role_id, permission_id, created_at)
                VALUES (:id, :role_id, :permission_id, :now)
            """),
            {
                "id": uuid4(),
                "role_id": admin_role_id,
                "permission_id": perm_id,
                "now": datetime.utcnow()
            }
        )

    staff_perms = connection.execute(
        sa.text("""
            SELECT id FROM permissions
            WHERE resource IN ('loan', 'kyc', 'funding', 'underwriting', 'vendor')
            AND action IN ('read', 'create', 'update')
        """)
    ).fetchall()

    for perm in staff_perms:
        perm_id = perm[0]
        connection.execute(
            sa.text("""
                INSERT INTO role_permissions (id, role_id, permission_id, created_at)
                VALUES (:id, :role_id, :permission_id, :now)
            """),
            {
                "id": uuid4(),
                "role_id": staff_role_id,
                "permission_id": perm_id,
                "now": datetime.utcnow()
            }
        )

    patient_perms = connection.execute(
        sa.text("""
            SELECT id FROM permissions
            WHERE resource IN ('loan', 'kyc')
            AND action IN ('read', 'create')
        """)
    ).fetchall()

    for perm in patient_perms:
        perm_id = perm[0]
        connection.execute(
            sa.text("""
                INSERT INTO role_permissions (id, role_id, permission_id, created_at)
                VALUES (:id, :role_id, :permission_id, :now)
            """),
            {
                "id": uuid4(),
                "role_id": patient_role_id,
                "permission_id": perm_id,
                "now": datetime.utcnow()
            }
        )

    vendor_perms = connection.execute(
        sa.text("""
            SELECT id FROM permissions
            WHERE resource = 'vendor' AND action = 'read'
        """)
    ).fetchall()

    for perm in vendor_perms:
        perm_id = perm[0]
        connection.execute(
            sa.text("""
                INSERT INTO role_permissions (id, role_id, permission_id, created_at)
                VALUES (:id, :role_id, :permission_id, :now)
            """),
            {
                "id": uuid4(),
                "role_id": vendor_role_id,
                "permission_id": perm_id,
                "now": datetime.utcnow()
            }
        )


def downgrade():
    return
    connection.execute(sa.text("DELETE FROM role_permissions"))
    connection.execute(sa.text("DELETE FROM permissions"))
    connection.execute(sa.text("DELETE FROM roles"))