"""Unit tests for the delinquency-aging job runner (no DB)."""
from unittest.mock import MagicMock, patch

from app.jobs import delinquency


def test_main_runs_aging_commits_and_closes():
    fake_db = MagicMock()
    with patch.object(delinquency, "SessionLocal", return_value=fake_db), patch.object(
        delinquency, "run_delinquency_aging", return_value="aged"
    ) as aging:
        rc = delinquency.main()
    assert rc == 0
    aging.assert_called_once()
    fake_db.commit.assert_called_once()
    fake_db.close.assert_called_once()
    fake_db.rollback.assert_not_called()


def test_main_rolls_back_and_returns_1_on_error():
    fake_db = MagicMock()
    with patch.object(delinquency, "SessionLocal", return_value=fake_db), patch.object(
        delinquency, "run_delinquency_aging", side_effect=RuntimeError("boom")
    ):
        rc = delinquency.main()
    assert rc == 1
    fake_db.rollback.assert_called_once()
    fake_db.close.assert_called_once()
