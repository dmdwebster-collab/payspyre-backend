"""Settings backend gaps: per-user permission grants + the workplace permission grid.

Revision ID: 072_settings_backend_gaps
Revises: 071_customer_profile
Create Date: 2026-07-22

Unblocks three Settings leaves the frontend correctly refused to build as
decorative editors (frontend PR #66's ``SETTINGS_BACKEND_GAPS``).

Only **Gap 1 (Accounts → Users)** needs DDL. The other two gaps are schema-only:

* Gap 2 (product config depth) extends the typed documents already stored in
  ``platform_credit_products.pricing_config`` / ``policy_config`` (JSONB) —
  ``policy_config`` is nullable and readers fall back to defaults, so no data
  migration is required or wanted. ``platform_credit_products.provinces`` was
  verified to ALREADY EXIST (added by the province-compliance work) and is
  already writable through the product create/PATCH API; nothing is added here.
* Gap 3 (integrations depth) types the existing free-form
  ``platform_integration_settings.config`` JSONB. No column change.

This migration therefore:

1. Creates ``user_permissions`` — permissions granted DIRECTLY to a user,
   alongside the existing role-derived grants. Purely additive: authorization
   consults roles first, then this table, and it starts empty, so no existing
   authorization decision changes.
2. Seeds the 19 workplace permissions from Dave's `Add user` grid
   (docs/turnkey_parity/rewatch_2026-07-21/07-08_settings.md §A1.1) as
   ``permissions`` rows, and grants them all to the ``admin`` role (admins are
   implicitly allowed everywhere; the grants make the role self-describing).
   Idempotent static SQL, mirroring the conventions of 009 / 060 / 063.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "072_settings_backend_gaps"
down_revision = "071_customer_profile"
branch_labels = None
depends_on = None


#: (name, description, resource, action) — the 19 checkboxes of the TL
#: `Add user` permission grid. Must stay in sync with
#: ``app.services.staff_accounts.WORKPLACE_PERMISSIONS``.
_WORKPLACE_PERMISSIONS = [
    ("origination.access", "Loan origination workplace", "origination", "access"),
    ("collection.access", "Collection workplace", "collection", "access"),
    ("risk_evaluation.access", "Risk Evaluation workplace", "risk_evaluation", "access"),
    ("underwriting.access", "Underwriting workplace", "underwriting", "access"),
    ("reports.access", "Reports", "reports", "access"),
    ("system_administration.access", "System administration", "system_administration", "access"),
    ("customer_management.access", "Customer management", "customer_management", "access"),
    ("archive.access", "Archive", "archive", "access"),
    ("loan_servicing.access", "Loan servicing", "loan_servicing", "access"),
    ("blacklist.access", "Blacklist management", "blacklist", "access"),
    (
        "repayment_transaction.edit_reverse",
        "Edit/Reverse repayment transaction (works only alongside another role)",
        "repayment_transaction",
        "edit_reverse",
    ),
    ("import_transactions.access", "Import transactions", "import_transactions", "access"),
    ("export.access", "Export data", "export", "access"),
    (
        "import_loans_customers.access",
        "Import loans and customers",
        "import_loans_customers",
        "access",
    ),
    ("loan_migration.access", "Loan migration", "loan_migration", "access"),
    (
        "assignment_officer.access",
        "Assignment officer (works only alongside another role)",
        "assignment_officer",
        "access",
    ),
    (
        "branch_management.access",
        "Branch management (works only alongside another role)",
        "branch_management",
        "access",
    ),
    (
        "document_verification.access",
        "Document verification (works only alongside another role)",
        "document_verification",
        "access",
    ),
    ("vendor_management.access", "Vendor management", "vendor_management", "access"),
]

_PERMISSION_NAMES = tuple(p[0] for p in _WORKPLACE_PERMISSIONS)


def upgrade() -> None:
    # ---------------------------------------------------------------- 1. DDL
    op.create_table(
        "user_permissions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "permission_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("permissions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "granted_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("idx_user_permissions_user", "user_permissions", ["user_id"])
    op.create_index("idx_user_permissions_permission", "user_permissions", ["permission_id"])
    op.create_index(
        "idx_user_permissions_unique",
        "user_permissions",
        ["user_id", "permission_id"],
        unique=True,
    )

    # ------------------------------------------------- 2. Seed the grid rows
    connection = op.get_bind()
    for name, description, resource, action in _WORKPLACE_PERMISSIONS:
        connection.execute(
            sa.text(
                """
                INSERT INTO permissions (id, name, description, resource, action, created_at)
                VALUES (uuid_generate_v4(), :name, :description, :resource, :action, now())
                ON CONFLICT (name) DO NOTHING
                """
            ),
            {"name": name, "description": description, "resource": resource, "action": action},
        )

    # Grant every workplace permission to the admin role. Admin is implicitly
    # allowed by require_permission_or_admin already; this makes the
    # role→permission table self-describing (same rationale as migration 060).
    connection.execute(
        sa.text(
            """
            INSERT INTO role_permissions (id, role_id, permission_id, created_at)
            SELECT uuid_generate_v4(), r.id, p.id, now()
            FROM roles r
            JOIN permissions p ON p.name = ANY(:names)
            WHERE r.name = 'admin'
            ON CONFLICT (role_id, permission_id) DO NOTHING
            """
        ),
        {"names": list(_PERMISSION_NAMES)},
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            DELETE FROM role_permissions rp
            USING permissions p
            WHERE rp.permission_id = p.id AND p.name = ANY(:names)
            """
        ),
        {"names": list(_PERMISSION_NAMES)},
    )
    connection.execute(
        sa.text("DELETE FROM permissions WHERE name = ANY(:names)"),
        {"names": list(_PERMISSION_NAMES)},
    )
    op.drop_index("idx_user_permissions_unique", table_name="user_permissions")
    op.drop_index("idx_user_permissions_permission", table_name="user_permissions")
    op.drop_index("idx_user_permissions_user", table_name="user_permissions")
    op.drop_table("user_permissions")
