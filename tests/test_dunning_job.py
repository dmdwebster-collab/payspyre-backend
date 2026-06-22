"""Unit tests for the dunning job runner (no DB)."""
from unittest.mock import MagicMock, patch

from app.jobs import dunning
from app.services.dunning import DunningResult
from datetime import date


def test_main_runs_scan_commits_and_closes():
    fake_db = MagicMock()
    with patch.object(dunning, "SessionLocal", return_value=fake_db), patch.object(
        dunning, "run_dunning_scan",
        return_value=DunningResult(as_of=date(2026, 7, 1), reminders_emitted=2, overdue_emitted=1),
    ) as scan:
        rc = dunning.main()
    assert rc == 0
    scan.assert_called_once()
    fake_db.commit.assert_called_once()
    fake_db.close.assert_called_once()
    fake_db.rollback.assert_not_called()


def test_main_rolls_back_and_returns_1_on_error():
    fake_db = MagicMock()
    with patch.object(dunning, "SessionLocal", return_value=fake_db), patch.object(
        dunning, "run_dunning_scan", side_effect=RuntimeError("boom")
    ):
        rc = dunning.main()
    assert rc == 1
    fake_db.rollback.assert_called_once()
    fake_db.close.assert_called_once()
