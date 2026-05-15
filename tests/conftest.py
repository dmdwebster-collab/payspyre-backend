import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Import Base directly from sqlalchemy.ext.declarative to avoid circular import
from sqlalchemy.ext.declarative import declarative_base

TEST_DATABASE_URL = "sqlite:///./test.db"

test_engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

# Create a test-specific Base to avoid circular imports
Base = declarative_base()


@pytest.fixture(scope="function")
def db_session():
    Base.metadata.create_all(bind=test_engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=test_engine)