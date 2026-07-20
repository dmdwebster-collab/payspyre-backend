"""DB-free tests for WS-J Hardship v1 (deferment + due-date change, e-sign gated).

Dave (03__WP_Servicing §5): "we can't unilaterally change those things without
the borrower first signing legal documentation."

THE test that matters most here (money/legal path): **no schedule mutation
before ``signed_at`` is set** — draft creation, send-for-signature, decline,
cancel and expiry must all leave the amortization schedule untouched, and the
apply step hard-rejects an unsigned request.

Also covered: validation (mandatory reason/comment, deferment rolling-window
limit, due-day month bounds), the draft preview (incl. the interest-keeps-
accruing disclosure + integer-cents estimate), composition of WS-F surgery
primitives on apply (suspend + end-of-contract custom transactions), snap_back
capture, webhook translation, maintenance (expiry/completion), the borrower
notification trigger, and the migration-chain pin.

Run just this file:

    source .venv/bin/activate && \
        python -m pytest tests/test_hardship.py -p no:warnings -q
"""
import importlib.util
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.services import hardship, schedule_surgery
from app.services.hardship import HardshipError, HardshipPolicy

D = date
UTC = timezone.utc


# --- fakes -------------------------------------------------------------------


class _Item:
    def __init__(self, n, *, status="scheduled", paid_cents=0, due=None):
        self.id = uuid4()
        self.loan_id = "loan-1"
        self.installment_number = n
        self.due_date = due or D(2026, 7, 1) + timedelta(days=30 * (n - 1))
        self.principal_cents = 25_000
        self.interest_cents = 1_500
        self.total_cents = 26_500
        self.status = status
        self.paid_cents = paid_cents


class _Loan:
    def __init__(self, schedule=None, *, status="active"):
        self.id = uuid4()
        self.application_id = None
        self.patient_id = None
        self.status = status
        self.annual_rate_bps = 1_299  # 12.99%
        self.principal_balance_cents = 300_000
        self.schedule = (
            schedule
            if schedule is not None
            else [
                _Item(n, due=D(2026, 8, 1) if n == 1 else D(2026, 8 + n - 1, 1))
                for n in (1, 2, 3, 4)
            ]
        )


class _Query:
    """Fake query: filters resolved Python-side by the service where it counts;
    honors the (id, loan_id) ownership filter the surgery primitives use."""

    def __init__(self, rows):
        self._rows = rows
        self._wanted_id = None

    def filter(self, *criteria):
        for c in criteria:
            try:
                col = c.left.name
                val = c.right.value
            except AttributeError:
                continue
            if col == "id":
                self._wanted_id = val
            if col == "esign_document_ref":
                self._rows = [
                    r for r in self._rows if getattr(r, "esign_document_ref", None) == val
                ]
        return self

    def first(self):
        for row in self._rows:
            if self._wanted_id is None or row.id == self._wanted_id:
                return row
        return None

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, loan):
        self.loan = loan
        self.added = []
        self.custom_rows = []
        self.hardship_rows = []
        self.commits = 0

    def add(self, obj):
        self.added.append(obj)
        name = type(obj).__name__
        if name == "PlatformLoanCustomTransaction":
            obj.id = obj.id or uuid4()
            self.custom_rows.append(obj)
        if name == "PlatformHardshipRequest":
            if obj.id is None:
                obj.id = uuid4()
            self.hardship_rows.append(obj)

    def flush(self):
        for row in self.custom_rows:
            if row.id is None:
                row.id = uuid4()

    def commit(self):
        self.commits += 1

    def query(self, model, *a, **k):
        name = getattr(model, "__name__", str(model))
        if name == "PlatformLoanScheduleItem":
            return _Query(self.loan.schedule)
        if name == "PlatformLoanCustomTransaction":
            return _Query(self.custom_rows)
        if name == "PlatformHardshipRequest":
            return _Query(self.hardship_rows)
        if name == "PlatformLoan":
            return _Query([self.loan])
        return _Query([])


def _events(db, event_type=None):
    evs = [o for o in db.added if type(o).__name__ == "PlatformEvent"]
    if event_type:
        evs = [e for e in evs if e.event_type == event_type]
    return evs


