"""DB-free unit tests for the decision flow's post-commit outbound ordering.

Hardening item ``decision-txn-outbound``: the outbound vendor side effects of a
decision (SignNow e-sign invite, adverse-action email) must run AFTER the
decision transaction commits and the application row lock is released — never
inline inside the unit-of-work while holding the lock. These tests prove, with
no database, that:

  * the deferred outbound actions fire strictly after the unit-of-work commits
    (so a slow/hanging vendor can't pin the row lock), and
  * a FAILING outbound action does not roll back the already-committed decision
    and does not block the other outbound actions.

They drive the real ``submit_for_decision`` / ``_run_pending_outbound`` paths
against a fake session that records a "COMMIT" marker, with ``_decide`` stubbed
to register a recording outbound action (the decision OUTCOME logic itself is
exercised by the DB-backed tests in ``test_flow_orchestrator.py``).
"""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.flow_orchestrator import FlowOrchestrator


class FakeSession:
    """Minimal Session stand-in that records commit/rollback into a shared log."""

    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.rolled_back = False

    def commit(self) -> None:
        self._log.append("COMMIT")

    def rollback(self) -> None:
        self.rolled_back = True
        self._log.append("ROLLBACK")

    def refresh(self, *_args, **_kwargs) -> None:  # called post-commit by callers
        pass


def _orch_with_fake_db(log: list[str]) -> FlowOrchestrator:
    return FlowOrchestrator(
        db=FakeSession(log),
        consent_service=SimpleNamespace(),
        verification_dispatcher=SimpleNamespace(),
    )


def _stub_decide_registering(orch: FlowOrchestrator, app, log: list[str], outbound):
    """Replace _decide with a stub that mutates app like the real one (sets a
    terminal status) and registers ``outbound`` as a post-commit action."""

    def fake_decide(application):
        application.status = "approved"
        orch._pending_outbound.append(("test_outbound", outbound))
        return {"decision": "approve"}

    orch._decide = fake_decide  # type: ignore[assignment]


def _patch_submit_loaders(orch: FlowOrchestrator, app):
    """Stub out the DB-touching helpers submit_for_decision calls before _decide."""
    orch._get_application = lambda application_id, lock=False: app  # type: ignore[assignment]
    orch._pending_verification_types = lambda application_id: []  # type: ignore[assignment]


class TestRunPendingOutbound:
    """Unit coverage for the post-commit runner in isolation."""

    def test_drains_in_registration_order(self):
        orch = _orch_with_fake_db([])
        order: list[str] = []
        orch._pending_outbound = [
            ("a", lambda: order.append("a")),
            ("b", lambda: order.append("b")),
        ]
        orch._run_pending_outbound()
        assert order == ["a", "b"]
        # Queue is emptied so a retry does not re-fire.
        assert orch._pending_outbound == []

    def test_one_failing_action_does_not_block_the_others_and_never_raises(self):
        orch = _orch_with_fake_db([])
        order: list[str] = []

        def boom():
            order.append("boom")
            raise RuntimeError("vendor down")

        orch._pending_outbound = [
            ("boom", boom),
            ("ok", lambda: order.append("ok")),
        ]
        # Must not propagate — a vendor failure can't break the committed decision.
        orch._run_pending_outbound()
        assert order == ["boom", "ok"]


class TestSubmitForDecisionOutboundOrdering:
    def test_outbound_runs_after_commit(self):
        log: list[str] = []
        orch = _orch_with_fake_db(log)
        app = SimpleNamespace(id=uuid4(), status="started", decision=None)
        _patch_submit_loaders(orch, app)
        _stub_decide_registering(
            orch, app, log, outbound=lambda: log.append("OUTBOUND")
        )

        orch.submit_for_decision(app.id)

        # The decision transaction commits first; the vendor-facing side effect runs
        # only afterwards (lock released). The trailing COMMIT persists any follow-up
        # DB writes the outbound flushed (e.g. the adverse-action audit event). Order
        # proves no inline-under-lock vendor call.
        assert log == ["COMMIT", "OUTBOUND", "COMMIT"]
        # The first COMMIT (the decision) strictly precedes the outbound call.
        assert log.index("COMMIT") < log.index("OUTBOUND")

    def test_failing_outbound_does_not_roll_back_committed_decision(self):
        log: list[str] = []
        orch = _orch_with_fake_db(log)
        app = SimpleNamespace(id=uuid4(), status="started", decision=None)
        _patch_submit_loaders(orch, app)

        def failing_outbound():
            log.append("OUTBOUND_ATTEMPT")
            raise RuntimeError("SignNow timeout")

        _stub_decide_registering(orch, app, log, outbound=failing_outbound)

        # A hanging/failing vendor must not surface as an error to the caller…
        result = orch.submit_for_decision(app.id)

        # …the decision is committed (and stays committed — no rollback)…
        assert "COMMIT" in log
        assert "ROLLBACK" not in log
        assert orch.db.rolled_back is False
        # …the outbound was attempted strictly after the decision commit (the
        # trailing COMMIT is the post-outbound flush-persist, harmless here)…
        assert log == ["COMMIT", "OUTBOUND_ATTEMPT", "COMMIT"]
        assert log.index("COMMIT") < log.index("OUTBOUND_ATTEMPT")
        # …and the decision result is returned normally.
        assert result.decision == {"decision": "approve"}

    def test_external_txn_defers_outbound_to_owner(self):
        """When the caller owns the transaction (_in_external_txn=True) the
        orchestrator must NOT commit or drain — it leaves the outbound queued for
        the owner to drain after their commit."""
        log: list[str] = []
        orch = _orch_with_fake_db(log)
        app = SimpleNamespace(id=uuid4(), status="started", decision=None)
        _patch_submit_loaders(orch, app)
        _stub_decide_registering(
            orch, app, log, outbound=lambda: log.append("OUTBOUND")
        )

        orch.submit_for_decision(app.id, _in_external_txn=True)

        # No self-commit and no outbound fired; the action waits for the owner.
        assert log == []
        assert len(orch._pending_outbound) == 1
        assert orch._pending_outbound[0][0] == "test_outbound"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
