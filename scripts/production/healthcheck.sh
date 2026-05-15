#!/bin/bash
# Health check script for load balancers
# Returns 0 if healthy, 1 if unhealthy

HEALTH_URL="${HEALTH_URL:-http://localhost:8000/health}"
TIMEOUT=${TIMEOUT:-5}

curl -f -s -o /dev/null --max-time "$TIMEOUT" "$HEALTH_URL" || exit 1
exit 0