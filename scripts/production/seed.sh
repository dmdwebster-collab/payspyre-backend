#!/bin/bash
# Production database seed script (for initial setup only)
# DO NOT run on existing production databases

set -e

echo "WARNING: This script seeds production data. Ensure you want to proceed."
read -p "Continue? (yes/no): " confirm
if [ "$confirm" != "yes" ]; then
  echo "Aborted."
  exit 1
fi

# Run the initial schema setup
psql "$DATABASE_URL" -f /app/scripts/init-db.sql

echo "Database seeded successfully"