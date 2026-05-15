# DigitalOcean Deployment Configuration

## GitHub Secrets Required

Add these to your GitHub repository secrets:

### Required
- `DO_ACCESS_TOKEN` - DigitalOcean API token with read/write access
- `DO_APP_ID` - Production App Platform app ID
- `DO_APP_ID_STAGING` - Staging App Platform app ID (optional)

### App Secrets (set in DigitalOcean, not GitHub)
- `jwt.JWT_SECRET_KEY` - Production JWT secret
- `vendors.DIDIT_API_KEY` - Didit API key
- `vendors.DIDIT_WEBHOOK_SECRET` - Didit webhook secret
- `vendors.PERSONA_API_KEY` - Persona API key
- `vendors.PERSONA_WEBHOOK_SECRET` - Persona webhook secret

## Deployment Process

1. **Push to `main` branch** → triggers production deploy
2. **Push to `staging` branch** → triggers staging deploy
3. **Pull requests** → run tests only, no deploy

## App Platform Environment Variables

### Runtime (set in App Platform UI)
- `DATABASE_URL` - Auto-provisioned by DO
- `JWT_SECRET_KEY` - Secret
- `DIDIT_API_KEY` - Secret
- `DIDIT_WEBHOOK_SECRET` - Secret
- `PERSONA_API_KEY` - Secret
- `PERSONA_WEBHOOK_SECRET` - Secret
- `CORS_ORIGINS` - Comma-separated list of allowed origins

## Manual Database Operations

### Run migrations manually
```bash
# Connect to the App Platform via SSH
doctl apps ssh <app-id>

# Run migrations
alembic upgrade head
```

### Create a new migration
```bash
# Locally
alembic revision --autogenerate -m "description"

# Then commit and push
git add alembic/versions/
git commit -m "Add migration: description"
git push
```

### Rollback migration
```bash
# SSH into the app
doctl apps ssh <app-id>

# Rollback one step
alembic downgrade -1
```

## Droplet Alternative

If using droplets instead of App Platform:

```bash
# SSH into droplet
ssh root@<droplet-ip>

# Pull latest code
cd /opt/payspyre
git pull origin main

# Build new image
docker build -t payspyre-backend .

# Stop old container
docker stop payspyre-api

# Start new container
docker run -d \
  --name payspyre-api \
  --restart unless-stopped \
  -p 8000:8000 \
  --env-file /opt/payspyre/.env.production \
  payspyre-backend

# Run migrations
docker exec payspyre-api alembic upgrade head
```