# PaySpyre — Staging Deploy Runbook (DigitalOcean App Platform)

**First time use:** follow steps 1 → 7 in order. Total time: 30–45 minutes.

**Re-deploy after this:** push to `staging` branch → DO auto-deploys via the GitHub integration.

---

## Prerequisites

```bash
# Install doctl if not present (macOS)
brew install doctl
# Windows
scoop install doctl

# Authenticate
doctl auth init    # paste your DO Personal Access Token
doctl account get  # verify
```

You'll also need:
- The `dmdwebster-collab` GitHub account connected to your DO team (Settings → Integrations → GitHub)
- A DNS provider where you control `payspyre.com` (Cloudflare based on prior project notes)

---

## Step 1 — Create the staging app

```bash
cd /path/to/payspyre-backend
doctl apps create --spec .do/app-staging.yaml
```

This returns an `App ID`. Save it:

```bash
export STAGING_APP_ID=<id-from-output>
echo $STAGING_APP_ID > .do/staging-app-id.txt   # gitignored
```

## Step 2 — Set secrets

DO won't deploy until every `type: SECRET` env var has a value. Set them via console (App → Settings → web → Edit Component → Environment Variables) **or** via CLI:

```bash
# Required for boot
doctl apps update-config $STAGING_APP_ID --env "JWT_SECRET_KEY=$(openssl rand -hex 32)"

# Identity verification (use staging keys, NOT production)
doctl apps update-config $STAGING_APP_ID --env "DIDIT_API_KEY=<staging-key>"
doctl apps update-config $STAGING_APP_ID --env "DIDIT_WEBHOOK_SECRET=<staging-secret>"
doctl apps update-config $STAGING_APP_ID --env "PERSONA_API_KEY=<staging-key>"
doctl apps update-config $STAGING_APP_ID --env "PERSONA_WEBHOOK_SECRET=<staging-secret>"

# Observability (the .env.example expects these)
doctl apps update-config $STAGING_APP_ID --env "SENTRY_DSN=<sentry-staging-dsn>"
doctl apps update-config $STAGING_APP_ID --env "ENVIRONMENT=staging"
doctl apps update-config $STAGING_APP_ID --env "VERSION=0.1.0"

# Stripe (use test keys)
doctl apps update-config $STAGING_APP_ID --env "STRIPE_SECRET_KEY=sk_test_..."
doctl apps update-config $STAGING_APP_ID --env "STRIPE_WEBHOOK_SECRET=whsec_..."

# Notifications
doctl apps update-config $STAGING_APP_ID --env "RESEND_API_KEY=<staging>"
doctl apps update-config $STAGING_APP_ID --env "RESEND_FROM_EMAIL=noreply@staging.payspyre.com"
doctl apps update-config $STAGING_APP_ID --env "TWILIO_ACCOUNT_SID=<staging>"
doctl apps update-config $STAGING_APP_ID --env "TWILIO_AUTH_TOKEN=<staging>"
doctl apps update-config $STAGING_APP_ID --env "TWILIO_FROM_NUMBER=<staging>"

# AWS S3
doctl apps update-config $STAGING_APP_ID --env "AWS_ACCESS_KEY_ID=<key>"
doctl apps update-config $STAGING_APP_ID --env "AWS_SECRET_ACCESS_KEY=<secret>"
doctl apps update-config $STAGING_APP_ID --env "AWS_S3_BUCKET=payspyre-documents-staging"
doctl apps update-config $STAGING_APP_ID --env "AWS_REGION=us-east-1"
```

**Alt:** put everything in `.do/staging.env` (gitignored) and use `xargs`:

```bash
grep -v '^#' .do/staging.env | xargs -I {} doctl apps update-config $STAGING_APP_ID --env "{}"
```

## Step 3 — Push the `staging` branch

The app spec deploys from `branch: staging` (not `main`), so:

```bash
git checkout -b staging
git push -u origin staging
```

DO will detect the push and start a deploy. Watch it:

```bash
doctl apps list-deployments $STAGING_APP_ID
doctl apps logs $STAGING_APP_ID --follow
```

