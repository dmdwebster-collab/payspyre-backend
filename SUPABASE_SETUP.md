# PaySpyre v2 — Supabase Database Setup Required

## Current Status

**Issue:** `.env` file contains localhost PostgreSQL connection, but:
- No PostgreSQL server is running on localhost:5432
- PaySpyre v2 spec requires Supabase PostgreSQL
- Migrations (PR P1) have not been applied to any database yet

**What This Means:** The database connection needs to be configured before migrations can run.

---

## Quick Setup (2 Options)

### Option A: Use Supabase Cloud (Recommended for Development)

1. **Go to Supabase Dashboard:** https://supabase.com/dashboard
2. **Create/select your project** — you should already have one per the spec
3. **Get your credentials:**
   - Go to Settings → Database
   - Find your **Project Reference** (from URL or dashboard)
   - Note your **database password** (set when project was created)

4. **Configure locally** — run the PowerShell setup script:
   ```powershell
   .\scripts\setup-supabase-env.ps1
   ```
   
   This will prompt for your PROJECT_REF and password, then create `.env.local`.

5. **Verify connection:**
   ```powershell
   .\venv\Scripts\Activate.ps1
   alembic current
   ```

### Option B: Use Local PostgreSQL (For Testing Only)

If you want to test migrations locally without Supabase:

1. **Install PostgreSQL** (if not already installed):
   - Windows: https://www.postgresql.org/download/windows/
   - Default port: 5432
   - Default user: postgres
   - Set a password during installation

2. **Create the database:**
   ```powershell
   # Using psql command line
   createdb -U postgres payspyre
   ```

3. **Update `.env`** with your local PostgreSQL credentials:
   ```env
   DATABASE_URL=postgresql+psycopg2://postgres:YOUR_PASSWORD@localhost:5432/payspyre
   ```

4. **Activate virtualenv and run migrations:**
   ```powershell
   .\venv\Scripts\Activate.ps1
   alembic upgrade head
   ```

---

## What Happens Next

Once configured, the following will execute automatically:

1. **Alembic current** — shows current migration version
2. **Alembic upgrade head** — applies all pending migrations (015-021)
3. **Schema verification** — confirms all 7 platform tables created
4. **WORM constraint test** — validates `platform_events` append-only protection

---

## Migration Details (PR P1)

| Migration | Table | Purpose |
|-----------|-------|---------|
| 015 | `platform_patients` | Canonical patient record |
| 016 | `platform_patient_fields` | Source-tagged field audit trail |
| 017 | `platform_credit_products` | Configurable verification matrix |
| 018 | `platform_credit_applications` | Applications with co-applicant support |
| 019 | `platform_verifications` | Unified verification tracking |
| 020 | `platform_consents` | Granular per-purpose consent |
| 021 | `platform_events` | Append-only audit log (WORM protected) |

**Total:** 7 tables, 7 models, comprehensive tests

---

## Security Notes

- **Never commit `.env.local`** with real credentials (it's in `.gitignore`)
- **WORM constraint** on `platform_events` prevents UPDATE/DELETE via database trigger
- **Connection string format:** Use `postgresql+psycopg2://` for SQLAlchemy compatibility

---

## Need Help?

If you don't have a Supabase project yet:
1. Go to https://supabase.com
2. Click "New Project"
3. Choose a region (closest to you)
4. Set a strong database password (save it securely!)
5. Wait ~2 minutes for provisioning
6. Run the PowerShell setup script above
