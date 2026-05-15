#!/bin/bash
# Production database migration script
# Run this after deploying to production

set -e

echo "Starting database migrations..."

# Wait for database to be ready
until pg_isready -h "${DB_HOST:-db}" -U "${DB_USER:-payspyre}" -d "${DB_NAME:-payspyre}"; do
  echo "Waiting for database..."
  sleep 2
done

# Run Alembic migrations
alembic upgrade head

echo "Migrations completed successfully"