def _schedule_snapshot(loan):
    return [(i.status, i.due_date) for i in loan.schedule]


def _mk_deferment(db, loan, ids=None, **kw):
    ids = ids if ids is not None else [str(loan.schedule[0].id)]
    return hardship.create_request(
        db,
        loan,
        kind="deferment",
        params={"installment_ids": ids},
        reason=kw.pop("reason", "Job loss — borrower requested relief"),
        comment=kw.pop("comment", "Spoke with borrower 2026-07-10"),
        actor=kw.pop("actor", "staff-7"),
        **kw,
    )


# --- create: validation + preview (draft touches NOTHING) ---------------------


def test_create_deferment_draft_returns_preview_and_touches_nothing():
    loan = _Loan()
    db = _FakeSession(loan)
    before = _schedule_snapshot(loan)

    req = _mk_deferment(db, loan)

    assert req.status == "draft"
    assert req.signed_at is None and req.applied_at is None
    # THE money/legal rule: a draft NEVER touches the schedule.
    assert _schedule_snapshot(loan) == before
    assert db.custom_rows == []

    preview = req.preview
    assert preview["kind"] == "deferment"
    (change,) = preview["changes"]
    assert change["action"] == "suspend_and_append"
    assert change["installment_number"] == 1
    assert change["amount_cents"] == 26_500
    # Appended AFTER the contract's last installment.
    contract_end = max(i.due_date for i in loan.schedule)
    assert date.fromisoformat(change["new_scheduled_date"]) > contract_end
    # Interest keeps accruing — stated plainly, with an integer-cents estimate.
    assert "continues to accrue" in preview["interest_disclosure"]
    assert isinstance(preview["estimated_additional_interest_cents"], int)
    assert preview["estimated_additional_interest_cents"] > 0

    (ev,) = _events(db, hardship.REQUEST_CREATED_EVENT)
    assert ev.actor == "staff-7"
    assert ev.payload["reason"] == "Job loss — borrower requested relief"


def test_create_requires_reason_and_comment():
    loan = _Loan()
    db = _FakeSession(loan)
    with pytest.raises(HardshipError, match="reason is required"):
        _mk_deferment(db, loan, reason="  ")
    with pytest.raises(HardshipError, match="comment is required"):
        _mk_deferment(db, loan, comment="")
    assert db.hardship_rows == []


def test_create_rejects_bad_items_and_kinds():
    loan = _Loan()
    db = _FakeSession(loan)
    with pytest.raises(HardshipError, match="not found"):
        _mk_deferment(db, loan, ids=[str(uuid4())])
    loan.schedule[0].status = "paid"
    with pytest.raises(HardshipError, match="cannot be deferred"):
        _mk_deferment(db, loan, ids=[str(loan.schedule[0].id)])
    with pytest.raises(HardshipError, match="unknown hardship kind"):
        hardship.create_request(
            db, loan, kind="rate_change", params={}, reason="r", comment="c", actor="a"
        )
    with pytest.raises(HardshipError, match="non-empty"):
        _mk_deferment(db, loan, ids=[])
    with pytest.raises(HardshipError, match="duplicate"):
        _mk_deferment(db, loan, ids=[str(loan.schedule[1].id)] * 2)


def test_create_rejects_non_live_loans():
    loan = _Loan(status="paid_off")
    db = _FakeSession(loan)
    with pytest.raises(HardshipError, match="live loan"):
        _mk_deferment(db, loan)


