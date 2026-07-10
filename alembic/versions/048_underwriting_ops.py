"""Underwriting ops — decision-reason directories + application assignment (WS-E)

Revision ID: 048_underwriting_ops
Revises: 043_canonical_credit_application
Create Date: 2026-07-10

Two additive pieces of underwriting operational completeness (Turnkey parity,
docs/turnkey_parity/02__WP_Underwriting.md):

1. ``platform_decision_reasons`` — the admin-editable directory of REJECT and
   CANCEL reasons (Turnkey keeps these in a Settings directory; Dave: reasons
   must be "standardized ... compliant with all regulations ... not
   discriminatory and defensible"). Seeded with:
     * reject reasons mapped from the engine's stable ``decision_reasons`` codes
       (flow_engine.py) with borrower-facing wording consistent with the
       adverse-action notice (adverse_action._REASON_TEXT), plus staff-usable
       Turnkey-style codes;
     * cancel reasons (non-credit closure): customer request, duplicate
       application, vendor request, offer expired, bank verification expired,
       other.
   Rows are soft-deactivated (``active=false``), never hard-deleted — a code
   that was ever used in a decision must stay resolvable for audit.

2. Application assignment: ``assigned_to_user_id`` (FK users, SET NULL on user
   delete) + ``assigned_at`` on ``platform_credit_applications`` — the
   underwriting queue's assignee lane.

All changes are additive; no existing row or query breaks.

FLAGGED FOR DAVE: the borrower_facing_text seed wordings below require his (and
counsel's) review before launch — they are editable in the directory afterwards.
"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "048_underwriting_ops"
down_revision: Union[str, None] = "043_canonical_credit_application"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Seed data. Codes are stable slugs; reject codes intentionally MATCH the
# engine's stable decision_reasons codes where one exists, so the directory's
# borrower_facing_text can override the adverse-action notice wording for both
# automated and staff declines. Wording mirrors adverse_action._REASON_TEXT.
# ---------------------------------------------------------------------------

REJECT_REASON_SEEDS: list[tuple[str, str, str]] = [
    # (code, internal_label, borrower_facing_text)
    (
        "bureau_below_minimum",
        "Credit score below minimum requirement",
        "Your credit score did not meet our minimum requirement.",
    ),
    (
        "insufficient_income",
        "Income below minimum requirement",
        "Your income did not meet our minimum requirement for the amount requested.",
    ),
    (
        "excessive_debt_ratio",
        "Debt-to-income ratio too high",
        "Your existing debt obligations are too high relative to your income.",
    ),
    (
        "active_bankruptcy",
        "Active or undischarged bankruptcy",
        "Our records indicate an active or recent bankruptcy.",
    ),
    (
        "bankruptcy_discharge_recent",
        "Bankruptcy discharged too recently",
        "Our records indicate a bankruptcy that was discharged too recently to "
        "meet our lending criteria.",
    ),
    (
        "fraud_signal_review",
        "Information could not be verified (fraud signal)",
        "We were unable to verify the information provided.",
    ),
    (
        "identity_manual_review",
        "Identity could not be fully verified",
        "We were unable to fully verify your identity.",
    ),
    (
        "many_active_loans",
        "Too many active credit obligations",
        "You have too many active credit obligations at this time.",
    ),
    (
        "non_resident",
        "Not a resident of Canada",
        "Applicants must be residents of Canada.",
    ),
    (
        "quebec_coming_soon",
        "Province not yet serviced (Quebec)",
        "Service is not yet available in your province.",
    ),
]

CANCEL_REASON_SEEDS: list[tuple[str, str, str]] = [
    (
        "customer_request",
        "Customer requested cancellation",
        "Your application was cancelled at your request.",
    ),
    (
        "duplicate_application",
        "Duplicate application",
        "Your application was cancelled because it duplicates another "
        "application on file.",
    ),
    (
        "vendor_request",
        "Vendor requested cancellation",
        "Your application was cancelled at the request of your treatment provider.",
    ),
    (
        "offer_expired",
        "Offer expired",
        "Your application was cancelled because the associated offer expired.",
    ),
    (
        "bank_verification_expired",
        "Bank verification expired",
        "Your application was cancelled because your bank verification expired "
        "before it could be completed.",
    ),
    (
        "other",
        "Other (see comment)",
        "Your application has been cancelled.",
    ),
]


def upgrade() -> None:
    # --- reason-kind enum ----------------------------------------------------
    op.execute(
        "CREATE TYPE platform_decision_reason_kind AS ENUM ('reject', 'cancel')"
    )
    kind_enum = postgresql.ENUM(name="platform_decision_reason_kind", create_type=False)

    # --- directory table -------------------------------------------------------
    reasons = op.create_table(
        "platform_decision_reasons",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", kind_enum, nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("internal_label", sa.String(), nullable=False),
        sa.Column("borrower_facing_text", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "uq_platform_decision_reasons_kind_code",
        "platform_decision_reasons",
        ["kind", "code"],
        unique=True,
    )

    # --- seeds -----------------------------------------------------------------
    rows = []
    for sort, (code, label, text_) in enumerate(REJECT_REASON_SEEDS):
        rows.append(
            {
                "id": uuid.uuid4(),
                "kind": "reject",
                "code": code,
                "internal_label": label,
                "borrower_facing_text": text_,
                "active": True,
                "sort_order": sort,
            }
        )
    for sort, (code, label, text_) in enumerate(CANCEL_REASON_SEEDS):
        rows.append(
            {
                "id": uuid.uuid4(),
                "kind": "cancel",
                "code": code,
                "internal_label": label,
                "borrower_facing_text": text_,
                "active": True,
                "sort_order": sort,
            }
        )
    op.bulk_insert(reasons, rows)

    # --- application assignment --------------------------------------------------
    op.add_column(
        "platform_credit_applications",
        sa.Column(
            "assigned_to_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "platform_credit_applications",
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_platform_credit_applications_assigned_to",
        "platform_credit_applications",
        ["assigned_to_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_credit_applications_assigned_to",
        "platform_credit_applications",
    )
    op.drop_column("platform_credit_applications", "assigned_at")
    op.drop_column("platform_credit_applications", "assigned_to_user_id")

    op.drop_index(
        "uq_platform_decision_reasons_kind_code", "platform_decision_reasons"
    )
    op.drop_table("platform_decision_reasons")
    op.execute("DROP TYPE IF EXISTS platform_decision_reason_kind")
