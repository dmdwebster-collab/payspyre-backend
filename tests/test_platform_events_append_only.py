"""
Verifies that the platform_events WORM trigger from migration 021 actually fires.

Hard Rule #1 from payspyre_v2_mvp_kickoff.md: platform_events is append-only.
Migration 021 enforces this via trigger `platform_events_append_only` which
raises 'Cannot modify platform_events table - append-only audit trail (WORM)'
on UPDATE or DELETE.

These tests guarantee a regression migration cannot silently drop the trigger.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import InternalError, ProgrammingError, OperationalError

from app.models.platform.event import PlatformEvent


WORM_ERROR_FRAGMENT = "Cannot modify platform_events table"


def _insert_seed_event(db_session) -> int:
    """Insert one row and return its id. Inserts are allowed; only UPDATE/DELETE are blocked."""
    event = PlatformEvent(
        event_type="test.worm_check",
        actor="test-suite",
        payload={"purpose": "verify_append_only_trigger"},
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)
    assert event.id is not None
    return event.id


class TestPlatformEventsAppendOnly:
    """Trigger-level enforcement of platform_events append-only / WORM property."""

    def test_insert_is_allowed(self, db_session):
        """Sanity check: appends still work. Trigger only blocks mutations."""
        event_id = _insert_seed_event(db_session)
        result = db_session.execute(
            text("SELECT event_type FROM platform_events WHERE id = :id"),
            {"id": event_id},
        ).scalar_one()
        assert result == "test.worm_check"

    def test_update_is_blocked_by_trigger(self, db_session):
        """UPDATE on platform_events must raise the WORM exception."""
        event_id = _insert_seed_event(db_session)

        with pytest.raises((InternalError, ProgrammingError, OperationalError)) as exc_info:
            db_session.execute(
                text("UPDATE platform_events SET event_type = :new WHERE id = :id"),
                {"new": "test.tampered", "id": event_id},
            )
            db_session.commit()
        db_session.rollback()

        assert WORM_ERROR_FRAGMENT in str(exc_info.value), (
            f"Expected WORM exception message, got: {exc_info.value}"
        )

    def test_delete_is_blocked_by_trigger(self, db_session):
        """DELETE on platform_events must raise the WORM exception."""
        event_id = _insert_seed_event(db_session)

        with pytest.raises((InternalError, ProgrammingError, OperationalError)) as exc_info:
            db_session.execute(
                text("DELETE FROM platform_events WHERE id = :id"),
                {"id": event_id},
            )
            db_session.commit()
        db_session.rollback()

        assert WORM_ERROR_FRAGMENT in str(exc_info.value), (
            f"Expected WORM exception message, got: {exc_info.value}"
        )

    def test_trigger_exists_in_pg_catalog(self, db_session):
        """Belt-and-suspenders: confirm the trigger is registered in pg_catalog."""
        result = db_session.execute(
            text(
                """
                SELECT tgname
                FROM pg_trigger
                WHERE tgname = 'platform_events_append_only'
                  AND tgrelid = 'platform_events'::regclass
                  AND NOT tgisinternal
                """
            )
        ).scalar()
        assert result == "platform_events_append_only", (
            "WORM trigger missing from pg_catalog — migration 021 may have been altered"
        )