def test_deferment_rolling_window_limit():
    """Config max (default 3 per rolling 12 months) counts previously APPLIED
    deferments; a request that would exceed it is rejected up front."""
    loan = _Loan()
    db = _FakeSession(loan)

    class _Prior:
        kind = "deferment"
        status = "active"
        applied_at = datetime.now(UTC) - timedelta(days=30)
        params = {"installment_ids": ["a", "b"]}

    db.hardship_rows.append(_Prior())

    # 2 already in window + 2 requested > 3 → rejected.
    with pytest.raises(HardshipError, match="deferment limit exceeded"):
        _mk_deferment(db, loan, ids=[str(loan.schedule[0].id), str(loan.schedule[1].id)])

    # 2 + 1 == 3 → allowed.
    req = _mk_deferment(db, loan, ids=[str(loan.schedule[0].id)])
    assert req.status == "draft"

    # A prior deferment OUTSIDE the window doesn't count.
    _Prior.applied_at = datetime.now(UTC) - timedelta(days=400)
    req2 = _mk_deferment(db, loan, ids=[str(loan.schedule[1].id), str(loan.schedule[2].id)])
    assert req2.status == "draft"


def test_due_date_change_day_of_month_preview_and_bounds():
    today = date.today()
    items = [
        _Item(1, due=D(today.year + 1, 3, 1)),
        _Item(2, due=D(today.year + 1, 4, 1)),
    ]
    loan = _Loan(items)
    db = _FakeSession(loan)

    with pytest.raises(HardshipError, match="between 1 and 28"):
        hardship.create_request(
            db, loan, kind="due_date_change", params={"new_day_of_month": 31},
            reason="r", comment="c", actor="a",
        )
    with pytest.raises(HardshipError, match="exactly one of"):
        hardship.create_request(
            db, loan, kind="due_date_change", params={},
            reason="r", comment="c", actor="a",
        )

    before = _schedule_snapshot(loan)
    req = hardship.create_request(
        db, loan, kind="due_date_change", params={"new_day_of_month": 15},
        reason="Payday moved to the 14th", comment="c", actor="a",
    )
    assert _schedule_snapshot(loan) == before  # draft touches nothing
    changes = req.preview["changes"]
    assert [c["new_due_date"] for c in changes] == [
        D(today.year + 1, 3, 15).isoformat(),
        D(today.year + 1, 4, 15).isoformat(),
    ]
    assert all(c["shift_days"] == 14 for c in changes)
    assert req.preview["estimated_additional_interest_cents"] > 0


def test_due_date_change_item_shift_must_stay_in_month():
    today = date.today()
    item = _Item(1, due=D(today.year + 1, 3, 25))
    loan = _Loan([item])
    db = _FakeSession(loan)
    with pytest.raises(HardshipError, match="within its original month"):
        hardship.create_request(
            db, loan, kind="due_date_change",
            params={"item_shifts": [
                {"item_id": str(item.id), "new_due_date": D(today.year + 1, 4, 2).isoformat()}
            ]},
            reason="r", comment="c", actor="a",
        )
    req = hardship.create_request(
        db, loan, kind="due_date_change",
        params={"item_shifts": [
            {"item_id": str(item.id), "new_due_date": D(today.year + 1, 3, 10).isoformat()}
        ]},
        reason="r", comment="c", actor="a",
    )
    (change,) = req.preview["changes"]
    assert change["shift_days"] == -15
    # Moving EARLIER accrues less interest — signed negative estimate.
    assert change["estimated_interest_impact_cents"] < 0


# --- send for signature (still NOTHING applied) --------------------------------


def test_send_for_signature_simulation_mode_advances_without_mutation():
    loan = _Loan()
    db = _FakeSession(loan)
    req = _mk_deferment(db, loan)
    before = _schedule_snapshot(loan)

    hardship.send_for_signature(db, req, loan, actor="staff-7", esign=None)

    assert req.status == "awaiting_signature"
    assert req.esign_document_ref is None  # simulation mode: no vendor call
    assert req.signature_expires_at is not None
    window = req.signature_expires_at - req.signature_requested_at
    assert window == timedelta(days=HardshipPolicy().signature_window_days)
    assert _schedule_snapshot(loan) == before  # STILL nothing applied
    assert db.custom_rows == []
    (ev,) = _events(db, hardship.SENT_FOR_SIGNATURE_EVENT)
    assert ev.payload["simulation_mode"] is True

    # Borrower notification trigger emitted (outbox passthrough shape).
    (notif,) = _events(db, hardship.AGREEMENT_NOTIFICATION_EVENT)
    assert notif.payload["channels"] == ["email"]
    ctx = notif.payload["context"]
    assert set(ctx) >= {"borrower_name", "kind_label", "summary", "signing_url", "expires_date"}
    assert "LOAN AMENDMENT" in ctx["summary"]
    assert "sign" in ctx["summary"].lower()


