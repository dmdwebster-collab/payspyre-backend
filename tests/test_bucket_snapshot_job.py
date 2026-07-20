"""Unit tests for the month-end bucket-snapshot job runner (no DB)."""
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.jobs import bucket_snapshot


def _result(month=date(2026, 7, 1)):
    return SimpleNamespace(
        snapshot_month=month,
        month_end=date(2026, 7, 31),
        loans_snapshotted=3,
        bucket_counts={"current": 2, "pot_30": 1},
    )


def test_main_runs_snapshot_and_closes():
    fake_db = MagicMock()
    with patch.object(bucket_snapshot, "SessionLocal", return_value=fake_db), patch.object(
        bucket_snapshot, "run_bucket_snapshot", return_value=_result()
    ) as run:
        rc = bucket_snapshot.main([])
    assert rc == 0
    run.assert_called_once_with(fake_db, None)
    fake_db.close.assert_called_once()
    fake_db.rollback.assert_not_called()


def test_main_passes_explicit_month():
    fake_db = MagicMock()
    with patch.object(bucket_snapshot, "SessionLocal", return_value=fake_db), patch.object(
        bucket_snapshot, "run_bucket_snapshot", return_value=_result(date(2026, 5, 1))
    ) as run:
        rc = bucket_snapshot.main(["--month", "2026-05"])
    assert rc == 0
    run.assert_called_once_with(fake_db, date(2026, 5, 1))


def test_main_rolls_back_and_returns_1_on_error():
    fake_db = MagicMock()
    with patch.object(bucket_snapshot, "SessionLocal", return_value=fake_db), patch.object(
        bucket_snapshot, "run_bucket_snapshot", side_effect=RuntimeError("boom")
    ):
        rc = bucket_snapshot.main([])
    assert rc == 1
    fake_db.rollback.assert_called_once()
    fake_db.close.assert_called_once()
