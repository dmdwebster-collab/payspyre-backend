"""Dave's canonical Application Status Flow v1.00 (2026-07-21 review §A)

Revision ID: 068_dave_status_model
Revises: 067_app_process_config
Create Date: 2026-07-21

Extends ``platform_application_status`` with the states Dave's model names that
we had no slot for, and converts the two legacy values that are genuinely
superseded.

ADDED VALUES (forward-only; Postgres can add but never remove enum values):
    credit_report, bank_verification, application_verification
        — his three PARALLEL verification gates
    offer_acceptance, agreement_signature
        — the two steps between Credit Underwriting and Approved
    active
        — the loan is activated; today this lives only on ``loans.status``
    repaid, renewed, refinanced, transferred, settlement, written_off
        — the six closed states hanging off Active

ROW CONVERSIONS (data-preserving, both are legacy sub-states no code WRITES —
they only appear in read-side maps, verified by grep before writing this):
    pre_qualified      -> origination
        Dave folds pre-qualification scoring INTO Origination ("Pre-Qualification
        scoring - initial limited application scoring completed" is one of that
        status's descriptions, not a status of its own).
    awaiting_hard_pull -> credit_report
        Exactly his Credit Report status: submitted, credit report required and
        not yet received.

DELIBERATELY NOT CONVERTED (see the PR body for the full reasoning):
    started / verifying / underwriting / under_review / approved / declined /
    withdrawn / expired all keep their rows. Those values are read AND written by
    the live decision path and by ~2500 tests; ``underwriting`` vs
    ``under_review`` is a pre-decision vs post-decision distinction the
    orchestrator depends on, and merging them would silently swallow late
    verification webhooks. They are mapped to Dave's canonical statuses in
    ``app/services/application_status.LEGACY_TO_CANONICAL`` instead — no
    information is dropped, and the UI reads the canonical layer.

No table/column changes; ``platform_credit_applications`` is untouched apart
from the two UPDATEs above. SQL is fully static (bandit B608).
"""
from typing import Sequence, Union

from alembic import op


revision: str = "068_dave_status_model"
down_revision: Union[str, None] = "067_app_process_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ADD_VALUE = "ALTER TYPE platform_application_status ADD VALUE IF NOT EXISTS "

#: Fully static statements (no interpolation — bandit B608 clean).
_ADD_STATUS_VALUES = (
    # parallel verification gates
    _ADD_VALUE + "'credit_report'",
    _ADD_VALUE + "'bank_verification'",
    _ADD_VALUE + "'application_verification'",
    # post-underwriting steps
    _ADD_VALUE + "'offer_acceptance'",
    _ADD_VALUE + "'agreement_signature'",
    # servicing
    _ADD_VALUE + "'active'",
    # the six closed states off Active
    _ADD_VALUE + "'repaid'",
    _ADD_VALUE + "'renewed'",
    _ADD_VALUE + "'refinanced'",
    _ADD_VALUE + "'transferred'",
    _ADD_VALUE + "'settlement'",
    _ADD_VALUE + "'written_off'",
)


def upgrade() -> None:
    # --- extend the enum ---------------------------------------------------
    # ADD VALUE cannot run inside a transaction block on older PG; alembic wraps
    # migrations in a txn, so run them in an autocommit block (idiom of migration
    # 043). IF NOT EXISTS keeps the migration idempotent on re-run. The block also
    # commits each ADD VALUE, so the new labels are usable by the UPDATEs below.
    with op.get_context().autocommit_block():
        for statement in _ADD_STATUS_VALUES:
            op.execute(statement)

    # --- convert the superseded legacy rows ---------------------------------
    op.execute(
        """
        UPDATE platform_credit_applications
           SET status = 'origination'
         WHERE status = 'pre_qualified'
        """
    )
    op.execute(
        """
        UPDATE platform_credit_applications
           SET status = 'credit_report'
         WHERE status = 'awaiting_hard_pull'
        """
    )


def downgrade() -> None:
    # Forward-only. Postgres cannot drop an enum value, and the two conversions
    # are not reversible without a per-row audit of which 'origination' rows were
    # previously 'pre_qualified'. Rolling back the enum would require recreating
    # the type and rewriting every dependent column; the values are additive and
    # harmless if left in place.
    pass