def test_send_for_signature_only_from_draft():
    loan = _Loan()
    db = _FakeSession(loan)
    req = _mk_deferment(db, loan)
    hardship.send_for_signature(db, req, loan, actor="a")
    with pytest.raises(HardshipError, match="only a draft"):
        hardship.send_for_signature(db, req, loan, actor="a")


class _FakeEsign:
    def __init__(self):
        self.sent = []

    def send_for_signature(self, *, signer, template_id=None, document_id=None,
                           subject=None, message=None, fields=None):
        from app.services.esign.signnow_adapter import SignNowSendResult

        self.sent.append({"signer": signer, "template_id": template_id})
        return SignNowSendResult(document_id="doc-123", signing_url="https://sign/x")


def test_send_for_signature_with_adapter_stores_document_ref(monkeypatch):
    loan = _Loan()
    db = _FakeSession(loan)
    req = _mk_deferment(db, loan)

    class _Setting:
        config = {"hardship_template_id": "tpl-hardship"}

    monkeypatch.setattr(
        "app.services.integration_settings.get", lambda _db, _p: _Setting()
    )
    from app.services.esign.signnow_adapter import SignerInput

    monkeypatch.setattr(
        "app.services.loan_lifecycle._signer_for_loan",
        lambda _db, _loan: SignerInput(email="b@x.ca", name="B X"),
    )
    esign = _FakeEsign()
    hardship.send_for_signature(db, req, loan, actor="staff-7", esign=esign)

    assert req.status == "awaiting_signature"
    assert req.esign_document_ref == "doc-123"
    assert esign.sent[0]["template_id"] == "tpl-hardship"
    (ev,) = _events(db, hardship.SENT_FOR_SIGNATURE_EVENT)
    assert ev.payload["simulation_mode"] is False
    (notif,) = _events(db, hardship.AGREEMENT_NOTIFICATION_EVENT)
    assert notif.payload["context"]["signing_url"] == "https://sign/x"


# --- THE money/legal-path invariant --------------------------------------------


def test_no_schedule_mutation_before_signed_at():
    """MONEY/LEGAL PATH: create + send + decline/cancel/expire NEVER touch the
    schedule; apply is impossible without signed_at."""
    loan = _Loan()
    db = _FakeSession(loan)
    before = _schedule_snapshot(loan)

    req = _mk_deferment(db, loan)
    hardship.send_for_signature(db, req, loan, actor="a")
    assert _schedule_snapshot(loan) == before

    # Direct apply of an unsigned request is hard-rejected.
    assert req.signed_at is None
    with pytest.raises(HardshipError, match="unsigned"):
        hardship._apply(db, req, loan, actor="a")
    assert _schedule_snapshot(loan) == before
    assert db.custom_rows == []

    # Decline → terminal, untouched.
    hardship.decline(db, req, actor="vendor:signnow")
    assert req.status == "declined"
    assert _schedule_snapshot(loan) == before

    # A declined request can never be signed afterwards.
    with pytest.raises(HardshipError, match="cannot record a signature"):
        hardship.mark_signed(db, req, loan, actor="a")
    assert _schedule_snapshot(loan) == before


