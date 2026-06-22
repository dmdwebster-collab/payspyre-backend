"""Pure retry-scheduling helpers for the notification outbox (P7.4c).

Deliberately DB-free and side-effect-free so the backoff math can be unit
tested without touching Postgres. The durable model lives in
``app/models/platform/notification_outbox.py``; the worker that consumes it
lives in ``scripts/process_notification_outbox.py``; the enqueue path is in
``RealNotificationDispatcher``.

Semantics (driven by the existing NOTIFICATION_* settings):

- ``NOTIFICATION_MAX_RETRIES`` (default 3) is the number of *retries* AFTER the
  initial send. So an item that failed its first (inline) send has ``attempts=1``
  and is eligible for up to MAX_RETRIES additional worker attempts.
- ``NOTIFICATION_RETRY_DELAYS`` (default "5,15,30") is a comma-separated list of
  per-retry backoff delays in SECONDS. Retry #1 waits delays[0], retry #2 waits
  delays[1], etc. If MAX_RETRIES exceeds the list length, the LAST delay is
  reused (clamp) — so the backoff never silently collapses to zero.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Sequence


def parse_retry_delays(raw: str) -> list[int]:
    """Parse the ``NOTIFICATION_RETRY_DELAYS`` CSV into a list of int seconds.

    Blanks / non-numeric / negative entries are dropped. An empty or fully
    invalid string yields ``[]`` (callers treat that as "no backoff configured"
    and fall back to a single 0s delay via ``delay_seconds_for_attempt``).
    """
    delays: list[int] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value >= 0:
            delays.append(value)
    return delays


def delay_seconds_for_attempt(attempt: int, delays: Sequence[int]) -> int:
    """Backoff (seconds) before the retry that PRODUCES ``attempt``.

    ``attempt`` is the attempt number the worker is about to make: the first
    worker retry is ``attempt=2`` (attempt 1 was the inline send). It indexes
    ``delays[attempt - 2]``; past the end of the list the LAST delay is clamped
    so a short list doesn't degrade into a zero-delay hot loop. With no delays
    configured at all the result is 0 (retry immediately).
    """
    if not delays:
        return 0
    idx = attempt - 2
    if idx < 0:
        idx = 0
    if idx >= len(delays):
        idx = len(delays) - 1
    return int(delays[idx])


def next_attempt_at(
    attempt: int,
    delays: Sequence[int],
    now: datetime | None = None,
) -> datetime:
    """Timezone-aware UTC instant the next attempt (``attempt``) becomes due.

    Pure: ``now`` is injected for tests. ``attempt`` is the upcoming attempt
    number (>= 2 for worker retries). The returned value is ``now`` plus the
    clamped backoff delay for that attempt.
    """
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base + timedelta(seconds=delay_seconds_for_attempt(attempt, delays))


def is_exhausted(attempts: int, max_retries: int) -> bool:
    """True once we've used the initial send + ``max_retries`` worker retries.

    ``attempts`` counts every attempt INCLUDING the inline one. The initial send
    is attempt 1, so the cap is ``1 + max_retries``. At/over that, the item is a
    permanent failure and the worker must stop retrying.
    """
    return attempts >= (1 + max(0, max_retries))
