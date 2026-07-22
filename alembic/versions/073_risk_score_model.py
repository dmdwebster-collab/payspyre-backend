"""Risk-score persistence model — immutable, versioned score snapshots

Revision ID: 073_risk_score_model
Revises: 072_settings_backend_gaps
Create Date: 2026-07-22

MERGE ORDER (coordinated): this originally chained from ``071_customer_profile``
alongside its sibling ``072_settings_backend_gaps`` (PR #203). That sibling
landed on main first, so this revision was RE-CHAINED onto ``072`` — main keeps
a single linear head at ``073``, and ``alembic heads`` must never report two.

Creates ``platform_application_risk_scores``: one APPEND-ONLY row per scoring
event on an application. It records what the decision engine computed and then
threw away — numeric score (raw additive total + the 0..1000 scaled display
score), the scorecard identity/version used, the risk band/segment/level,
probability of default and odds (Good:Bad), the 8-node check pipeline, the
grouped business-rule evaluations with generated human-readable comments, and
the DUAL decision (system vs underwriter, each with its own timestamp + actor).

DECISION-PATH: additive only. No existing table or column is touched, and no
decision outcome depends on anything created here.

IMMUTABILITY is enforced in the database, matching ``platform_events`` (021) and
``kyc_events`` (014): a BEFORE UPDATE OR DELETE trigger raises. The one
permitted mutation is flipping ``is_current`` from true to false on a superseded
row (the "which row to show" pointer, not a fact about the score) — the trigger
allows an UPDATE whose only difference is that flag.

All SQL is emitted through the Alembic op API or fully static strings
(bandit B608 clean).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "073_risk_score_model"
down_revision: Union[str, None] = "072_settings_backend_gaps"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_application_risk_scores",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "application_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_credit_applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "is_current", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "scored_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "score_source", sa.String(), nullable=False, server_default="flow_engine"
        ),
        sa.Column(
            "is_backfilled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        # --- the score -----------------------------------------------------
        sa.Column("raw_score", sa.Integer(), nullable=True),
        sa.Column("scaled_score", sa.Integer(), nullable=True),
        sa.Column("natural_min", sa.Integer(), nullable=True),
        sa.Column("natural_max", sa.Integer(), nullable=True),
        sa.Column(
            "scorecard_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_scorecards.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("scorecard_name", sa.String(), nullable=True),
        sa.Column("scorecard_version", sa.Integer(), nullable=True),
        sa.Column("risk_band", sa.String(), nullable=True),
        sa.Column("risk_segment", sa.String(), nullable=True),
        sa.Column("risk_level", sa.String(), nullable=True),
        sa.Column("probability_of_default", sa.Numeric(9, 6), nullable=True),
        sa.Column("odds_good_bad", sa.Numeric(14, 4), nullable=True),
        sa.Column("pd_method", sa.String(), nullable=True),
        sa.Column("pd_calibration", postgresql.JSONB(), nullable=True),
        sa.Column("band_credit_limit_cents", sa.BigInteger(), nullable=True),
        sa.Column("band_annual_rate_bps", sa.Integer(), nullable=True),
        sa.Column("recommended_decision", sa.String(), nullable=True),
        sa.Column("recommended_decision_text", sa.Text(), nullable=True),
        # --- dual decision -------------------------------------------------
        sa.Column("system_decision", sa.String(), nullable=True),
        sa.Column("system_decision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("system_decision_actor", sa.String(), nullable=True),
        sa.Column("underwriter_decision", sa.String(), nullable=True),
        sa.Column("underwriter_decision_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("underwriter_decision_actor", sa.String(), nullable=True),
        # --- frozen payloads -----------------------------------------------
        sa.Column(
            "checks", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "rule_evaluations", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("scorecard_inputs", postgresql.JSONB(), nullable=True),
        sa.Column("decision_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column(
            "notes", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "application_id", "sequence", name="uq_risk_scores_application_sequence"
        ),
    )

    op.create_index(
        "idx_risk_scores_application",
        "platform_application_risk_scores",
        ["application_id"],
    )
    # Exactly one live row per application.
    op.create_index(
        "uq_risk_scores_application_current",
        "platform_application_risk_scores",
        ["application_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    # Report scans: band / segment distributions over a scored-at window.
    op.create_index(
        "idx_risk_scores_band_scored_at",
        "platform_application_risk_scores",
        ["risk_band", "scored_at"],
    )
    op.create_index(
        "idx_risk_scores_segment_scored_at",
        "platform_application_risk_scores",
        ["risk_segment", "scored_at"],
    )
    op.create_index(
        "idx_risk_scores_scorecard",
        "platform_application_risk_scores",
        ["scorecard_id"],
    )

    # ------------------------------------------------------------------
    # APPEND-ONLY (WORM) — same discipline as platform_events / kyc_events.
    # The sole permitted UPDATE is retiring `is_current` on a superseded row.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_risk_score_modification()
        RETURNS TRIGGER AS $$
        BEGIN
            IF (TG_OP = 'DELETE') THEN
                RAISE EXCEPTION
                    'Cannot delete platform_application_risk_scores - append-only score history';
            END IF;
            IF (OLD.is_current AND NOT NEW.is_current
                AND NEW.id IS NOT DISTINCT FROM OLD.id
                AND NEW.application_id IS NOT DISTINCT FROM OLD.application_id
                AND NEW.sequence IS NOT DISTINCT FROM OLD.sequence
                AND NEW.raw_score IS NOT DISTINCT FROM OLD.raw_score
                AND NEW.scaled_score IS NOT DISTINCT FROM OLD.scaled_score
                AND NEW.risk_band IS NOT DISTINCT FROM OLD.risk_band
                AND NEW.risk_segment IS NOT DISTINCT FROM OLD.risk_segment
                AND NEW.system_decision IS NOT DISTINCT FROM OLD.system_decision
                AND NEW.underwriter_decision IS NOT DISTINCT FROM OLD.underwriter_decision
                AND NEW.checks IS NOT DISTINCT FROM OLD.checks
                AND NEW.rule_evaluations IS NOT DISTINCT FROM OLD.rule_evaluations
                AND NEW.scored_at IS NOT DISTINCT FROM OLD.scored_at) THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION
                'Cannot modify platform_application_risk_scores - append-only score history '
                '(re-score by inserting a new row)';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER platform_application_risk_scores_append_only
        BEFORE UPDATE OR DELETE ON platform_application_risk_scores
        FOR EACH ROW
        EXECUTE FUNCTION prevent_risk_score_modification();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS platform_application_risk_scores_append_only "
        "ON platform_application_risk_scores"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_risk_score_modification()")
    op.drop_index("idx_risk_scores_scorecard", table_name="platform_application_risk_scores")
    op.drop_index(
        "idx_risk_scores_segment_scored_at", table_name="platform_application_risk_scores"
    )
    op.drop_index(
        "idx_risk_scores_band_scored_at", table_name="platform_application_risk_scores"
    )
    op.drop_index(
        "uq_risk_scores_application_current", table_name="platform_application_risk_scores"
    )
    op.drop_index("idx_risk_scores_application", table_name="platform_application_risk_scores")
    op.drop_table("platform_application_risk_scores")