def test_mark_signed_sets_signed_at_then_applies_deferment():
    loan = _Loan()
    db = _FakeSession(loan)
    item1, item2 = loan.schedule[0], loan.schedule[1]
    req = _mk_deferment(db, loan, ids=[str(item1.id), str(item2.id)])
    hardship.send_for_signature(db, req, loan, actor="staff-7")

    hardship.mark_signed(db, req, loan, actor="vendor:signnow")

    assert req.signed_at is not None
    assert req.status == "active"
    assert req.applied_at is not None

    # Composed WS-F primitives: items suspended…
    assert item1.status == "suspended" and item2.status == "suspended"
    assert len(_events(db, schedule_surgery.ITEM_SUSPENDED_EVENT)) == 2
    # …and the equivalent amounts appended as end-of-contract custom txns.
    assert len(db.custom_rows) == 2
    contract_end = max(D(2026, 8, 1), *(i.due_date for i in loan.schedule[2:]))
    assert all(r.scheduled_date > contract_end for r in db.custom_rows)
    assert [r.amount_cents for r in db.custom_rows] == [26_500, 26_500]
    assert all(r.repayment_mode == "regular" for r in db.custom_rows)
    assert len(_events(db, schedule_surgery.CUSTOM_TXN_ADDED_EVENT)) == 2

    # snap_back captured BEFORE mutation: prior statuses + the created txn ids.
    snap = req.snap_back
    assert [s["prior_status"] for s in snap["items"]] == ["scheduled", "scheduled"]
    assert len(snap["custom_transaction_ids"]) == 2

    (applied,) = _events(db, hardship.APPLIED_EVENT)
    assert applied.payload["signed_at"] == req.signed_at.isoformat()
    assert len(applied.payload["applied"]) == 2


def test_mark_signed_is_idempotent_for_webhook_replay():
    loan = _Loan()
    db = _FakeSession(loan)
    req = _mk_deferment(db, loan)
    hardship.send_for_signature(db, req, loan, actor="a")
    hardship.mark_signed(db, req, loan, actor="vendor:signnow")
    n_customs = len(db.custom_rows)
    out = hardship.mark_signed(db, req, loan, actor="vendor:signnow")  # replay
    assert out is req and req.status == "active"
    assert len(db.custom_rows) == n_customs  # nothing applied twice


def test_mark_signed_after_window_expires_applies_nothing():
    loan = _Loan()
    db = _FakeSession(loan)
    req = _mk_deferment(db, loan)
    hardship.send_for_signature(db, req, loan, actor="a")
    before = _schedule_snapshot(loan)
    late = req.signature_expires_at + timedelta(days=1)
    with pytest.raises(HardshipError, match="expired"):
        hardship.mark_signed(db, req, loan, actor="vendor:signnow", now=late)
    assert req.status == "expired"
    assert _schedule_snapshot(loan) == before
    assert db.custom_rows == []
    assert len(_events(db, hardship.EXPIRED_EVENT)) == 1


def test_due_date_change_applies_shifts_and_completes_immediately():
    today = date.today()
    items = [_Item(1, due=D(today.year + 1, 3, 1)), _Item(2, due=D(today.year + 1, 4, 1))]
    loan = _Loan(items)
    db = _FakeSession(loan)
    req = hardship.create_request(
        db, loan, kind="due_date_change", params={"new_day_of_month": 15},
        reason="Payday change", comment="c", actor="staff-7",
    )
    hardship.send_for_signature(db, req, loan, actor="staff-7")
    assert [i.due_date.day for i in items] == [1, 1]  # untouched until signed

    hardship.mark_signed(db, req, loan, actor="vendor:signnow")

    assert [i.due_date.day for i in items] == [15, 15]
    assert req.snap_back["items"][0]["prior_due_date"] == D(today.year + 1, 3, 1).isoformat()
    # No ongoing window → completed on apply (v1: the change persists).
    assert req.status == "completed"
    (done,) = _events(db, hardship.COMPLETED_EVENT)
    assert done.payload["snap_back_performed"] is False


# --- cancel ---------------------------------------------------------------------


def test_cancel_requires_comment_and_only_pre_signature_states():
    loan = _Loan()
    db = _FakeSession(loan)
    req = _mk_deferment(db, loan)
    with pytest.raises(HardshipError, match="comment is required"):
        hardship.cancel(db, req, actor="a", comment="  ")
    hardship.cancel(db, req, actor="staff-8", comment="borrower withdrew request")
    assert req.status == "cancelled" and req.cancelled_by == "staff-8"
    with pytest.raises(HardshipError, match="cannot cancel"):
        hardship.cancel(db, req, actor="a", comment="again")

    # An applied (active) request cannot be cancelled — it needs the P1
    # snap-back machine, not a status flip.
    req2 = _mk_deferment(db, loan, ids=[str(loan.schedule[1].id)])
    hardship.send_for_signature(db, req2, loan, actor="a")
    hardship.mark_signed(db, req2, loan, actor="a")
    with pytest.raises(HardshipError, match="cannot cancel"):
        hardship.cancel(db, req2, actor="a", comment="c")


