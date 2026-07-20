"""System documents: versioned merge-field templates + generated loan documents (WS-B)

Revision ID: 056_document_templates
Revises: 054_hardship
Create Date: 2026-07-20

Turnkey parity (video 08 §2.11 "System documents", video 11 borrower documents
tab, executive gap #4):

* ``platform_document_templates`` — one row per template VERSION (new version =
  new row; old rows immutable; ``active`` is the only mutable flag). Scopes:
  global default, per-credit-product, per-vendor (the BC1180/AB4464-style
  per-clinic agreement library). Body is HTML with {{MergeField}} placeholders.
* ``platform_loan_documents`` — generated document snapshots per loan (the
  booking-time loan agreement the borrower sees/signs, on-demand admin
  regenerations, borrower statements). Rendered HTML frozen at generation time
  with the merge context used.

Seeds a v1 GLOBAL default template for each document kind so generation and the
borrower documents tab work out of the box (Dave's real TL templates are
migrated later as new versions / scoped overrides — never destructive).

NOTE for the merge orchestrator: down_revision was the single head
``054_hardship`` when this branch was cut; re-chain at merge if another
workstream landed 055 first.
"""
from typing import Sequence, Union
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "056_document_templates"
down_revision: Union[str, None] = "055_communications_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_document_templates",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False, server_default="global"),
        sa.Column(
            "product_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_credit_products.id"),
            nullable=True,
        ),
        sa.Column(
            "vendor_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vendors.id"),
            nullable=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "(scope = 'global' AND product_id IS NULL AND vendor_id IS NULL) OR "
            "(scope = 'product' AND product_id IS NOT NULL AND vendor_id IS NULL) OR "
            "(scope = 'vendor' AND vendor_id IS NOT NULL AND product_id IS NULL)",
            name="ck_document_templates_scope_coherent",
        ),
        sa.CheckConstraint("version >= 1", name="ck_document_templates_version_positive"),
    )
    # One row per (kind, scope-key, version): three partial unique indexes
    # because NULL scope FKs defeat a plain unique constraint.
    op.create_index(
        "uq_document_templates_global_kind_version",
        "platform_document_templates",
        ["kind", "version"],
        unique=True,
        postgresql_where=sa.text("scope = 'global'"),
    )
    op.create_index(
        "uq_document_templates_product_kind_version",
        "platform_document_templates",
        ["kind", "product_id", "version"],
        unique=True,
        postgresql_where=sa.text("scope = 'product'"),
    )
    op.create_index(
        "uq_document_templates_vendor_kind_version",
        "platform_document_templates",
        ["kind", "vendor_id", "version"],
        unique=True,
        postgresql_where=sa.text("scope = 'vendor'"),
    )
    op.create_index(
        "ix_document_templates_kind_active",
        "platform_document_templates",
        ["kind", "active"],
    )

    op.create_table(
        "platform_loan_documents",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "loan_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column(
            "template_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_document_templates.id"),
            nullable=True,
        ),
        sa.Column("template_version", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("merge_data", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.Column("generated_via", sa.String(), nullable=False, server_default="on_demand"),
        sa.Column("created_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_loan_documents_loan_created",
        "platform_loan_documents",
        ["loan_id", "created_at"],
    )

    _seed_default_templates()


def _seed_default_templates() -> None:
    """v1 global defaults for every kind (placeholder copy, real merge fields).

    Static, parameter-bound INSERTs (bandit B608-safe). Idempotent enough for a
    fresh migration; bodies are deliberately simple — Dave's real TL templates
    supersede them as NEW versions, keeping these as immutable v1 history.
    """
    templates = sa.table(
        "platform_document_templates",
        sa.column("id", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.column("kind", sa.String),
        sa.column("scope", sa.String),
        sa.column("version", sa.Integer),
        sa.column("title", sa.String),
        sa.column("description", sa.String),
        sa.column("body_html", sa.Text),
        sa.column("active", sa.Boolean),
    )

    footer = (
        "<p>{{CompanyName}} — {{SupportEmail}}"
        " — {{WebsiteUrl}}</p>"
    )
    rows = [
        {
            "kind": "loan_agreement",
            "title": "Loan Agreement — Default Template",
            "description": "Global default loan agreement (v1 seed).",
            "body_html": (
                "<h1>Loan Agreement</h1>"
                "<p>This loan agreement (the \"Agreement\") is entered into on "
                "{{GeneratedDate}} between {{CompanyName}} (the \"Lender\") and "
                "{{BorrowerFullName}} (the \"Borrower\").</p>"
                "<h2>Loan terms</h2>"
                "<ul>"
                "<li>Loan reference: {{LoanId}}</li>"
                "<li>Principal amount: {{PrincipalAmount}}</li>"
                "<li>Annual interest rate: {{AnnualInterestRate}}</li>"
                "<li>Term: {{TermMonths}} months ({{InstallmentCount}} installments)</li>"
                "<li>First payment due: {{FirstDueDate}}</li>"
                "<li>Final payment due: {{MaturityDate}}</li>"
                "<li>Total of payments: {{TotalOfPayments}}</li>"
                "<li>Credit product: {{ProductName}}</li>"
                "<li>Vendor: {{VendorName}}, {{VendorCity}}, {{VendorProvince}}</li>"
                "</ul>"
                "<h2>Payment schedule</h2>"
                "{{Table:AmortizationSchedule}}"
                "<h2>Fees</h2>"
                "{{Table:FeeSchedule}}"
                "<p>The Borrower agrees to repay the principal together with "
                "interest and applicable fees according to the schedule above.</p>"
                + footer
            ),
        },
        {
            "kind": "pad_agreement",
            "title": "Pre-Authorized Debit (PAD) Agreement — Default Template",
            "description": "Global default PAD agreement (v1 seed).",
            "body_html": (
                "<h1>Pre-Authorized Debit Agreement</h1>"
                "<p>{{BorrowerFullName}} authorizes {{CompanyName}} to debit the "
                "bank account on file for the scheduled payments of loan "
                "{{LoanId}}, per the schedule below.</p>"
                "{{Table:AmortizationSchedule}}"
                "<p>This authorization remains in effect until the loan is paid "
                "in full or the authorization is revoked in accordance with the "
                "Payments Canada Rule H1 PAD framework.</p>"
                + footer
            ),
        },
        {
            "kind": "amortization_schedule",
            "title": "Amortization Schedule — Default Template",
            "description": "Global default amortization schedule (v1 seed).",
            "body_html": (
                "<h1>Amortization Schedule</h1>"
                "<p>Loan {{LoanId}} — {{BorrowerFullName}} — principal "
                "{{PrincipalAmount}} at {{AnnualInterestRate}} over "
                "{{TermMonths}} months.</p>"
                "{{Table:AmortizationSchedule}}"
                + footer
            ),
        },
        {
            "kind": "fee_schedule",
            "title": "Fee Schedule — Default Template",
            "description": "Global default fee schedule (v1 seed).",
            "body_html": (
                "<h1>Fee Schedule</h1>"
                "<p>Fees applicable to loan {{LoanId}} under the "
                "{{ProductName}} credit product.</p>"
                "{{Table:FeeSchedule}}"
                + footer
            ),
        },
        {
            "kind": "terms_and_conditions",
            "title": "Terms and Conditions — Default Template",
            "description": "Global default T&Cs (v1 seed — replace with counsel-approved copy).",
            "body_html": (
                "<h1>Terms and Conditions</h1>"
                "<p>These are the terms and conditions governing the use of the "
                "{{CompanyName}} platform and services. The authoritative "
                "published copy lives at {{TermsUrl}}.</p>"
                + footer
            ),
        },
        {
            "kind": "privacy_policy",
            "title": "Privacy Policy — Default Template",
            "description": "Global default privacy policy (v1 seed — replace with counsel-approved copy).",
            "body_html": (
                "<h1>Privacy Policy</h1>"
                "<p>{{CompanyName}} collects and safeguards personal information "
                "in accordance with PIPEDA. The authoritative published copy "
                "lives at {{PrivacyUrl}}.</p>"
                + footer
            ),
        },
        {
            "kind": "account_statement",
            "title": "Account Statement — Default Template",
            "description": "Global default account statement (v1 seed).",
            "body_html": (
                "<h1>Account Statement</h1>"
                "<p>{{BorrowerFullName}} — loan {{LoanId}}</p>"
                "<p>Statement period: {{StatementPeriodStart}} to "
                "{{StatementPeriodEnd}}</p>"
                "<ul>"
                "<li>Opening balance: {{StatementOpeningBalance}}</li>"
                "<li>Principal paid this period: {{StatementPrincipalPaid}}</li>"
                "<li>Interest paid this period: {{StatementInterestPaid}}</li>"
                "<li>Closing balance: {{StatementClosingBalance}}</li>"
                "</ul>"
                + footer
            ),
        },
    ]
    op.bulk_insert(
        templates,
        [
            {
                "id": uuid4(),
                "scope": "global",
                "version": 1,
                "active": True,
                **row,
            }
            for row in rows
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_loan_documents_loan_created", "platform_loan_documents")
    op.drop_table("platform_loan_documents")
    op.drop_index("ix_document_templates_kind_active", "platform_document_templates")
    op.drop_index("uq_document_templates_vendor_kind_version", "platform_document_templates")
    op.drop_index("uq_document_templates_product_kind_version", "platform_document_templates")
    op.drop_index("uq_document_templates_global_kind_version", "platform_document_templates")
    op.drop_table("platform_document_templates")
