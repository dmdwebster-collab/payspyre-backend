# Repo Admin Quick Start - Branch Protection

**For:** GitHub repository administrators
**Goal:** Enable branch protection rules on the `main` branch
**Time:** 5-10 minutes

---

## TL;DR - One Minute Setup

1. Go to **Settings → Branches**
2. Click **Add rule** (or edit `main`)
3. Enter branch name: `main`
4. Enable:
   - ✅ Require status checks to pass before merging
   - ✅ Require branches to be up to date before merging
   - ✅ Select: `test` and `migrations`
5. Click **Create** (or **Save changes**)

**Done!** 🎉

---

## Verification

### Test It Works

```bash
# Create test branch
git checkout -b test-branch-protection
echo "# test" >> README.md
git add README.md
git commit -m "test: verify branch protection"
git push origin test-branch-protection

# Open PR on GitHub
# Verify "Merge pull request" button is disabled until CI passes
```

### Clean Up

```bash
# After verification
git checkout main
git pull origin main
git branch -d test-branch-protection
git push origin --delete test-branch-protection
```

---

## What This Does

**Blocks:**
- ❌ Direct commits to `main`
- ❌ Merging PRs with failing tests
- ❌ Merging PRs that are behind `main`

**Requires:**
- ✅ All PRs must pass CI
- ✅ All PRs must be up-to-date with `main`
- ✅ Both `test` and `migrations` jobs must pass

---

## Emergency Bypass

**⚠️ Only for production outages or security fixes**

1. Go to **Settings → Branches**
2. Edit `main` rule
3. Uncheck "Require status checks to pass"
4. Save
5. Make emergency fix
6. **Re-enable immediately**

---

## Need Help?

- **Full guide:** See [BRANCH_PROTECTION.md](./BRANCH_PROTECTION.md)
- **Contributor docs:** See [CONTRIBUTING.md](./CONTRIBUTING.md)
- **Issues:** Create a GitHub issue or ask in Slack

---

**Status:** ✅ Ready to enable
**Pre-requisites:** CI workflows must exist and run successfully