# --- webhook translation ----------------------------------------------------------


def test_handle_esign_event_routes_signed_and_declined():
    loan = _Loan()
    db = _FakeSession(loan)
    req = _mk_deferment(db, loan)
    hardship.send_for_signature(db, req, loan, actor="a")
    req.esign_document_ref = "doc-777"  # as the adapter path would have set

    assert hardship.handle_esign_event(db, document_id="nope", status="signed") is None

    out = hardship.handle_esign_event(db, document_id="doc-777", status="signed")
    assert out == "applied"
    assert req.status == "active"
    assert loan.schedule[0].status == "suspended"

    # Replay → idempotent, still "applied".
    assert hardship.handle_esign_event(db, document_id="doc-777", status="signed") == "applied"

    req2 = _mk_deferment(db, loan, ids=[str(loan.schedule[1].id)])
    hardship.send_for_signature(db, req2, loan, actor="a")
    req2.esign_document_ref = "doc-888"
    assert hardship.handle_esign_event(db, document_id="doc-888", status="declined") == "declined"
    assert req2.status == "declined"
    assert loan.schedule[1].status == "scheduled"  # untouched

    # Non-terminal statuses are acknowledged but change nothing.
    req3 = _mk_deferment(db, loan, ids=[str(loan.schedule[2].id)])
    hardship.send_for_signature(db, req3, loan, actor="a")
    req3.esign_document_ref = "doc-999"
    assert hardship.handle_esign_event(db, document_id="doc-999", status="pending") == "ignored"
    assert req3.status == "awaiting_signature"


# --- maintenance -------------------------------------------------------------------


def test_run_maintenance_expires_and_completes():
    loan = _Loan()
    db = _FakeSession(loan)

    stale = _mk_deferment(db, loan, ids=[str(loan.schedule[0].id)])
    hardship.send_for_signature(db, stale, loan, actor="a")

    active = _mk_deferment(db, loan, ids=[str(loan.schedule[1].id)])
    hardship.send_for_signature(db, active, loan, actor="a")
    hardship.mark_signed(db, active, loan, actor="a")
    assert active.status == "active"

    # Far future: the stale signature window lapsed AND the deferral window
    # (end-of-contract custom txn dates) fully passed.
    far = datetime.now(UTC) + timedelta(days=800)
    result = hardship.run_maintenance(db, as_of=far)

    assert result == {"expired": 1, "completed": 1}
    assert stale.status == "expired"
    assert active.status == "completed"
    # v1: completion does NOT undo anything — the suspension persists.
    assert loan.schedule[1].status == "suspended"


# --- notification wiring -------------------------------------------------------------


def test_notification_processor_and_registry_know_the_trigger():
    from app.services.notification_processor import TRIGGER_EVENT_TYPES
    from app.services.notification_render import NOTIFICATION_TYPES, render_email

    assert "hardship_agreement_sent" in TRIGGER_EVENT_TYPES
    spec = NOTIFICATION_TYPES["hardship_agreement_sent"]
    assert spec.sms_template is None  # legal amendment: email-only

    subject, html = render_email(
        "hardship_agreement_sent",
        {
            "borrower_name": "Alex",
            "kind_label": "installment deferment",
            "summary": "LOAN AMENDMENT — HARDSHIP: DEFERMENT\n- Installment #1 …",
            "signing_url": "https://sign/x",
            "expires_date": "2026-08-09",
        },
    )
    assert "installment deferment" in subject
    assert "Alex" in html and "https://sign/x" in html and "2026-08-09" in html


# --- migration chain pin (merge-train convention) -------------------------------------


