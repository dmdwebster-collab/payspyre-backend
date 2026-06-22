"""DB-free unit tests for the notification retry-scheduling math (P7.4c).

Pure functions in ``app.services.notification_retry`` — no Postgres, no network,
safe to run alongside concurrent agents sharing the test DB.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.services.notification_retry import (
    delay_seconds_for_attempt,
    is_exhausted,
    next_attempt_at,
    parse_retry_delays,
)
from app.services.real_notification_dispatcher import (
    RealNotificationDispatcher,
    TransientNotificationError,
)


def _dispatcher_failing_transient() -> RealNotificationDispatcher:
    """A dispatcher whose vendor send raises transiently, with every DB/vendor
    touch mocked out so the enqueue DECISION can be unit-tested without a DB."""
    d = RealNotificationDispatcher(db=MagicMock())
    d._resolve_recipient = MagicMock(return_value="dest@example.com")
    d._check_suppression = MagicMock(return_value=None)
    d._record_issued_event = MagicMock(return_value=MagicMock(id=1))
    d._build_sender = MagicMock(return_value=MagicMock())
    d._send = MagicMock(side_effect=TransientNotificationError("vendor 503"))
    d._maybe_enqueue_retry = MagicMock()
    return d


class TestEnqueueOnFailureGate:
    """The outbox worker re-drives sends itself; it must NOT re-enqueue (which
    would fork a duplicate queue row). The inline applicant path still enqueues."""

    def test_worker_path_does_not_self_enqueue(self):
        d = _dispatcher_failing_transient()
        with pytest.raises(TransientNotificationError):
            d.send_magic_link(
                uuid.uuid4(), uuid.uuid4(), "email", "TKN", enqueue_on_failure=False
            )
        d._maybe_enqueue_retry.assert_not_called()

    def test_inline_path_enqueues_on_transient_by_default(self):
        d = _dispatcher_failing_transient()
        with pytest.raises(TransientNotificationError):
            d.send_magic_link(uuid.uuid4(), uuid.uuid4(), "email", "TKN")
        d._maybe_enqueue_retry.assert_called_once()


class TestParseRetryDelays:
    def test_default_string(self):
        assert parse_retry_delays("5,15,30") == [5, 15, 30]

    def test_whitespace_and_blanks(self):
        assert parse_retry_delays(" 5 , , 15 ,30,") == [5, 15, 30]

    def test_drops_non_numeric_and_negative(self):
        assert parse_retry_delays("5,abc,-3,15") == [5, 15]

    def test_empty_string(self):
        assert parse_retry_delays("") == []
        assert parse_retry_delays("   ") == []

    def test_zero_is_kept(self):
        assert parse_retry_delays("0,5") == [0, 5]


class TestDelaySecondsForAttempt:
    DELAYS = [5, 15, 30]

    def test_first_worker_retry_uses_first_delay(self):
        # attempt 2 is the first worker retry -> delays[0]
        assert delay_seconds_for_attempt(2, self.DELAYS) == 5

    def test_second_and_third_retries(self):
        assert delay_seconds_for_attempt(3, self.DELAYS) == 15
        assert delay_seconds_for_attempt(4, self.DELAYS) == 30

    def test_past_end_clamps_to_last(self):
        # MAX_RETRIES may exceed the list; the last delay is reused, never 0.
        assert delay_seconds_for_attempt(5, self.DELAYS) == 30
        assert delay_seconds_for_attempt(99, self.DELAYS) == 30

    def test_attempt_below_two_clamps_to_first(self):
        assert delay_seconds_for_attempt(1, self.DELAYS) == 5
        assert delay_seconds_for_attempt(0, self.DELAYS) == 5

    def test_empty_delays_is_zero(self):
        assert delay_seconds_for_attempt(2, []) == 0


class TestNextAttemptAt:
    def test_adds_delay_to_now(self):
        now = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
        nxt = next_attempt_at(2, [5, 15, 30], now=now)
        assert (nxt - now).total_seconds() == 5

    def test_backoff_grows_per_attempt(self):
        now = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
        assert (next_attempt_at(2, [5, 15, 30], now=now) - now).total_seconds() == 5
        assert (next_attempt_at(3, [5, 15, 30], now=now) - now).total_seconds() == 15
        assert (next_attempt_at(4, [5, 15, 30], now=now) - now).total_seconds() == 30

    def test_result_is_timezone_aware(self):
        now = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
        assert next_attempt_at(2, [5], now=now).tzinfo is not None

    def test_naive_now_is_coerced_to_utc(self):
        naive = datetime(2026, 6, 22, 12, 0, 0)
        nxt = next_attempt_at(2, [10], now=naive)
        assert nxt.tzinfo is not None
        assert (nxt - naive.replace(tzinfo=timezone.utc)).total_seconds() == 10


class TestIsExhausted:
    def test_inline_send_not_exhausted(self):
        # attempt 1 = inline send only; with 3 retries cap is 4.
        assert is_exhausted(1, 3) is False

    def test_within_retry_budget(self):
        assert is_exhausted(2, 3) is False
        assert is_exhausted(3, 3) is False

    def test_at_cap_is_exhausted(self):
        # 1 inline + 3 retries = 4 attempts total -> exhausted.
        assert is_exhausted(4, 3) is True
        assert is_exhausted(5, 3) is True

    def test_zero_retries_exhausts_after_inline(self):
        assert is_exhausted(1, 0) is True

    def test_negative_max_retries_treated_as_zero(self):
        assert is_exhausted(1, -5) is True
