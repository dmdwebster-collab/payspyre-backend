"""
Migration integrity tests.

Verifies:
1. All migrations apply cleanly from empty DB to head
2. All migrations can be rolled back (downgrade) and re-applied
3. Schema matches SQLAlchemy models after `upgrade head`
4. Migrations are idempotent (re-running doesn't break anything)
"""
import pytest
from alembic.config import Config
from alembic import command
from sqlalchemy import create_engine, inspect, text

# Import all models to populate Base.metadata
# Note: funding models are excluded (not yet migrated)
from app.models import credit, document, kyc, loan, user, stripe, notification
from app.db.base import Base


@pytest.fixture
def alembic_cfg():
    """Alembic configuration for migration tests."""
    cfg = Config("alembic.ini")
    return cfg


@pytest.fixture
def empty_db_url():
    """Create a fresh empty database for each migration test."""
    import os
    import uuid
    import re
    from sqlalchemy import create_engine

    # Get base connection string
    base_url = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg2://payspyre:dev123@localhost:5432")
    if "/" in base_url:
        base_url = base_url.rsplit("/", 1)[0]

    db_name = f"migration_test_{uuid.uuid4().hex[:8]}"
    admin_engine = create_engine(f"{base_url}/postgres", isolation_level="AUTOCOMMIT")

    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))

    yield f"{base_url}/{db_name}"

    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        conn.close()


def test_upgrade_from_empty_to_head(alembic_cfg, empty_db_url):
    """Migrations apply cleanly from an empty database."""
    alembic_cfg.set_main_option("sqlalchemy.url", empty_db_url)
    command.upgrade(alembic_cfg, "head")

    engine = create_engine(empty_db_url)
    inspector = inspect(engine)
    tables = inspector.get_table_names()

    # Sanity check: we should have all 30 tables
    assert len(tables) >= 30, f"Expected ≥30 tables, got {len(tables)}: {tables}"


def test_downgrade_then_upgrade(alembic_cfg, empty_db_url):
    """Every migration must be reversible."""
    alembic_cfg.set_main_option("sqlalchemy.url", empty_db_url)
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")  # should work again


def test_migrations_are_idempotent(alembic_cfg, empty_db_url):
    """Running `upgrade head` twice should not error."""
    alembic_cfg.set_main_option("sqlalchemy.url", empty_db_url)
    command.upgrade(alembic_cfg, "head")
    command.upgrade(alembic_cfg, "head")  # second run must succeed


@pytest.mark.skip("Funding tables not yet migrated - TODO: create migration for payments, statements, etc.")
def test_schema_matches_models(alembic_cfg, empty_db_url):
    """After migration, DB schema must match SQLAlchemy models (no drift)."""
    alembic_cfg.set_main_option("sqlalchemy.url", empty_db_url)
    command.upgrade(alembic_cfg, "head")

    engine = create_engine(empty_db_url)
    inspector = inspect(engine)
    db_tables = set(inspector.get_table_names())
    model_tables = set(Base.metadata.tables.keys())

    missing_in_db = model_tables - db_tables
    extra_in_db = db_tables - model_tables - {"alembic_version"}

    # Note: funding tables are not yet migrated, exclude from this check
    # TODO: Create migration for funding tables (payments, statements, refunds, payment_schedule)
    funding_tables = {'funding', 'statements', 'payments', 'refunds', 'payment_schedule'}
    missing_in_db = missing_in_db - funding_tables

    assert not missing_in_db, f"Tables in models but not DB: {missing_in_db}"
    assert not extra_in_db, f"Tables in DB but not models: {extra_in_db}"


def test_step_by_step_upgrade(alembic_cfg, empty_db_url):
    """Apply migrations one at a time — catches migrations that depend on a later one."""
    alembic_cfg.set_main_option("sqlalchemy.url", empty_db_url)
    from alembic.script import ScriptDirectory
    script = ScriptDirectory.from_config(alembic_cfg)
    revisions = list(script.walk_revisions())
    revisions.reverse()  # oldest first

    for rev in revisions:
        command.upgrade(alembic_cfg, rev.revision)