def test_migration_chain():
    """Pin 054_hardship onto 050_repayment_modes (WS-J builds on WS-F). The
    merge train re-chains this to the final Phase-2 head (expected
    053_delinquency_buckets) — update BOTH the migration and this pin then."""
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "054_hardship.py"
    spec = importlib.util.spec_from_file_location("migration_054", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "054_hardship"
    assert mod.down_revision == "053_delinquency_buckets"


def _route_paths(routes):
    """Collect route paths version-robustly (same pattern as
    test_schedule_surgery): newer starlette/fastapi versions put router
    objects (e.g. ``_IncludedRouter``) in ``app.routes`` with no ``.path`` —
    never assume the attribute, and descend into nested ``.routes``."""
    for r in routes:
        p = getattr(r, "path", None)
        if p:
            yield p
        sub = getattr(r, "routes", None)
        if sub:
            yield from _route_paths(sub)


def test_app_imports_with_hardship_wired():
    """Smoke: the app (model, service, endpoints, webhook extension) imports
    and exposes the WS-J routes; the surface is permission-gated."""
    from app.main import app

    paths = set(_route_paths(app.routes)) | set(app.openapi()["paths"])
    assert "/api/v1/admin/loans/{loan_id}/hardship" in paths
    assert "/api/v1/admin/loans/{loan_id}/hardship/{request_id}" in paths
    assert "/api/v1/admin/loans/{loan_id}/hardship/{request_id}/send-for-signature" in paths
    assert "/api/v1/admin/loans/{loan_id}/hardship/{request_id}/cancel" in paths
    # There is deliberately NO staff "apply" endpoint — only the e-sign webhook
    # (or the non-prod dev force-sign) can apply a hardship change.
    assert "/api/v1/admin/loans/{loan_id}/hardship/{request_id}/apply" not in paths

    # The dev force-sign substitutes for the borrower's signature and is only
    # mounted with the rest of the dev tools (never in production).
    from app.core.config import settings

    dev_tools_mounted = (
        settings.ENVIRONMENT in ("development", "test") or settings.ENABLE_DEV_TOOLS
    )
    force_sign = "/api/v1/admin/dev/hardship/{request_id}/force-sign"
    assert (force_sign in paths) == dev_tools_mounted


def test_hardship_maintenance_job_module():
    """Smoke: the cron runner (python -m app.jobs.hardship) imports and wraps
    hardship.run_maintenance (no DB touched at import time)."""
    from app.jobs import hardship as job

    assert callable(job.main)
    assert job.run_maintenance is hardship.run_maintenance


# --- the dedicated permission gate ----------------------------------------------------


class _FakePerm:
    def __init__(self, resource, action):
        self.resource = resource
        self.action = action


class _FakeUser:
    """Mirrors the current_user.roles[*].role.{name,permissions[*].permission}
    chain require_permission_or_admin walks."""

    def __init__(self, role_name, perms=()):
        role = type("R", (), {})()
        role.name = role_name
        role.permissions = [
            type("RP", (), {"permission": p})() for p in perms
        ]
        self.roles = [type("UR", (), {"role": role})()]


def test_require_permission_or_admin_gate():
    """WS-J permission model (Dave: 'user-defined availability so that junior
    staff members can't …'): admin is implicitly allowed; a plain staff role is
    NOT; the dedicated hardship Role→Permission grant is."""
    from fastapi import HTTPException

    from app.core.auth import require_permission_or_admin

    checker = require_permission_or_admin("hardship", "create")

    # admin → implicit allow.
    admin = _FakeUser("admin")
    assert checker(current_user=admin) is admin

    # plain staff (the rest of /admin/loans allows this) → 403 here.
    with pytest.raises(HTTPException) as exc:
        checker(current_user=_FakeUser("staff"))
    assert exc.value.status_code == 403

    # the dedicated hardship/create grant → allowed.
    officer = _FakeUser("hardship_officer", perms=[_FakePerm("hardship", "create")])
    assert checker(current_user=officer) is officer

    # a grant on the wrong action/resource does NOT leak through.
    wrong = _FakeUser(
        "hardship_officer",
        perms=[_FakePerm("hardship", "apply"), _FakePerm("loans", "create")],
    )
    with pytest.raises(HTTPException):
        checker(current_user=wrong)
