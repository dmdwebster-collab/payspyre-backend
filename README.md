# PaySpyre Backend API

FastAPI backend for PaySpyre Financial — Canadian dental patient-financing lender.

## Quick Start

```bash
# 1. Copy environment
cp .env.example .env

# 2. Start with Docker (includes DB)
docker-compose up -d

# 3. Run migrations
docker-compose exec api alembic upgrade head

# 4. API docs at http://localhost:8000/docs
```

**Windows PowerShell:**
```powershell
Copy-Item .env.example .env
docker-compose up -d
docker-compose exec api alembic upgrade head
```

## Stack

FastAPI · PostgreSQL 16 · SQLAlchemy 2 · Alembic · pytest