#!/bin/bash
# Deploy to DigitalOcean droplet
# Usage: ./scripts/production/deploy-droplet.sh <droplet-ip>

set -e

DROPLET_IP="${1:?Droplet IP required}"
SSH_USER="${SSH_USER:-root}"
PROJECT_DIR="${PROJECT_DIR:-/opt/payspyre}"

echo "Deploying to $DROPLET_IP..."

# SSH into droplet and deploy
ssh "$SSH_USER@$DROPLET_IP" << 'ENDSSH'
  set -e

  # Navigate to project directory
  cd /opt/payspyre

  # Pull latest code
  git fetch origin main
  git checkout main
  git pull origin main

  # Build new image
  docker build -f Dockerfile.production -t payspyre-backend:latest .

  # Run migrations
  docker run --rm \
    --env-file /opt/payspyre/.env.production \
    payspyre-backend:latest \
    alembic upgrade head

  # Stop and remove old container
  docker stop payspyre-api || true
  docker rm payspyre-api || true

  # Start new container
  docker run -d \
    --name payspyre-api \
    --restart unless-stopped \
    -p 8000:8000 \
    --env-file /opt/payspyre/.env.production \
    payspyre-backend:latest

  # Clean up old images
  docker image prune -af

  echo "Deployment complete"
ENDSSH

echo "Deployed successfully to $DROPLET_IP"