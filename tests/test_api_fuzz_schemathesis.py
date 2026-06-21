"""L3 — API contract / schema fuzz (Schemathesis over the FastAPI OpenAPI schema).

Schemathesis derives semantics-aware fuzzers from the live `/openapi.json` and throws
malformed / boundary / adversarial inputs at EVERY operation. The invariant asserted
here is the strongest, lowest-noise one: **no input may crash the server** (a 5xx is an
unhandled exception). Auth-gated routes answer 401/403 (well below 500) — that's a pass;
the value is the unauthenticated surface (products, applications, webhooks) where a raw
input reaches real handler logic.

Run: TEST_DATABASE_URL=... pytest tests/test_api_fuzz_schemathesis.py -q
"""
from __future__ import annotations

import os

import schemathesis
from hypothesis import HealthCheck, seed, settings
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.main import app

# Build the fuzzers from the app's own OpenAPI document (ASGI — no network).
schema = schemathesis.openapi.from_asgi("/openapi.json", app)

_TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL", "postgresql+psycopg2://payspyre:dev123@localhost:5432/payspyre_test"
)
_fuzz_engine = create_engine(
    _TEST_DB_URL,
    connect_args={"sslmode": "disable"} if "localhost" in _TEST_DB_URL else {},
)


# Per-push CI leaves FUZZ_SEED unset → seed 0 → a deterministic gate (always the same
# inputs, never flaky). The nightly EXPLORER sets FUZZ_SEED (e.g. the run number) and a
# larger FUZZ_MAX_EXAMPLES to roam new inputs and surface fresh bugs — reproducibly,
# because the seed is logged. This is the "test over and over until perfect" loop.
_SEED = int(os.getenv("FUZZ_SEED", "0"))
_MAX_EXAMPLES = int(os.getenv("FUZZ_MAX_EXAMPLES", "20"))


@schema.parametrize()
@seed(_SEED)
@settings(
    max_examples=_MAX_EXAMPLES,  # per operation; the whole API is the breadth
    deadline=None,             # ASGI round-trips + DB writes are not latency-bounded here
    database=None,             # don't let a local .hypothesis/ cache sway CI outcomes
    suppress_health_check=list(HealthCheck),
)
def test_no_server_error(case, db_session):
    # Full per-EXAMPLE isolation: this body re-runs for every generated input, so each
    # gets its own connection + outer transaction. The request's Session joins it in
    # create_savepoint mode, so the handler's commit() only releases a savepoint inside
    # that transaction — which we then roll back. Result: every fuzz input runs against
    # the same clean state, nothing persists (no pollution of the shared test DB), and
    # the gate is deterministic run-to-run. (db_session prepares a migrated/truncated base.)
    connection = _fuzz_engine.connect()
    transaction = connection.begin()

    def _get_db():
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _get_db
    try:
        response = case.call()
    finally:
        app.dependency_overrides.pop(get_db, None)
        transaction.rollback()
        connection.close()

    assert response.status_code < 500, (
        f"{case.method} {case.path} crashed with {response.status_code}: "
        f"{response.text[:300]}"
    )
