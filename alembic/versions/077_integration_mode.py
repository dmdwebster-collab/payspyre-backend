"""Per-integration Simulator/Live ``mode`` (Dave's Integration SIMULATOR mandate).

Revision ID: 077_integration_mode
Revises: 076_rejected_status
Create Date: 2026-07-23

Dave's directive (docs/dave_review_2026-07-21/DAVE_DIRECTIVES_2026-07-22.md,
"Integration SIMULATOR mandate"): every integration must have TWO explicit,
toggle-able modes — ``simulator`` and ``live`` — where the simulator is a real,
reviewable simulation of the integration's behaviour (not a no-op / "not
available"), and live is the real vendor call. Missing credentials is not an
excuse to hide a simulator.

This adds a single first-class ``mode`` column to
``platform_integration_settings`` (default ``simulator``), UNIFYING the mode
concept into one place instead of a parallel per-provider flag.

RECONCILIATION with #207's Flinks ``config.test_mode``: #207 added a typed
Flinks knob ``test_mode`` (bool, default True) that routed bank verification
back through the simulator. That knob is now SUBSUMED by ``mode``. On upgrade we
backfill the flinks row's ``mode`` from its existing ``test_mode`` value so no
tenant's behaviour changes silently:

    flinks.config.test_mode == false  ->  mode = 'live'
    otherwise                          ->  mode = 'simulator'

Every other existing row defaults to ``simulator`` — the safe default, and the
behaviour they already had (no real credentials wired = simulated). The service
layer keeps ``config.test_mode`` mirrored to ``mode`` on every write from here on
so the two can never diverge again; the column is authoritative.

Idempotent static SQL (bandit B608: no interpolation — literal DDL + a single
bound-parameter-free UPDATE keyed off JSONB).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "077_integration_mode"
down_revision = "076_rejected_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add the column with a server default so existing rows are populated
    #    atomically as 'simulator' (the safe default — no accidental live calls).
    op.add_column(
        "platform_integration_settings",
        sa.Column(
            "mode",
            sa.String(),
            nullable=False,
            server_default="simulator",
        ),
    )

    # 2. Constrain to the two allowed values.
    op.create_check_constraint(
        "platform_integration_settings_mode_check",
        "platform_integration_settings",
        "mode IN ('simulator', 'live')",
    )

    # 3. Subsume #207's Flinks ``test_mode``: a flinks row that had test_mode
    #    explicitly false was running live, so preserve that as mode='live'.
    #    Static SQL, no interpolation. ``config`` is JSONB.
    op.execute(
        """
        UPDATE platform_integration_settings
           SET mode = 'live'
         WHERE provider = 'flinks'
           AND (config ->> 'test_mode') = 'false'
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "platform_integration_settings_mode_check",
        "platform_integration_settings",
        type_="check",
    )
    op.drop_column("platform_integration_settings", "mode")
