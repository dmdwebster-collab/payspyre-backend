# PaySpyre Authentication System

## Overview
Complete authentication and authorization system with JWT tokens, role-based access control (RBAC), session management, and API key support.

## Features
- User registration with email verification
- Login/logout with JWT access and refresh tokens
- Password reset flow
- Role-based access control (admin, staff, patient, vendor)
- Session management with device tracking
- API key management for vendor integrations
- Rate limiting
- Password strength validation

## User Roles
- **admin**: Full system access, user/role management
- **staff**: Read/create/update access to loans, KYC, funding
- **patient**: Read/create access to own loans and KYC
- **vendor**: Limited read access to vendor-related resources

## API Endpoints

### Authentication
- `POST /api/v1/auth/register` - Register new user
- `POST /api/v1/auth/login` - Login (returns access + refresh tokens)
- `POST /api/v1/auth/refresh` - Refresh access token
- `POST /api/v1/auth/logout` - Logout (invalidate refresh token)
- `POST /api/v1/auth/logout-all` - Revoke all user sessions

### User Management
- `GET /api/v1/auth/me` - Get current user info
- `PATCH /api/v1/auth/me` - Update current user profile
- `POST /api/v1/auth/change-password` - Change password (requires old password)
- `GET /api/v1/auth/users` - List all users (admin only)
- `GET /api/v1/auth/users/{id}` - Get user by ID (admin only)
- `PATCH /api/v1/auth/users/{id}` - Update user (admin only)
- `POST /api/v1/auth/users/{id}/activate` - Activate user (admin only)
- `POST /api/v1/auth/users/{id}/deactivate` - Deactivate user (admin only)

### Password Reset
- `POST /api/v1/auth/forgot-password` - Initiate password reset
- `POST /api/v1/auth/reset-password` - Confirm password reset with token

### Email Verification
- `POST /api/v1/auth/verify-email` - Verify email address with token

### Sessions
- `GET /api/v1/auth/sessions` - List user sessions
- `DELETE /api/v1/auth/sessions/{id}` - Revoke specific session

### API Keys
- `GET /api/v1/auth/api-keys` - List user API keys
- `POST /api/v1/auth/api-keys` - Create new API key
- `DELETE /api/v1/auth/api-keys/{id}` - Revoke API key

## Database Schema

### Tables
- `users` - User accounts
- `roles` - Role definitions
- `permissions` - Permission definitions (resource + action)
- `role_permissions` - Role-permission mapping
- `user_roles` - User-role mapping
- `sessions` - User login sessions (refresh tokens)
- `api_keys` - API keys for vendor integrations

### Indexes
- User email (unique)
- Session refresh token (unique)
- API key hash (unique)
- Various composite indexes for RBAC queries

## Configuration

Required environment variables:
```
JWT_SECRET_KEY=your_secret_key_here
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=30
JWT_REFRESH_TOKEN_EXPIRE_DAYS=7
RESEND_API_KEY=resend_api_key
RESEND_FROM_EMAIL=noreply@payspyre.com
```

Rate limiting (optional, enabled by default):
```
RATE_LIMIT_ENABLED=true
RATE_LIMIT_AUTH_REQUESTS=5
RATE_LIMIT_AUTH_WINDOW=60
```

## Setup

1. Run migrations:
```bash
alembic upgrade head
```

2. Seed admin user:
```bash
python scripts/seed_admin.py
```

Default admin credentials:
- Email: `admin@payspyre.com`
- Password: `Admin123!ChangeMe`

**Important**: Change the admin password immediately after first login.

## Usage Examples

### Register a new user
```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "user@example.com",
    "password": "SecurePass123",
    "first_name": "John",
    "last_name": "Doe",
    "roles": ["patient"]
  }'
```

### Login
```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=user@example.com&password=SecurePass123"
```

### Use access token
```bash
curl http://localhost:8000/api/v1/auth/me \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```

### Create API key
```bash
curl -X POST http://localhost:8000/api/v1/auth/api-keys \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Production API Key",
    "scopes": ["read", "write"],
    "expires_in_days": 90
  }'
```

### Use API key
```bash
curl http://localhost:8000/api/v1/loans \
  -H "X-API-Key: YOUR_API_KEY"
```

## Security Features

1. **Password Hashing**: bcrypt with automatic salt
2. **JWT Tokens**: HS256 signed access tokens (30 min expiry)
3. **Refresh Tokens**: Secure random tokens (7 day expiry)
4. **Session Tracking**: IP address, user agent, device info
5. **Rate Limiting**: Configurable per-endpoint limits
6. **API Key Hashing**: bcrypt hashed keys stored in database
7. **Email Verification**: Required for account activation
8. **Password Reset**: Time-limited reset tokens (1 hour)

## Testing

Run auth tests:
```bash
pytest tests/test_auth.py -v
```

Run all tests with coverage:
```bash
pytest --cov=app --cov-report=html
```

## Middleware

### `get_current_user`
Authenticates JWT access token, returns User object.

### `get_current_active_user`
Same as above, but verifies user is active.

### `require_roles(*roles)`
Decorator to require specific roles.

### `require_permission(resource, action)`
Decorator to require specific permission.

### `rate_limit(limiter, identifier)`
Rate limiting middleware.

## Email Service

Uses Resend API for transactional emails. Templates included for:
- Email verification
- Password reset

To configure:
1. Set `RESEND_API_KEY` in environment
2. Set `RESEND_FROM_EMAIL` in environment
3. Update `CORS_ORIGINS` for verification/reset URLs

## Token Payload Structure

```python
{
    "sub": "user_uuid",  # User ID
    "exp": 1234567890,   # Expiration timestamp
    "type": "access"     # Token type
}
```