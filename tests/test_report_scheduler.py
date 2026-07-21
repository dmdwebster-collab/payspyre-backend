"""DB-free unit tests for the WS-H scheduled-reports orchestration.

Exercises ``app.services.report_scheduler`` against a fake session + fakes for
the render/deliver seams — no DB, no email. Verifies due-ness gating, cursor
advancement, inert delivery, per-vendor fan-out, and failure isolation.
"""
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

from app.services import report_scheduler as RS


class FakeQuery:
    def __init__(self, items):
        self._items = items

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._items


class FakeSession:
    """Minimal session: returns preset schedules, records added runs + commits."""

    def __init__(self, schedules):
        self._schedules = schedules
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    def query(self, model):
        # The scheduler only queries PlatformReportSchedule at the top level;
        # everything else (definitions, vendors, loans) is monkeypatched away.
        return FakeQuery(self._schedules)

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _schedule(**kw):
    base = dict(
        id=uuid4(),
        report_key="borrower-data",
        definition_id=None,
        cadence="monthly",
        params={},
        recipients=["ops@payspyre.com"],
        per_vendor=False,
        active=True,
        last_period_key=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _fake_result(title="Borrower Data"):
    return SimpleNamespace(
        key="borrower-data",
        title=title,
        filename="payspyre_borrower_data_2026-07-20.xlsx",
        content=b"xlsx-bytes",
        row_count=3,
    )


class RecordingSender:
    def __init__(self):
        self.sent = []

    def send_report(self, *, to_email, subject, html, attachment_filename, attachment_bytes):
        self.sent.append(to_email)
        return SimpleNamespace(status="sent")


def test_run_schedule_delivers_and_advances_cursor(monkeypatch):
    monkeypatch.setattr(RS, "_render_for", lambda *a, **k: _fake_result())
    sched = _schedule()
    db = FakeSession([sched])
    sender = RecordingSender()

    run = RS.run_schedule(db, sched, today=date(2026, 7, 20), sender=sender)

    assert run.status == "sent"
    assert run.report_count == 1
    assert run.recipient_count == 1
    assert sender.sent == ["ops@payspyre.com"]
    # cursor advanced to the previous completed month
    assert sched.last_period_key == "2026-06"
    assert db.commits == 1


def test_run_schedule_inert_without_sender(monkeypatch):
    monkeypatch.setattr(RS, "_render_for", lambda *a, **k: _fake_result())
    # Force notifications off and no sender → inert delivery, still renders.
    monkeypatch.setattr(RS.settings, "USE_REAL_NOTIFICATIONS", False)
    sched = _schedule()
    db = FakeSession([sched])

    run = RS.run_schedule(db, sched, today=date(2026, 7, 20), sender=None)

    assert run.status == "rendered"
    assert run.report_count == 1
    assert run.recipient_count == 0
    assert sched.last_period_key == "2026-06"  # still advances on inert success


def test_run_schedule_skipped_when_no_recipients(monkeypatch):
    monkeypatch.setattr(RS, "_render_for", lambda *a, **k: _fake_result())
    sched = _schedule(recipients=[])
    db = FakeSession([sched])

    run = RS.run_schedule(db, sched, today=date(2026, 7, 20), sender=RecordingSender())

    assert run.status == "skipped"
    assert sched.last_period_key == "2026-06"


def test_run_schedule_per_vendor_fans_out(monkeypatch):
    v1, v2 = uuid4(), uuid4()
    monkeypatch.setattr(RS, "_render_for", lambda *a, **k: _fake_result())
    monkeypatch.setattr(RS, "_vendors_on_book", lambda db, scope: [v1, v2])
    monkeypatch.setattr(
        RS,
        "_resolve_vendor_recipients",
        lambda db, vid: [f"{vid}@clinic.test"],
    )
    sched = _schedule(per_vendor=True, recipients=[])
    db = FakeSession([sched])
    sender = RecordingSender()

    run = RS.run_schedule(db, sched, today=date(2026, 7, 20), sender=sender)

    assert run.status == "sent"
    assert run.report_count == 2       # one per vendor
    assert run.recipient_count == 2
    assert len(sender.sent) == 2


def test_run_schedule_failure_is_isolated(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(RS, "_render_for", _boom)
    sched = _schedule(last_period_key="2026-05")
    db = FakeSession([sched])

    run = RS.run_schedule(db, sched, today=date(2026, 7, 20), sender=RecordingSender())

    assert run.status == "failed"
    assert "render exploded" in run.detail
    # cursor NOT advanced — retried next run
    assert sched.last_period_key == "2026-05"
    assert db.rollbacks == 1


def test_run_due_only_runs_due_schedules(monkeypatch):
    today = date(2026, 7, 20)
    from app.services.metrics import reports_depth as RD

    ran_key, _f, _t = RD.previous_period("monthly", today)

    due = _schedule(last_period_key=None)             # never run → due
    not_due = _schedule(last_period_key=ran_key)      # already ran → skip

    calls = []
    monkeypatch.setattr(
        RS,
        "run_schedule",
        lambda db, s, today, sender=None: calls.append(s)
        or SimpleNamespace(status="sent", report_count=1, recipient_count=1),
    )
    db = FakeSession([due, not_due])

    summary = RS.run_due_schedules(db, today=today)

    assert summary.schedules_considered == 2
    assert summary.schedules_run == 1
    assert calls == [due]
    assert summary.statuses == {"sent": 1}
