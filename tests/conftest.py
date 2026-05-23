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
    # by migrations (roles, permissions, platform_credit_products, etc.) —
    # tests like test_kyc_001_schema rely on those rows existing.
    SEED_TABLES = {
        "alembic_version",
        "roles",
        "role_permissions",
        "permissions",
        "platform_credit_products",
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