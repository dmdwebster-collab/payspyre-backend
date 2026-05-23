"""Seed dental_full_arch_v1 credit product

Revision ID: 022_seed_dental_full_arch_v1
Revises: 021_create_platform_events
Create Date: 2026-05-22

Part of PR P3: Credit Product Service + JSON Schema Validation

Inserts the first canonical credit product: dental_full_arch_v1.
Pure seed migration — no schema changes, no new columns, no other tables touched.

Downgrade: removes the seeded row by code (idempotent, no error if already absent).
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "022_seed_dental_full_arch_v1"
down_revision: Union[str, None] = "021_create_platform_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VERIFICATION_MATRIX = {
    "identity": {
        "required": True,
        "methods": ["email_otp", "id_doc_scan"],
        "min_confidence": 0.85,
    },
    "income": {
        "required": True,
        "methods": ["bank_link", "t4", "noa"],
        "require_bank_link": False,
        "min_stated_income_cents": 5000000,
    },
    "bureau": {
        "soft_pull_required": True,
        "hard_pull_required": True,
        "min_score": 660,
        "max_score_age_days": 90,
    },
    "affordability": {
        "max_dti": 0.43,
        "min_payment_to_income_ratio": 0.20,
        "max_loan_to_income_ratio": 3.0,
    },
}

_PRICING_CONFIG = {
    "term_options": [24, 36, 48, 60],
    "apr_range": [7.99, 28.99],
    "origination_fee_pct": 0.025,
}


def upgrade() -> None:
    vm_json = json.dumps(_VERIFICATION_MATRIX)
    pc_json = json.dumps(_PRICING_CONFIG)

    op.execute(
        sa.text("""
            INSERT INTO platform_credit_products
                (code, name, vertical, status, min_amount_cents, max_amount_cents,
                 currency, verification_matrix, decision_ruleset, pricing_config,
                 funding_source)
            VALUES (
                'dental_full_arch_v1',
                'Dental Full Arch v1',
                'dental'::platform_vertical,
                'active'::platform_credit_product_status,
                1500000,
                8000000,
                'CAD',
                CAST(:vm AS jsonb),
                'dental_full_arch_v1.yaml',
                CAST(:pc AS jsonb),
                'payspyre_capital'::platform_funding_source
            )
            ON CONFLICT (code) DO NOTHING
        """).bindparams(vm=vm_json, pc=pc_json)
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM platform_credit_products WHERE code = 'dental_full_arch_v1'")
    )
