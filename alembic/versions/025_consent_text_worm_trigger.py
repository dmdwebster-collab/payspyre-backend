"""Add WORM trigger protecting platform_consents immutable text columns

Revision ID: 025_consent_text_worm_trigger
Revises: 024_p7_5_manual_review_enum
Create Date: 2026-06-06

Closes the "No DB-level WORM trigger on platform_consents" item logged in
payspyre_backlog.md (2026-05-25, from P5).

Spec §2.6 / §8.2 / Hard Rule #1: ``consent_text_shown`` and
``consent_text_version`` are immutable once written ("non-negotiable for
class-action defense"). Until now that was enforced only at the application
layer (``consent_service.revoke_consent`` never touches the text columns). This
migration adds database-level enforcement so a raw UPDATE cannot tamper with the
captured consent language.

IMPORTANT — this is NOT a wholesale append-only trigger like the one on
platform_events (migration 021). ``platform_consents`` legitimately receives an
UPDATE when a consent is revoked (``revoked_at``). So the trigger is
**column-specific**: it blocks an UPDATE only when ``consent_text_shown`` or
``consent_text_version`` would actually change, and allows every other UPDATE
(notably ``revoked_at``).

DELETE is intentionally left to existing ORM cascade behavior
(``PlatformPatient`` / ``PlatformCreditApplication`` use
``cascade="all, delete-orphan"``); the documented gap and the spec requirement
are about UPDATE immutability of the text columns. A later migration can add
DELETE protection if consent-record permanence must be enforced at the DB level.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "025_consent_text_worm_trigger"
down_revision: Union[str, None] = "024_p7_5_manual_review_enum"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FUNCTION = "prevent_platform_consents_text_modification"
_TRIGGER = "platform_consents_text_immutable"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_FUNCTION}()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.consent_text_shown IS DISTINCT FROM OLD.consent_text_shown
               OR NEW.consent_text_version IS DISTINCT FROM OLD.consent_text_version THEN
                RAISE EXCEPTION
                    'Cannot modify platform_consents.consent_text_shown or consent_text_version - immutable consent record (WORM, spec 2.6). Create a new consent row instead.';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    op.execute(
        f"""
        CREATE TRIGGER {_TRIGGER}
        BEFORE UPDATE ON platform_consents
        FOR EACH ROW
        EXECUTE FUNCTION {_FUNCTION}();
        """
    )

    op.execute(
        f"""
        COMMENT ON FUNCTION {_FUNCTION}() IS
        'Security function: blocks UPDATEs that change platform_consents.consent_text_shown or consent_text_version. Enforces consent-text immutability (WORM) for class-action defense (spec 2.6 / 8.2). revoked_at and all other columns remain updatable. Do NOT drop trigger platform_consents_text_immutable.';
        """
    )


def downgrade() -> None:
    op.execute(f"COMMENT ON FUNCTION {_FUNCTION}() IS NULL")
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON platform_consents")
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION}()")
