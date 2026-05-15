import pytest
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Import Base directly from sqlalchemy.ext.declarative to avoid circular import
from sqlalchemy.ext.declarative import declarative_base

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

# Create a test-specific Base to avoid circular imports
Base = declarative_base()


@pytest.fixture(scope="function")
def db_session():
    # Create test database if it doesn't exist
    import psycopg2
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

    # Parse connection string
    if "localhost" in TEST_DATABASE_URL:
        conn = psycopg2.connect(
            host="localhost",
            user="postgres",
            password="dev123",
            dbname="postgres",
            isolation_level=ISOLATION_LEVEL_AUTOCOMMIT
        )
        conn.autocommit = True
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = 'payspyre_test'")
        if not cursor.fetchone():
            cursor.execute("CREATE DATABASE payspyre_test")
            cursor.execute("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\"")
        cursor.close()
        conn.close()

    Base.metadata.create_all(bind=test_engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=test_engine)