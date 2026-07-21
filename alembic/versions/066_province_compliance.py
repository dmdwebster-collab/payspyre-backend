"""Province compliance engine (Workstream W2 — Turnkey parity, videos 07-08).

Revision ID: 066_province_compliance
Revises: 064_borrower_depth
Create Date: 2026-07-20

NOTE (merge train): down_revision pinned to the current single head
``064_borrower_depth``. If a parallel Wave-2 workstream lands first, re-chain
this down_revision to the new head (one-line change) — this migration is purely
additive and order-independent.

Two additive pieces:

1. ``platform_province_compliance_rules`` — one row per Canadian province /
   territory. Encodes APR caps, high-cost-credit licensing thresholds,
   licensing flags, communication-window / frequency caps, required
   disclosures, and the Quebec French-language requirement. Consumed by
   app/services/province_compliance.py, which BLOCKS product creation/update
   when a config's worst-case APR breaches an enabled targeted province.

2. ``platform_credit_products.provinces`` (JSONB, nullable) — the provinces a
   product is offered in; feeds the compliance gate (NULL/empty => must clear
   every enabled province's cap).

The table is SEEDED with CONSERVATIVE PLACEHOLDERS — apr_cap_bps = the federal
Criminal Code s.347 ceiling (3500 bps / 35%), high-cost thresholds left NULL
(unknown), comms window 08:00–21:00 / ≤3 per week, SK license_required=True,
QC disabled + French-language required. Every row is counsel_confirmed=False;
the admin API surfaces these as "PENDING COUNSEL — Dave/legal must confirm".
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "066_province_compliance"
down_revision: Union[str, None] = "065_vendor_disbursements"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PENDING = "PENDING COUNSEL — Dave/legal must confirm this value."
_S347_PLACEHOLDER_BPS = 3500

# (code, name). Canada's 13 provinces + territories.
_PROVINCES = (
    ("AB", "Alberta"),
    ("BC", "British Columbia"),
    ("MB", "Manitoba"),
    ("NB", "New Brunswick"),
    ("NL", "Newfoundland and Labrador"),
    ("NS", "Nova Scotia"),
    ("NT", "Northwest Territories"),
    ("NU", "Nunavut"),
    ("ON", "Ontario"),
    ("PE", "Prince Edward Island"),
    ("QC", "Quebec"),
    ("SK", "Saskatchewan"),
    ("YT", "Yukon"),
)


def _seed_rows() -> list[dict]:
    rows: list[dict] = []
    for code, name in _PROVINCES:
        rows.append(
            {
                "province_code": code,
                "province_name": name,
                "enabled": code != "QC",
                "apr_cap_bps": _S347_PLACEHOLDER_BPS,
                "high_cost_apr_threshold_bps": None,
                "high_cost_license_held": False,
                "license_required": code == "SK",
                "license_notes": (
                    "Saskatchewan requires an alternative-lender licence "
                    "regardless of APR. " + _PENDING
                    if code == "SK"
                    else _PENDING
                ),
                "comms_window_start_hour": 8,
                "comms_window_end_hour": 21,
                "comms_max_contacts_per_week": 3,
                "required_disclosures": [],
                "language_requirement": "fr-CA" if code == "QC" else None,
                "quebec_language_required": code == "QC",
                "counsel_confirmed": False,
                "notes": _PENDING,
            }
        )
    return rows


def upgrade() -> None:
    rules = op.create_table(
        "platform_province_compliance_rules",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("province_code", sa.String(2), nullable=False),
        sa.Column("province_name", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("apr_cap_bps", sa.Integer(), nullable=True),
        sa.Column("high_cost_apr_threshold_bps", sa.Integer(), nullable=True),
        sa.Column(
            "high_cost_license_held",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "license_required", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("license_notes", sa.Text(), nullable=True),
        sa.Column("comms_window_start_hour", sa.Integer(), nullable=True),
        sa.Column("comms_window_end_hour", sa.Integer(), nullable=True),
        sa.Column("comms_max_contacts_per_week", sa.Integer(), nullable=True),
        sa.Column(
            "required_disclosures",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("language_requirement", sa.String(16), nullable=True),
        sa.Column(
            "quebec_language_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "counsel_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("province_code", name="uq_province_compliance_code"),
    )

    op.bulk_insert(rules, _seed_rows())

    op.add_column(
        "platform_credit_products",
        sa.Column("provinces", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("platform_credit_products", "provinces")
    op.drop_table("platform_province_compliance_rules")
