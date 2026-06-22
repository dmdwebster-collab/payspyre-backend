"""Unit tests for the notification processor job runner (no DB)."""
from unittest.mock import MagicMock, patch

from app.jobs import notifications
from app.services.notification_processor import ProcessResult


def _proc_returning(results):
    """A fake NotificationProcessor whose run() yields the given results in order."""
    proc = MagicMock()
    proc.run.side_effect = results
    return proc


def test_drains_until_no_events():
    fake_db = MagicMock()
    proc = _proc_returning([
        ProcessResult(scanned=2, sent=2, cursor_advanced_to=2),
        ProcessResult(scanned=0, cursor_advanced_to=2),  # drained
    ])
    with patch.object(notifications, "SessionLocal", return_value=fake_db), patch.object(
        notifications, "NotificationProcessor", return_value=proc
    ):
        rc = notifications.main()
    assert rc == 0
    assert proc.run.call_count == 2
    fake_db.close.assert_called_once()
    fake_db.rollback.assert_not_called()


def test_stops_on_transient_standstill():
    # Cursor never advances (held by transient failures) — must not loop forever.
    fake_db = MagicMock()
    proc = _proc_returning([
        ProcessResult(scanned=1, failed=1, cursor_advanced_to=0),
        ProcessResult(scanned=1, failed=1, cursor_advanced_to=0),  # no progress → break
    ])
    with patch.object(notifications, "SessionLocal", return_value=fake_db), patch.object(
        notifications, "NotificationProcessor", return_value=proc
    ):
        rc = notifications.main()
    assert rc == 0
    assert proc.run.call_count == 2  # first pass, then standstill break on second


def test_error_rolls_back_and_returns_1():
    fake_db = MagicMock()
    proc = MagicMock()
    proc.run.side_effect = RuntimeError("boom")
    with patch.object(notifications, "SessionLocal", return_value=fake_db), patch.object(
        notifications, "NotificationProcessor", return_value=proc
    ):
        rc = notifications.main()
    assert rc == 1
    fake_db.rollback.assert_called_once()
    fake_db.close.assert_called_once()
