import pytest
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

# Disable rate limiting for tests
os.environ["RATE_LIMIT_ENABLED"] = "false"

# Use PostgreSQL for tests to match migrated schema
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg2://payspyre:dev123@localhost:5432/payspyre_test"
)

test_engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"sslmode": "disable"} if "localhost" in TEST_DATABASE_URL else {
        "sslmode": "require",
    },
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

# Import Base and all models after creating test engine to avoid circular imports
# This ensures all models are registered with Base before creating tables
from app.db.base import Base, get_db
import app.models  # Import all models to register them with Base


@pytest.fixture(autouse=True)
def _isolate_dependency_overrides():
    """Guarantee FastAPI dependency overrides never leak across tests.

    Several test modules override dependencies on the *real* app
    (``from app.main import app``) — e.g. ``get_current_user`` swapped for a
    synthetic user, ``get_db`` swapped for the test session. If any of those
    fixtures fails to clean up (an interrupted teardown, a fixture error, or a
    parallel worker race), the override bleeds into later tests in CI's
    execution order. A classic symptom is ``AttributeError: 'U' object has no
    attribute 'roles'`` in ``require_roles`` — a synthetic patient user object
    (with ``.role`` but not ``.roles``) leaked from an earlier test into the
    credit-products endpoints.

    This autouse fixture snapshots ``app.dependency_overrides`` before every
    test and restores that exact snapshot afterward, so no test can pollute the
    shared app's override map for the next one. It makes the whole suite
    order-independent regardless of how individual fixtures clean up.
    """
    from app.main import app

    snapshot = dict(app.dependency_overrides)
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(snapshot)


@pytest.fixture(scope="function")
def db_session():
    # Create test database if it doesn't exist
    import psycopg2
    import re

    # Parse connection string to extract credentials
    if "localhost" in TEST_DATABASE_URL:
        # postgresql+psycopg2://user:password@host:port/dbname
        match = re.match(r'postgresql\+psycopg2://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)', TEST_DATABASE_URL)
        if match:
            user, password, host, port, dbname = match.groups()
        else:
            # Fallback for local dev
            user, password, host, port, dbname = "payspyre", "dev123", "localhost", "5432", "payspyre_test"

        conn = psycopg2.connect(
            host=host,
            user=user,
            password=password,
            dbname="postgres"
        )
        conn.autocommit = True
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
        if not cursor.fetchone():
            cursor.execute(f'CREATE DATABASE "{dbname}"')
            cursor.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
        cursor.close()
        conn.close()

        # Skip create_all/drop_all when running against a migrated database (e.g. Supabase).
    # Migrations are the source of truth — SQLAlchemy models may have stale FKs.
    # Note: stripe_transactions has FK to payments table which doesn't exist - known issue.
    is_local = "localhost" in TEST_DATABASE_URL
    is_supabase = "supabase" in TEST_DATABASE_URL.lower()

    # Check if migrations have been run (alembic_version table exists)
    has_migrations = False
    if is_local and not is_supabase:
        try:
            from sqlalchemy import text
            test_conn = test_engine.connect()
            result = test_conn.execute(text("SELECT 1 FROM information_schema.tables WHERE table_name = 'alembic_version'"))
            has_migrations = result.fetchone() is not None
            test_conn.close()
        except Exception:
            pass

    if is_local and not is_supabase and not has_migrations:
        Base.metadata.create_all(bind=test_engine)

    # Truncate test-data tables before each test to guarantee isolation.
    # When tests run against a migrated DB (CI / Supabase), drop_all is not
    # safe because migrations are the source of truth — so we TRUNCATE instead.
    # This prevents cross-test collisions on UNIQUE constraints (vendor email,
    # user email, etc.) when fixtures hardcode values like 'test@example.com'.
    #
    # IMPORTANT: never truncate tables that hold seed/reference data populated
    # by migrations (roles, permissions, etc.) — those rows are required by
    # tests but never modified by them, so it's safe and correct to keep them.
    #
    # platform_credit_products is NOT in SEED_TABLES because tests both
    # create and read credit products. We truncate it and then re-insert
    # the dental_full_arch_v1 seed row (the only one required by tests).
    SEED_TABLES = {
        "alembic_version",
        "roles",
        "role_permissions",
        "permissions",
    }
    if has_migrations or is_supabase:
        from sqlalchemy import text
        with test_engine.begin() as conn:
            all_tables = [
                row[0] for row in conn.execute(text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                ))
            ]
            truncatable = [t for t in all_tables if t not in SEED_TABLES]
            if truncatable:
                quoted = ", ".join(f'"{t}"' for t in truncatable)
                conn.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))

            # Re-seed the dental_full_arch_v1 credit product after TRUNCATE.
            # Mirrors alembic migration 022_seed_dental_full_arch_v1. Tests in
            # test_credit_products_service.py and test_flow_engine.py depend
            # on this row. ON CONFLICT DO NOTHING keeps it idempotent.
            conn.execute(text("""
                INSERT INTO platform_credit_products
                    (code, name, vertical, status, min_amount_cents, max_amount_cents,
                     currency, verification_matrix, decision_ruleset, pricing_config,
                     funding_source)
                VALUES (
                    'dental_full_arch_v1',
                    'Dental Full Arch v1',
                    'dental'::platform_vertical,
                    'active'::platform_credit_product_status,
                    1500000,
                    8000000,
                    'CAD',
                    CAST(:vm AS jsonb),
                    'dental_full_arch_v1.yaml',
                    CAST(:pc AS jsonb),
                    'payspyre_capital'::platform_funding_source
                )
                ON CONFLICT (code) DO NOTHING
            """), {
                "vm": '{"identity": {"required": true, "methods": ["email_otp", "id_doc_scan"], "min_confidence": 0.85}, "income": {"required": true, "methods": ["bank_link", "t4", "noa"], "require_bank_link": false, "min_stated_income_cents": 5000000}, "bureau": {"soft_pull_required": true, "hard_pull_required": true, "min_score": 660, "max_score_age_days": 90}, "affordability": {"max_dti": 0.43, "min_payment_to_income_ratio": 0.20, "max_loan_to_income_ratio": 3.0}}',
                "pc": '{"term_options": [24, 36, 48, 60], "apr_range": [7.99, 28.99], "origination_fee_pct": 0.025}',
            })

    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        if is_local and not is_supabase and not has_migrations:
            Base.metadata.drop_all(bind=test_engine)


@pytest.fixture(scope="function")
def client(db_session):
    """FastAPI test client with database session override.

    This fixture provides a TestClient instance that overrides the get_db
    dependency to use the test database session. All API tests that make
    HTTP requests should use this fixture.
    """
    from app.main import app

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()