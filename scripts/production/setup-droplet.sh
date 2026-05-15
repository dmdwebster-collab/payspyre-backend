#!/bin/bash
# Initial setup for DigitalOcean droplet
# Run this once on a fresh droplet

set -e

echo "Setting up PaySpyre droplet..."

# Install dependencies
apt-get update
apt-get install -y docker docker-compose git nginx certbot python3-certbot-nginx

# Create project directory
mkdir -p /opt/payspyre
cd /opt/payspyre

# Clone repository (adjust repo URL)
git clone https://github.com/michael-webster/payspyre-backend.git .
# Or use SSH: git clone git@github.com:michael-webster/payspyre-backend.git .

# Create .env.production file (edit this manually)
cp .env.production.example .env.production

# Build Docker image
docker build -f Dockerfile.production -t payspyre-backend:latest .

# Setup Nginx (optional - for droplet with nginx)
mkdir -p /etc/nginx/ssl
# Place your SSL certificates in /etc/nginx/ssl/
# Then copy nginx/nginx.conf to /etc/nginx/nginx.conf

# Enable services
systemctl enable docker
systemctl enable nginx

# Start the application
docker run -d \
  --name payspyre-api \
  --restart unless-stopped \
  -p 8000:8000 \
  --env-file /opt/payspyre/.env.production \
  payspyre-backend:latest

echo "Setup complete. Edit /opt/payspyre/.env.production and restart the container."