"""
Production database configuration with connection pooling.
"""
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from app.core.config import settings
from app.core.db_logging import setup_db_logging


def create_engine_with_pooling(database_url: str):
    """
    Create database engine with production-ready connection pooling.

    Pool sizing formula:
    - pool_size = (cores * 2) + 1 = (4 * 2) + 1 = 9 for standard 4-core droplet
    - max_overflow = pool_size = 9 (total max connections: 18)
    - pool_recycle = 3600 (recycle connections after 1 hour to prevent stale connections)
    - pool_pre_ping = True (verify connections before using)
    """
    return create_engine(
        database_url,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {
            "sslmode": "require",  # Require SSL for PostgreSQL in production
        },
        echo=False,  # We use structured logging instead
        poolclass=QueuePool,
        pool_size=9,  # (cores * 2) + 1
        max_overflow=9,  # Allow doubling pool size under load
        pool_timeout=30,  # Wait 30 seconds for connection
        pool_recycle=3600,  # Recycle connections after 1 hour
        pool_pre_ping=True,  # Verify connections before using
    )


# Create engine with pooling
engine = create_engine_with_pooling(settings.DATABASE_URL)

# Enable query logging
setup_db_logging(engine)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """Dependency for FastAPI endpoints."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_pool_status():
    """Get current pool status for monitoring."""
    pool = engine.pool
    return {
        "pool_size": pool.size(),
        "checked_in": pool.checkedin(),
        "checked_out": pool.checkedout(),
        "overflow": pool.overflow(),
        # QueuePool stores the configured cap privately; non-queue pools
        # (NullPool/StaticPool) have none. getattr keeps the health check from
        # crashing on any pool type (caught by Schemathesis: a public
        # ``max_overflow`` attribute does not exist).
        "max_overflow": getattr(pool, "_max_overflow", None),
    }
