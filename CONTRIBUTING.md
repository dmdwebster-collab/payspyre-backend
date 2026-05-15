# Contributing to PaySpyre Backend

Thank you for your interest in contributing to PaySpyre! This document outlines the development workflow, CI requirements, and branch protection rules.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Development Workflow](#development-workflow)
- [CI/CD Requirements](#cicd-requirements)
- [Testing Locally](#testing-locally)
- [Branch Protection Rules](#branch-protection-rules)
- [Troubleshooting CI Failures](#troubleshooting-ci-failures)
- [Adding New Contributors](#adding-new-contributors)

---

## Prerequisites

### Required Tools

- **Python 3.12+** - Core runtime
- **PostgreSQL 16+** - Development database
- **Git** - Version control
- **GitHub CLI** (optional) - For workflow management

### Python Dependencies

```bash
# Install core dependencies
pip install -e .

# Install development dependencies
pip install pytest pytest-cov pytest-asyncio
pip install httpx passlib[bcrypt] python-jose[cryptography]
pip install psycopg2-binary alembic sqlalchemy
```

### Environment Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/your-org/payspyre-backend.git
   cd payspyre-backend
   ```

2. **Create a virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Configure environment variables:**
   ```bash
   cp .env.example .env
   # Edit .env with your local settings
   ```

4. **Set up the database:**
   ```bash
   # Create database
   createdb payspyre_dev

   # Run migrations
   alembic upgrade head
   ```

---

## Development Workflow

### 1. Branch Naming Convention

Use descriptive branch names with prefixes:
- `feature/` - New features
- `fix/` - Bug fixes
- `refactor/` - Code refactoring
- `docs/` - Documentation changes
- `test/` - Test additions or updates

**Examples:**
```bash
feature/add-stripe-webhook-handler
fix/bcrypt-password-validation
refactor/optimize-database-queries
docs/update-api-documentation
test/add-migration-coverage
```

### 2. Create a Feature Branch

```bash
# Ensure you're on main and it's up to date
git checkout main
git pull origin main

# Create your feature branch
git checkout -b feature/your-feature-name
```

### 3. Make Your Changes

1. Write code following the project's coding standards
2. Add tests for new functionality
3. Update documentation if needed
4. Run tests locally (see [Testing Locally](#testing-locally))

### 4. Commit Your Changes

Use clear, descriptive commit messages:

```bash
# Good
git commit -m "feat(stripe): add webhook handler for payment.success events"

# Bad
git commit -m "update stuff"
```

**Commit Message Format:**
```
type(scope): description

[optional body]

[optional footer]
```

**Types:**
- `feat` - New feature
- `fix` - Bug fix
- `docs` - Documentation change
- `style` - Code style change (formatting, etc.)
- `refactor` - Code refactoring
- `test` - Adding or updating tests
- `chore` - Maintenance task

### 5. Push to GitHub

```bash
git push origin feature/your-feature-name
```

### 6. Create a Pull Request

1. Go to GitHub and click "Compare & pull request"
2. Fill in the PR template
3. Link related issues (e.g., "Fixes #123")
4. Request review from a team member

**PR Title Format:**
```
[Type] Brief description of changes
```

**Examples:**
```
[Feature] Add Stripe webhook handler
[Fix] Resolve bcrypt password validation error
[Docs] Update CI/CD documentation
```

---

## CI/CD Requirements

### Overview

PaySpyre uses GitHub Actions for continuous integration. Every PR and push to `main` triggers two workflows:

1. **Tests** (`.github/workflows/tests.yml`)
   - Runs full test suite with PostgreSQL
   - Enforces zero-error gate
   - Uploads coverage reports

2. **Migrations** (`.github/workflows/migrations.yml`)
   - Validates migration integrity
   - Checks for schema drift
   - Blocks stub migrations
   - Verifies reversibility

### Required Status Checks

Before merging to `main`, all PRs must pass:

- ✅ `test` (from tests.yml)
- ✅ `migrations` (from migrations.yml)

These checks are enforced by branch protection rules (see [Branch Protection Rules](#branch-protection-rules)).

---

## Testing Locally

Before pushing, run tests locally to catch issues early:

### Full Test Suite

```bash
# Run all tests
pytest --tb=short -q

# Run with coverage
pytest --cov=app --cov-report=html

# Run specific test file
pytest tests/test_auth.py -v

# Run specific test
pytest tests/test_auth.py::test_login -v
```

### Migration Tests

```bash
# Run migration integrity tests
pytest tests/migrations/ -v

# Check for schema drift
python scripts/check_migration_drift.py

# Check for stub migrations
python scripts/check_no_stub_migrations.py
```

### Test Database Setup

Tests use a separate PostgreSQL database:

```bash
# Create test database
createdb payspyre_test

# Set environment variables
export TEST_DATABASE_URL="postgresql+psycopg2://user:pass@localhost:5432/payspyre_test"
```

### Common Test Issues

**Issue:** Tests fail with `ValueError: password cannot be longer than 72 bytes`

**Solution:** This is a known bcrypt limitation. API keys or session tokens exceeding 72 bytes need to be truncated before hashing:

```python
# Bad
hashed = bcrypt.hash(very_long_api_key)

# Good
hashed = bcrypt.hash(very_long_api_key[:72])
```

**Issue:** Tests fail with `Foreign key violation`

**Solution:** Ensure fixtures create dependent entities in the correct order using `db_session.flush()`:

```python
vendor = Vendor(id=uuid4(), ...)
db_session.add(vendor)

application = LoanApplication(vendor_id=vendor.id, ...)
db_session.add(application)

db_session.flush()  # Ensure vendor exists before creating dependent records
```

---

## Branch Protection Rules

PaySpyre enforces strict branch protection on `main` to maintain code quality.

### Protected Branch: `main`

**Settings (configured in GitHub → Settings → Branches):**

- ✅ **Require status checks to pass before merging**
- ✅ **Require branches to be up to date before merging**
- ✅ **Required status checks:**
  - `test` (from tests.yml)
  - `migrations` (from migrations.yml)
- ❌ **Do NOT allow bypassing the above settings**

### What This Means

1. **Direct commits to `main` are blocked** - All changes must go through PRs
2. **CI must pass** - Both `test` and `migrations` jobs must succeed
3. **Branches must be up to date** - Rebase `main` into your branch if it falls behind
4. **No bypassing** - Even admins cannot bypass these rules

### Merging a PR

Once CI passes:

```bash
# Update your branch with latest main
git checkout main
git pull origin main
git checkout feature/your-feature-name
git rebase main

# Push the rebased branch
git push origin feature/your-feature-name --force-with-lease
```

Then click "Merge pull request" in GitHub.

---

## Troubleshooting CI Failures

### CI Workflow Fails to Start

**Symptoms:** Workflow doesn't appear in the PR checks tab

**Possible Causes:**
1. `.github/workflows/` files not pushed
2. YAML syntax error in workflow file
3. Missing GitHub Actions permissions

**Solutions:**
1. Verify workflow files exist:
   ```bash
   ls -la .github/workflows/
   ```

2. Validate YAML syntax:
   ```bash
   python -c "import yaml; yaml.safe_load(open('.github/workflows/tests.yml', encoding='utf-8'))"
   ```

3. Check Actions permissions:
   - Go to Settings → Actions → General
   - Ensure "Allow all actions and reusable workflows" is selected

### Tests Fail Only in CI (Not Locally)

**Common Causes:**

1. **Environment differences**
   - CI uses PostgreSQL 16 on Ubuntu
   - Local may use different PostgreSQL version or OS

2. **Missing environment variables**
   - CI sets `DATABASE_URL`, `JWT_SECRET_KEY`, etc.
   - Local may not have these

**Solutions:**
```bash
# Replicate CI environment locally
docker run -d -p 5432:5432 \
  -e POSTGRES_USER=payspyre \
  -e POSTGRES_PASSWORD=dev123 \
  -e POSTGRES_DB=payspyre_test \
  postgres:16

export DATABASE_URL="postgresql+psycopg2://payspyre:dev123@localhost:5432/payspyre_test"
pytest --tb=short -q
```

### Migration Tests Fail with Drift Detected

**Symptoms:** `check_migration_drift.py` fails in CI

**Meaning:** SQLAlchemy models don't match the database schema

**Solution:**
```bash
# Generate a new migration to capture the drift
alembic revision --autogenerate -m "fix schema drift"

# Review the generated migration
git diff HEAD~1 alembic/versions/

# If correct, apply and commit
alembic upgrade head
git add alembic/versions/
git commit -m "fix(migrations): capture schema drift"
```

### Migration Tests Fail with Stub Detected

**Symptoms:** `check_no_stub_migrations.py` fails in CI

**Meaning:** A migration file contains placeholder code

**Solution:**
```bash
# Find the stub migration
grep -r "TODO\|FIXME\|STUB\|NotImplementedError" alembic/versions/

# Complete the migration logic
# Remove TODO/FIXME comments
# Replace pass/NotImplementedError with actual migration code
```

### Zero-Error Gate Blocks Merge

**Symptoms:** Tests pass but `Enforce zero errors` step fails

**Meaning:** Test output contains the word "error"

**Solution:**
```bash
# Run tests locally and check for errors
pytest --tb=no -q 2>&1 | grep -i error

# Common false positives:
# - "ValueError: password cannot be longer than 72 bytes"
# - "KeyError: 'user_id'"
# - "AssertionError: expected 200, got 400"

# Fix the underlying test issues
pytest --tb=short -v
```

---

## Adding New Contributors

### For Repo Admins

1. **Invite the contributor:**
   - Go to Settings → Collaborators
   - Click "Add people"
   - Enter their GitHub username
   - Select "Write" permission

2. **Grant access to external services:**
   - Stripe dashboard (if working on payment features)
   - Database access (read-only for staging)
   - API keys for development environment

3. **Onboard the contributor:**
   - Share this document
   - Provide access to internal documentation
   - Add them to relevant Slack channels
   - Schedule a pair programming session

### For New Contributors

1. **Set up your development environment** (see [Prerequisites](#prerequisites))
2. **Introduce yourself** in the team chat
3. **Pick a good first issue:**
   - Look for issues tagged `good first issue`
   - Start with documentation or test improvements
   - Avoid complex refactoring or database migrations initially
4. **Ask questions:**
   - Use Slack for quick questions
   - Create GitHub issues for bugs or feature requests
   - Request review before pushing to `main`

---

## Code Review Guidelines

### For Reviewers

1. **Review the code, not the person**
2. **Focus on:**
   - Business logic correctness
   - Security implications
   - Test coverage
   - Code clarity and maintainability
3. **Provide constructive feedback:**
   - Explain why a change is needed
   - Suggest improvements, don't just criticize
   - Offer to pair program if unclear

### For Authors

1. **Keep PRs small and focused**
2. **Respond to all review comments**
3. **Make requested changes or explain why not**
4. **Thank your reviewers**

---

## Additional Resources

- [PaySpyre Security Documentation](./SECURITY.md)
- [Database Hygiene Report](./DATABASE_HYGIENE_REPORT.md)
- [Task Summary](./TASK_SUMMARY.md)
- [Error Inventory](./error_inventory.md)
- [Fixture Dependency Order](./fixture_dependency_order.md)

---

## Getting Help

If you're stuck:

1. **Check existing issues** - Your question may already be answered
2. **Create a new issue** - For bugs or feature requests
3. **Ask in Slack** - For quick questions
4. **Request a code review** - If unsure about approach

---

## License

By contributing to PaySpyre, you agree that your contributions will be licensed under the same license as the project.

---

**Thank you for contributing to PaySpyre!** 🚀