## Step 4 — Run migrations

The current `app.yaml` does **not** declare a pre-deploy job (gap — TODO add to `jobs:` block). Until that's added, run migrations manually after the first deploy:

```bash
# Get a shell on the running container
doctl apps console $STAGING_APP_ID

# Inside the container:
alembic upgrade head
```

Once verified, this should be promoted into an automated pre-deploy job in `app-staging.yaml`:

```yaml
jobs:
- name: migrate
  kind: PRE_DEPLOY
  source_dir: /
  github:
    repo: dmdwebster-collab/payspyre-backend
    branch: staging
  dockerfile_path: Dockerfile
  run_command: alembic upgrade head
```

## Step 5 — Verify the app responds

```bash
# Find the DO-assigned default URL
doctl apps get $STAGING_APP_ID --format DefaultIngress --no-header

# Hit health
curl https://<that-ingress>/health
# Expect: {"status":"healthy"}
```

## Step 6 — Point DNS

Assuming Cloudflare manages `payspyre.com`:

1. Cloudflare dashboard → Select `payspyre.com` → DNS → Records → Add Record:
   - **Type:** `CNAME`
   - **Name:** `api.staging`  (full record becomes `api.staging.payspyre.com`)
   - **Target:** the DO-assigned `<ingress>.ondigitalocean.app` URL
   - **Proxy status:** **DNS only** (orange cloud OFF) — DO terminates TLS itself
   - **TTL:** Auto

2. Add the domain to the DO app:

```bash
doctl apps update $STAGING_APP_ID --spec .do/app-staging.yaml-with-domain.yaml
```

Or via console: App → Settings → Domains → Add Domain → `api.staging.payspyre.com` → wait for the cert to issue (~2 min).

3. Verify:

```bash
dig +short api.staging.payspyre.com   # should resolve to DO
curl https://api.staging.payspyre.com/health
```

## Step 7 — Production (once staging is fully green)

Repeat steps 1–6 with `app.yaml` (not `app-staging.yaml`) and:
- `branch: main`
- Apex domain: `api.payspyre.com`
- Production secrets (not test keys)
- `instance_size_slug` upgraded from `basic-xxs` → at minimum `basic-s` (current spec under-sizes prod)
- `instance_count: 2` for redundancy (current spec has 1 — single instance = single point of failure)

---

## Known gaps in the current spec (file in follow-up PRs)

1. **No PRE_DEPLOY migration job** — migrations run manually today, which violates "Migrations checked into git and applied automatically" from the production-readiness audit Section 5.
2. **`instance_count: 1`** for production — no load balancing, no rolling deploys without downtime. Audit Section "Load Balancing & Scaling" = ❌.
3. **No autoscaling block** — fine for staging, not for production. Add:
   ```yaml
   autoscaling:
     min_instance_count: 2
     max_instance_count: 4
     metrics:
       cpu:
         percent: 70
   ```
4. **`basic-xxs`** is 512 MB / 1 vCPU — under-sized for a FastAPI app with SQLAlchemy + Sentry + structlog. Recommend `basic-s` (1 GB / 1 vCPU) minimum for production.
5. **No domain block in YAML** — domains are configured via console, which means infra-as-code drift. Add a `domains:` section to both YAMLs.

---

## DNS record summary (Cloudflare)

| Record | Type | Target | Proxy |
|---|---|---|---|
| `api.staging.payspyre.com` | CNAME | `<staging-app>.ondigitalocean.app` | DNS only |
| `api.payspyre.com` | CNAME | `<prod-app>.ondigitalocean.app` | DNS only |

---

## Troubleshooting

- **Deploy stuck on "Building"** → check `doctl apps logs $APP_ID --type build`
- **Container won't start** → likely a missing secret. `doctl apps logs $APP_ID --type run` will show the FastAPI startup error.
- **`/health` returns 502** → migrations probably didn't run. Connect via console and run `alembic current` to confirm head.
- **DNS not resolving after 10 min** → confirm Cloudflare proxy is **OFF** for the record. DO needs to see direct traffic to issue the cert.
