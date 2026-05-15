#!/bin/bash
# Pre-deploy hook for DigitalOcean App Platform
# This runs migrations before the new deployment becomes live

set -e

echo "Running pre-deploy migrations..."

# Get database connection from environment
DB_URL="${DATABASE_URL:?DATABASE_URL not set}"

# Run migrations
alembic upgrade head

echo "Pre-deploy migrations completed"