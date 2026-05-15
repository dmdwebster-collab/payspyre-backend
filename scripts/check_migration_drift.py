"""
Fail if SQLAlchemy models have changes not reflected in migrations.
Run in CI to force devs to generate migrations for model changes.
"""
import sys
from alembic.config import Config
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine
from app.db.base import Base
import os

def main():
    engine = create_engine(os.environ.get("DATABASE_URL", "postgresql+pyscopg2://payspyre:dev123@localhost:5432/payspyre"))
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        diff = compare_metadata(ctx, Base.metadata)

    if diff:
        print("❌ Schema drift detected. Models differ from migrations:")
        for change in diff:
            print(f"  - {change}")
        print("\nRun: alembic revision --autogenerate -m 'describe change'")
        sys.exit(1)
    print("✅ No schema drift. Models match migrations.")

if __name__ == "__main__":
    main()
