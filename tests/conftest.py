import pytest
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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
from app.db.base import Base
import app.models  # Import all models to register them with Base


@pytest.fixture(scope="function")
def db_session():
    # Create test database if it doesn't exist
    import psycopg2

    # Parse connection string
    if "localhost" in TEST_DATABASE_URL:
        conn = psycopg2.connect(
            host="localhost",
            user="postgres",
            password="dev123",
            dbname="postgres"
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