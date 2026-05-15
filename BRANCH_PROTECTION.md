# Branch Protection Setup Checklist

**Purpose:** This checklist guides repo admins through enabling branch protection rules for the PaySpyre backend repository.

**Target:** `main` branch protection in GitHub

---

## Pre-Setup Checklist

### Prerequisites

- [ ] You have **admin permissions** on the GitHub repository
- [ ] CI workflows are pushed to GitHub:
  - [ ] `.github/workflows/tests.yml` exists
  - [ ] `.github/workflows/migrations.yml` exists
- [ ] At least one successful CI run has completed
- [ ] You understand the implications of branch protection (see [Impact Assessment](#impact-assessment))

### Verify Workflows Exist

```bash
# Check workflow files exist locally
ls -la .github/workflows/

# Expected output:
# tests.yml
# migrations.yml
```

### Verify Workflows Run on GitHub

```bash
# List workflows (requires gh CLI and authentication)
gh workflow list

# Expected output:
# Tests    active  77 minutes ago
# Migrations  active  77 minutes ago
```

**If workflows don't appear:**
1. Push the workflow files to GitHub:
   ```bash
   git add .github/workflows/
   git commit -m "ci: add GitHub Actions workflows"
   git push origin main
   ```
2. Wait for GitHub to register the workflows (may take 1-2 minutes)
3. Verify again with `gh workflow list`

---

## Branch Protection Configuration

### Step 1: Navigate to Branch Settings

1. Go to the repository on GitHub
2. Click **Settings** tab
3. Click **Branches** in the left sidebar
4. Click **Add rule** (or edit existing `main` rule)

### Step 2: Configure Branch Rule

**Branch name pattern:** `main`

**Settings:**

✅ **Require status checks to pass before merging**
- Check this box

✅ **Require branches to be up to date before merging**
- Check this box
- This ensures PRs are rebased on latest `main` before merging

✅ **Required status checks**
- Search for and select:
  - `test` (from Tests workflow)
  - `migrations` (from Migrations workflow)

❌ **Do NOT enable:**
- ❌ Require conversation resolution before merging
- ❌ Require signed commits
- ❌ Require linear history
- ❌ Restrict who can push to matching branches

**Restrictions:**
- ❌ Do NOT restrict who can push (keep open for all collaborators)

**Allow bypasses:**
- ❌ **Uncheck** "Allow specified actors to bypass required status checks"
- This ensures even admins must pass CI

### Step 3: Save the Rule

1. Click **Create** (or **Save changes**)
2. Verify the rule appears in the list with `main` as the branch pattern

---

## Post-Setup Verification

### Test Branch Protection Works

1. **Create a test branch:**
   ```bash
   git checkout -b test-branch-protection
   ```

2. **Make a trivial change:**
   ```bash
   echo "# test" >> README.md
   git add README.md
   git commit -m "test: verify branch protection"
   git push origin test-branch-protection
   ```

3. **Open a PR:**
   - Go to GitHub and create a PR targeting `main`
   - Verify you see:
     - ⏳ `test` check (pending → running → passed/failed)
     - ⏳ `migrations` check (pending → running → passed/failed)
   - Verify the "Merge pull request" button is **disabled** until both checks pass

4. **Attempt to merge before CI passes:**
   - Try clicking "Merge pull request" while CI is running
   - Verify you see: "Required status checks are expected"

5. **Wait for CI to pass, then merge:**
   - Once both checks show ✅
   - Click "Merge pull request"
   - Verify merge succeeds

### Clean Up Test PR

```bash
# Delete test branch
git checkout main
git pull origin main
git branch -d test-branch-protection
git push origin --delete test-branch-protection
```

---

## Impact Assessment

### What Branch Protection Prevents

**Blocked Actions:**
- ❌ Direct commits to `main` (must use PRs)
- ❌ Merging PRs with failing CI
- ❌ Merging outdated PRs (must rebase `main` first)
- ❌ Bypassing CI checks (even for admins)

**Still Allowed:**
- ✅ Creating PRs from any branch
- ✅ Force-pushing to non-`main` branches
- ✅ Merging PRs after CI passes
- ✅ Using squash merge, merge commit, or rebase merge

### Workflow Changes

**Before Branch Protection:**
```bash
git checkout main
git pull
git commit -m "hotfix"
git push origin main  # ❌ Now blocked
```

**After Branch Protection:**
```bash
git checkout -b hotfix/critical-bug
# Make changes
git commit -m "fix: resolve critical bug"
git push origin hotfix/critical-bug
# Open PR → wait for CI → merge
```

### Emergency Bypass Procedure

**⚠️ Use only in emergencies (production outage, security fix)**

1. Go to Settings → Branches
2. Edit `main` rule
3. Temporarily uncheck "Require status checks to pass"
4. Save changes
5. Make the emergency fix
6. Re-enable the rule immediately
7. Document why the bypass was necessary

**Post-Bypass:**
```bash
git commit -m "fix(emergency): bypassed CI - reason: production outage"
# Create issue to investigate why CI was blocking
gh issue create --title "Investigate emergency bypass on $(date +%Y-%m-%d)" \
  --body "Bypassed branch protection for urgent fix. Review CI/test failures."
```

---

## Troubleshooting

### Issue: Can't Find Status Checks to Require

**Symptom:** `test` or `migrations` don't appear in the status checks list

**Possible Causes:**
1. Workflows haven't run yet
2. Workflows failed to register
3. Status check name mismatch

**Solutions:**

1. **Trigger a workflow run:**
   ```bash
   gh workflow run tests.yml
   gh workflow run migrations.yml
   ```

2. **Wait for workflow to complete**, then refresh the branch settings page

3. **Verify status check names:**
   - Go to Actions tab
   - Click on a recent workflow run
   - Note the exact job names (e.g., `test`, `migrations`)

### Issue: PR Stuck "Required status checks are expected"

**Symptom:** Checks are passing but merge button remains disabled

**Possible Causes:**
1. Branch is behind `main`
2. Status checks haven't completed for the latest commit
3. Cached status checks from old commits

**Solutions:**

1. **Update branch with latest `main`:**
   ```bash
   git checkout main
   git pull origin main
   git checkout your-feature-branch
   git rebase main
   git push origin your-feature-branch --force-with-lease
   ```

2. **Wait for CI to complete on the new commit**

3. **Refresh the PR page**

### Issue: Admin Can't Bypass Status Checks

**Symptom:** Admin tries to merge but is blocked by CI

**This is expected behavior!** Branch protection rules apply to everyone, including admins.

**If you absolutely must bypass:** Follow the [Emergency Bypass Procedure](#emergency-bypass-procedure)

---

## Monitoring and Maintenance

### Regular Checks

**Weekly:**
- [ ] Verify no PRs are stuck with failing CI
- [ ] Review CI workflow performance (are they running slowly?)
- [ ] Check for flaky tests (tests that fail intermittently)

**Monthly:**
- [ ] Review branch protection rules
- [ ] Update status checks if workflows change
- [ ] Audit bypass history (Settings → Audit log)

### Updating Status Checks

**When to update:**
- Adding a new CI workflow (e.g., `lint.yml`, `security.yml`)
- Renaming existing workflows
- Removing deprecated workflows

**How to update:**
1. Go to Settings → Branches
2. Edit `main` rule
3. Add or remove status checks as needed
4. Save changes
5. Test with a PR

---

## Rollback Procedure

**If branch protection causes critical issues:**

1. **Disable branch protection:**
   - Go to Settings → Branches
   - Delete or disable the `main` rule

2. **Investigate the issue:**
   - Check CI logs
   - Review recent changes
   - Consult team

3. **Re-enable with fixes:**
   - Address the root cause
   - Follow this checklist again
   - Test thoroughly

---

## Completion Checklist

- [ ] Pre-setup prerequisites completed
- [ ] Workflows exist and run successfully
- [ ] Branch rule configured in GitHub settings
- [ ] Required status checks selected (`test`, `migrations`)
- [ ] Bypasses disabled
- [ ] Test PR created and verified
- [ ] Emergency procedure documented
- [ ] Team notified of new rules

---

## Additional Resources

- [GitHub Branch Protection Docs](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches)
- [PaySpyre Contributing Guide](./CONTRIBUTING.md)
- [Task Summary](./TASK_SUMMARY.md)

---

**Last Updated:** 2026-05-15

**Maintained By:** PaySpyre DevOps Team
