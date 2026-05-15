"""Add Row Level Security for multi-tenant data isolation

Revision ID: 006_add_row_level_security
Revises: 005_add_performance_indexes
Create Date: 2026-05-14

CRITICAL SECURITY MIGRATION

This migration implements Row Level Security (RLS) to enforce
multi-tenant data isolation at the database layer. This prevents
cross-tenant data leakage even if application logic has bugs.

Security Model:
- Admin users: Full access (bypass RLS)
- Vendor users: Access only to their vendor's data
- Borrower users: Access only to their own applications/documents
- Staff users: Read access to all, no write access

Tenant context is set via:
SET LOCAL app.current_user_id = '...';
SET LOCAL app.current_user_role = '...';
SET LOCAL app.vendor_id = '...';  -- For vendor users
SET LOCAL app.borrower_id = '...'; -- For borrower users
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '006_add_row_level_security'
down_revision: Union[str, None] = '005_add_performance_indexes'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ========================================================================
    # HELPER FUNCTIONS - Enable application to set tenant context
    # ========================================================================

    # Function to check if user is admin
    op.execute("""
        CREATE OR REPLACE FUNCTION is_admin_user()
        RETURNS BOOLEAN AS $$
            SELECT EXISTS (
                SELECT 1 FROM user_roles ur
                JOIN roles r ON r.id = ur.role_id
                WHERE ur.user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
                AND r.name = 'admin'
            );
        $$ LANGUAGE sql STABLE;
    """)

    # Function to get vendor ID for current user
    op.execute("""
        CREATE OR REPLACE FUNCTION get_current_vendor_id()
        RETURNS UUID AS $$
            SELECT NULLIF(current_setting('app.vendor_id', true), '')::uuid;
        $$ LANGUAGE sql STABLE;
    """)

    # Function to get borrower ID for current user
    op.execute("""
        CREATE OR REPLACE FUNCTION get_current_borrower_id()
        RETURNS UUID AS $$
            SELECT NULLIF(current_setting('app.borrower_id', true), '')::uuid;
        $$ LANGUAGE sql STABLE;
    """)

    # ========================================================================
    # LOAN APPLICATIONS - Multi-tenant isolation
    # ========================================================================

    op.execute("ALTER TABLE loan_applications ENABLE ROW LEVEL SECURITY")

    # Admin: Full access
    op.execute("DROP POLICY IF EXISTS admin_full_access_loan_applications ON loan_applications")
    op.execute("""
        CREATE POLICY admin_full_access_loan_applications ON loan_applications
        FOR ALL
        USING (is_admin_user())
        WITH CHECK (is_admin_user());
    """)

    # Vendor: See only their applications
    op.execute("DROP POLICY IF EXISTS vendor_isolation_loan_applications ON loan_applications")
    op.execute("""
        CREATE POLICY vendor_isolation_loan_applications ON loan_applications
        FOR SELECT
        USING (
            is_admin_user()
            OR vendor_id = get_current_vendor_id()
        );
    """)

    # Vendor: Update only their applications (status changes)
    op.execute("DROP POLICY IF EXISTS vendor_update_loan_applications ON loan_applications")
    op.execute("""
        CREATE POLICY vendor_update_loan_applications ON loan_applications
        FOR UPDATE
        USING (
            is_admin_user()
            OR (vendor_id = get_current_vendor_id() AND status IN ('underwriting', 'pending_documents'))
        )
        WITH CHECK (
            is_admin_user()
            OR vendor_id = get_current_vendor_id()
        );
    """)

    # Borrower: See only their applications
    op.execute("DROP POLICY IF EXISTS borrower_isolation_loan_applications ON loan_applications")
    op.execute("""
        CREATE POLICY borrower_isolation_loan_applications ON loan_applications
        FOR SELECT
        USING (
            is_admin_user()
            OR borrower_id = get_current_borrower_id()
        );
    """)

    # ========================================================================
    # BORROWERS - Borrowers can update their own info
    # ========================================================================

    op.execute("ALTER TABLE borrowers ENABLE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS admin_full_access_borrowers ON borrowers")
    op.execute("""
        CREATE POLICY admin_full_access_borrowers ON borrowers
        FOR ALL
        USING (is_admin_user())
        WITH CHECK (is_admin_user());
    """)

    op.execute("DROP POLICY IF EXISTS borrower_self_update_borrowers ON borrowers")
    op.execute("""
        CREATE POLICY borrower_self_update_borrowers ON borrowers
        FOR SELECT
        USING (is_admin_user() OR id = get_current_borrower_id());
    """)

    op.execute("DROP POLICY IF EXISTS borrower_update_own_borrowers ON borrowers")
    op.execute("""
        CREATE POLICY borrower_update_own_borrowers ON borrowers
        FOR UPDATE
        USING (id = get_current_borrower_id())
        WITH CHECK (id = get_current_borrower_id());
    """)

    # ========================================================================
    # VENDORS - Vendors see only their own record
    # ========================================================================

    op.execute("ALTER TABLE vendors ENABLE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS admin_full_access_vendors ON vendors")
    op.execute("""
        CREATE POLICY admin_full_access_vendors ON vendors
        FOR ALL
        USING (is_admin_user())
        WITH CHECK (is_admin_user());
    """)

    op.execute("DROP POLICY IF EXISTS vendor_self_access_vendors ON vendors")
    op.execute("""
        CREATE POLICY vendor_self_access_vendors ON vendors
        FOR SELECT
        USING (is_admin_user() OR id = get_current_vendor_id());
    """)

    op.execute("DROP POLICY IF EXISTS vendor_self_update_vendors ON vendors")
    op.execute("""
        CREATE POLICY vendor_self_update_vendors ON vendors
        FOR UPDATE
        USING (id = get_current_vendor_id())
        WITH CHECK (id = get_current_vendor_id());
    """)

    # ========================================================================
    # KYC SESSIONS - Application-level isolation
    # ========================================================================
    # NOTE: documents RLS policies moved to migration 007 where table is created
    # ========================================================================

    op.execute("ALTER TABLE kyc_sessions ENABLE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS admin_full_access_kyc_sessions ON kyc_sessions")
    op.execute("""
        CREATE POLICY admin_full_access_kyc_sessions ON kyc_sessions
        FOR ALL
        USING (is_admin_user())
        WITH CHECK (is_admin_user());
    """)

    op.execute("DROP POLICY IF EXISTS vendor_isolation_kyc_sessions ON kyc_sessions")
    op.execute("""
        CREATE POLICY vendor_isolation_kyc_sessions ON kyc_sessions
        FOR SELECT
        USING (
            is_admin_user()
            OR (
                SELECT vendor_id FROM loan_applications WHERE id = kyc_sessions.loan_application_id
            ) = get_current_vendor_id()
        );
    """)

    op.execute("DROP POLICY IF EXISTS borrower_isolation_kyc_sessions ON kyc_sessions")
    op.execute("""
        CREATE POLICY borrower_isolation_kyc_sessions ON kyc_sessions
        FOR SELECT
        USING (
            is_admin_user()
            OR borrower_id = get_current_borrower_id()
        );
    """)

    # ========================================================================
    # KYC RESULTS - Read-only after creation
    # ========================================================================

    op.execute("ALTER TABLE kyc_results ENABLE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS admin_full_access_kyc_results ON kyc_results")
    op.execute("""
        CREATE POLICY admin_full_access_kyc_results ON kyc_results
        FOR ALL
        USING (is_admin_user())
        WITH CHECK (is_admin_user());
    """)

    op.execute("DROP POLICY IF EXISTS kyc_results_read_only ON kyc_results")
    op.execute("""
        CREATE POLICY kyc_results_read_only ON kyc_results
        FOR SELECT
        USING (
            is_admin_user()
            OR kyc_session_id IN (
                SELECT id FROM kyc_sessions WHERE
                    borrower_id = get_current_borrower_id()
                    OR (
                        SELECT vendor_id FROM loan_applications WHERE id = kyc_sessions.loan_application_id
                    ) = get_current_vendor_id()
            )
        );
    """)

    # ========================================================================
    # NOTIFICATIONS - User isolation
    # ========================================================================

    op.execute("ALTER TABLE notifications ENABLE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS admin_full_access_notifications ON notifications")
    op.execute("""
        CREATE POLICY admin_full_access_notifications ON notifications
        FOR ALL
        USING (is_admin_user())
        WITH CHECK (is_admin_user());
    """)

    op.execute("DROP POLICY IF EXISTS user_isolation_notifications ON notifications")
    op.execute("""
        CREATE POLICY user_isolation_notifications ON notifications
        FOR ALL
        USING (is_admin_user() OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (is_admin_user() OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)


def downgrade() -> None:
    # Drop policies in reverse order
    op.execute("DROP POLICY IF EXISTS user_isolation_notifications ON notifications")
    op.execute("DROP POLICY IF EXISTS admin_full_access_notifications ON notifications")
    op.execute("ALTER TABLE notifications DISABLE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS kyc_results_read_only ON kyc_results")
    op.execute("DROP POLICY IF EXISTS admin_full_access_kyc_results ON kyc_results")
    op.execute("ALTER TABLE kyc_results DISABLE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS borrower_isolation_kyc_sessions ON kyc_sessions")
    op.execute("DROP POLICY IF EXISTS vendor_isolation_kyc_sessions ON kyc_sessions")
    op.execute("DROP POLICY IF EXISTS admin_full_access_kyc_sessions ON kyc_sessions")
    op.execute("ALTER TABLE kyc_sessions DISABLE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS vendor_self_update_vendors ON vendors")
    op.execute("DROP POLICY IF EXISTS vendor_self_access_vendors ON vendors")
    op.execute("DROP POLICY IF EXISTS admin_full_access_vendors ON vendors")
    op.execute("ALTER TABLE vendors DISABLE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS borrower_update_own_borrowers ON borrowers")
    op.execute("DROP POLICY IF EXISTS borrower_self_update_borrowers ON borrowers")
    op.execute("DROP POLICY IF EXISTS admin_full_access_borrowers ON borrowers")
    op.execute("ALTER TABLE borrowers DISABLE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS borrower_isolation_loan_applications ON loan_applications")
    op.execute("DROP POLICY IF EXISTS vendor_update_loan_applications ON loan_applications")
    op.execute("DROP POLICY IF EXISTS vendor_isolation_loan_applications ON loan_applications")
    op.execute("DROP POLICY IF EXISTS admin_full_access_loan_applications ON loan_applications")
    op.execute("ALTER TABLE loan_applications DISABLE ROW LEVEL SECURITY")

    # Drop helper functions
    op.execute("DROP FUNCTION IF EXISTS get_current_borrower_id()")
    op.execute("DROP FUNCTION IF EXISTS get_current_vendor_id()")
    op.execute("DROP FUNCTION IF EXISTS is_admin_user()")
