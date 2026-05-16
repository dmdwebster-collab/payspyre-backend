import pytest
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

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

    Base.metadata.create_all(bind=test_engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
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