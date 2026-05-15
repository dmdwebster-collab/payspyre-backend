# Production Deployment - PaySpyre Backend

## Quick Start (DigitalOcean App Platform)

1. Create App Platform app using `.do/app.yaml`
2. Set secrets in DO console (see `.do/DEPLOYMENT.md`)
3. Push to `main` branch → auto-deploys via GitHub Actions

## Alternative: Droplet Deployment

### Initial Setup
```bash
./scripts/production/setup-droplet.sh
```

### Deploy Updates
```bash
./scripts/production/deploy-droplet.sh <droplet-ip>
```

## Environment Variables

Required in production:
- `DATABASE_URL` - PostgreSQL connection string
- `JWT_SECRET_KEY` - Strong secret key
- `DIDIT_API_KEY` - Didit vendor API key
- `DIDIT_WEBHOOK_SECRET` - Didit webhook verification
- `PERSONA_API_KEY` - Persona vendor API key
- `PERSONA_WEBHOOK_SECRET` - Persona webhook verification
- `CORS_ORIGINS` - Comma-separated allowed origins

## Database Migrations

### Automatic
App Platform runs migrations on deploy via pre-deploy hook.

### Manual
```bash
# SSH into app or droplet
alembic upgrade head
```

### Create new migration
```bash
alembic revision --autogenerate -m "description"
```

## Monitoring

### Health Check
```bash
curl https://api.payspyre.com/health
```

### Logs (App Platform)
```bash
doctl apps logs <app-id> --follow
```

### Logs (Droplet)
```bash
docker logs payspyre-api -f
```

## Security

- All secret keys stored in DO environment (secrets, not plain env vars)
- SSL required for DO databases (`?sslmode=require` appended automatically)
- Non-root user in containers
- Rate limiting via Nginx (when using nginx proxy)
- Health check on `/health` endpoint