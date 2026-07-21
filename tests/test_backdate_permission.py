"""WS-F transaction-backdate policy — DB-free tests (pure helpers)."""
from datetime import datetime, timedelta, timezone

from app.api.v1.endpoints.admin_actions import backdate_days, check_backdate_allowed

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


class TestBackdateDays:
    def test_today_is_zero(self):
        assert backdate_days(datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc), NOW) == 0

    def test_future_negative(self):
        assert backdate_days(datetime(2026, 7, 22, tzinfo=timezone.utc), NOW) == -2

    def test_past_days(self):
        assert backdate_days(datetime(2026, 7, 10, tzinfo=timezone.utc), NOW) == 10

    def test_naive_treated_as_utc(self):
        assert backdate_days(datetime(2026, 7, 15), NOW) == 5


class TestCheckBackdateAllowed:
    def test_none_received_at_allowed(self):
        assert check_backdate_allowed(
            None, NOW, window_days=30, has_backdate_grant=False
        ) is None

    def test_today_allowed_without_grant(self):
        assert check_backdate_allowed(
            datetime(2026, 7, 20, tzinfo=timezone.utc), NOW,
            window_days=30, has_backdate_grant=False,
        ) is None

    def test_future_allowed_without_grant(self):
        assert check_backdate_allowed(
            NOW + timedelta(days=3), NOW, window_days=30, has_backdate_grant=False
        ) is None

    def test_past_without_grant_forbidden(self):
        res = check_backdate_allowed(
            NOW - timedelta(days=5), NOW, window_days=30, has_backdate_grant=False
        )
        assert res is not None and res[0] == 403

    def test_past_with_grant_within_window_allowed(self):
        assert check_backdate_allowed(
            NOW - timedelta(days=5), NOW, window_days=30, has_backdate_grant=True
        ) is None

    def test_beyond_window_forbidden_even_with_grant(self):
        res = check_backdate_allowed(
            NOW - timedelta(days=45), NOW, window_days=30, has_backdate_grant=True
        )
        assert res is not None and res[0] == 422

    def test_window_boundary_inclusive(self):
        # Exactly at the window edge is still allowed with the grant.
        assert check_backdate_allowed(
            NOW - timedelta(days=30), NOW, window_days=30, has_backdate_grant=True
        ) is None
        # One day past → 422.
        res = check_backdate_allowed(
            NOW - timedelta(days=31), NOW, window_days=30, has_backdate_grant=True
        )
        assert res is not None and res[0] == 422
