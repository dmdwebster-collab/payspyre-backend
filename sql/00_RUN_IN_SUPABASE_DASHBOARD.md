# PaySpyre v2 — PR P1 Migration Execution Guide

## Status: Network Blocked - Manual Execution Required

**Issue:** Port 5432 (PostgreSQL) is blocked by network policy, preventing direct `alembic upgrade head` execution.

**Workaround:** SQL files generated for manual execution in Supabase Dashboard.

---

## Quick Start (Manual Execution)

### Step 1: Access Supabase Dashboard SQL Editor

1. Go to: https://supabase.com/dashboard/project/dqdjkgfzhhysbzlqcdsl
2. Click: **SQL Editor** (left sidebar)
3. Click: **New Query** button

### Step 2: Execute Migrations (In Order)

Copy and paste each SQL file content into the SQL Editor and execute:

1. **sql/015_create_platform_patients.sql** (Creates `platform_patients` table)
2. **sql/016_create_platform_patient_fields.sql** (Creates `platform_patient_fields` table)
3. **sql/017_create_platform_credit_products.sql** (Creates `platform_credit_products` table)
4. **sql/018_create_platform_credit_applications.sql** (Creates `platform_credit_applications` table)
5. **sql/019_create_platform_verifications.sql** (Creates `platform_verifications` table)
6. **sql/020_create_platform_consents.sql** (Creates `platform_consents` table + FK to verifications)
7. **sql/021_create_platform_events.sql** (Creates `platform_events` table + WORM trigger)

### Step 3: Verify Tables Created

```sql
-- Run this in Supabase SQL Editor to verify all 7 tables exist:
SELECT table_name 
FROM information_schema.tables 
WHERE table_schema = 'public' 
  AND table_name LIKE 'platform_%'
ORDER BY table_name;
```

Expected output:
```
platform_credit_applications
platform_consents
platform_events
panel_patient_fields
platform_patients
platform_credit_products
platform_verifications
```

### Step 4: Test WORM Constraint

```sql
-- Test 1: Create a test event (should work)
INSERT INTO platform_events (event_type, actor, payload)
VALUES ('test.worm', 'system', '{"test": true}')
RETURNING id;

-- Test 2: Try UPDATE (should FAIL with trigger error)
UPDATE platform_events 
SET event_type = 'tampered' 
WHERE event_type = 'test.worm';

-- Test 3: Try DELETE (should FAIL with trigger error)
DELETE FROM platform_events 
WHERE event_type = 'test.worm';
```

**Expected:** Tests 2 & 3 should fail with error: "Cannot modify platform_events table - append-only audit trail (WORM)"

---

## Network Issue Details

**Diagnosis:**
- ✓ DNS resolves: `dqdjkgfzhhysbzlqcdsl.supabase.co`
- ✓ HTTP/HTTPS accessible (ports 80, 443)
- ✗ PostgreSQL blocked (ports 5432, 6543)

**Likely Cause:** Corporate firewall or Windows Firewall blocking outbound PostgreSQL connections.

**Potential Solutions:**
1. **Use VPN** if available
2. **Windows Firewall:** Add outbound rule for `python.exe` or `psql.exe` to allow port 5432
3. **Corporate policy:** Request network exception for Supabase PostgreSQL
4. **Alternative:** Continue with manual SQL execution (current approach)

---

## Files Generated

| File | Size | Description |
|------|------|-------------|
| `sql/015_create_platform_patients.sql` | 11.9 KB | Patient profile table with SIN encryption |
| `sql/016_create_platform_patient_fields.sql` | 10.7 KB | Source-tagged field audit trail |
| `sql/017_create_platform_credit_products.sql` | 8.6 KB | Credit product configuration |
| `sql/018_create_platform_credit_applications.sql` | 5.4 KB | Applications with co-applicant support |
| `sql/019_create_platform_verifications.sql` | 3.7 KB | Unified verification tracking |
| `sql/020_create_platform_consents.sql` | 2.1 KB | Granular per-purpose consent |
| `sql/021_create_platform_events.sql` | 115 B | Append-only event log + WORM trigger |

**Total:** 7 migrations, ~42 KB of SQL

---

## Next Steps After Manual Execution

Once migrations are applied via Supabase Dashboard:

1. **Re-run `alembic current`** - should show revision `021_create_platform_events`
2. **Run test suite:** `pytest tests/test_platform_001_schema.py`
3. **Test `authenticated` role permissions** (see below)
4. **Proceed to PR P2** (Patient Profile Service)

---

## Testing `authenticated` Role Permissions

After migrations are applied, run these commands to verify `authenticated` role grants:

```sql
-- Check if 'authenticated' role exists
SELECT rolname FROM pg_roles WHERE rolname = 'authenticated';

-- Check grants on platform_* tables
SELECT grantee, privilege, table_name 
FROM information_schema.role_table_grants 
WHERE table_name LIKE 'platform_%'
  AND grantee = 'authenticated'
ORDER BY table_name, privilege;

-- If authenticated lacks INSERT permission on platform_events:
GRANT INSERT ON platform_events TO authenticated;

-- If authenticated lacks SELECT on platform_* tables:
GRANT SELECT ON ALL TABLES IN SCHEMA public TO authenticated;
```

---

## Commit Status

**Ready to commit:** `sql/` directory with 7 migration SQL files

**Next commit:** After manual execution confirmation, commit migration completion with test results.
