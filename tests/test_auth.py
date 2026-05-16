import pytest
from datetime import datetime, timedelta
from uuid import uuid4

from app.models.user import User, Role, UserRoleLink
from app.core.security import get_password_hash, create_access_token


@pytest.fixture(scope="function")
def test_user(db_session):
    """Create a test user."""
    user = User(
        id=uuid4(),
        email="test@example.com",
        password_hash=get_password_hash("TestPassword123"),
        first_name="Test",
        last_name="User",
        is_active=True,
        is_verified=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture(scope="function")
def admin_user(db_session):
    """Create an admin user."""
    user = User(
        id=uuid4(),
        email="admin@example.com",
        password_hash=get_password_hash("AdminPassword123"),
        first_name="Admin",
        last_name="User",
        is_active=True,
        is_verified=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture(scope="function")
def auth_headers(test_user):
    token = create_access_token(data={"sub": str(test_user.id)})
    return {"Authorization": f"Bearer {token}"}


def test_register_user(client):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "newuser@example.com",
            "password": "NewPassword123",
            "first_name": "New",
            "last_name": "User",
            "roles": ["patient"]
        }
    )
    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "newuser@example.com"
    assert data["first_name"] == "New"
    assert data["last_name"] == "User"
    assert data["is_active"] is True
    assert data["is_verified"] is False


def test_register_duplicate_email(client, test_user):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": test_user.email,
            "password": "Password123",
            "first_name": "Test",
            "last_name": "User",
            "roles": ["patient"]
        }
    )
    assert response.status_code == 400


def test_register_weak_password(client):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "weak@example.com",
            "password": "weak",
            "first_name": "Weak",
            "last_name": "User",
            "roles": ["patient"]
        }
    )
    assert response.status_code == 422


def test_login_success(client, test_user):
    response = client.post(
        "/api/v1/auth/login",
        data={
            "username": test_user.email,
            "password": "TestPassword123"
        }
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"
    assert data["user"]["email"] == test_user.email


def test_login_invalid_credentials(client):
    response = client.post(
        "/api/v1/auth/login",
        data={
            "username": "wrong@example.com",
            "password": "WrongPassword123"
        }
    )
    assert response.status_code == 401


def test_login_unverified_user(client, db_session):
    user = User(
        email="unverified@example.com",
        password_hash=get_password_hash("Password123"),
        first_name="Unverified",
        last_name="User",
        is_active=True,
        is_verified=False,
    )
    db_session.add(user)
    db_session.commit()

    response = client.post(
        "/api/v1/auth/login",
        data={
            "username": user.email,
            "password": "Password123"
        }
    )
    assert response.status_code == 403


def test_get_me(client, auth_headers):
    response = client.get("/api/v1/auth/me", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "email" in data
    assert "first_name" in data
    assert "last_name" in data


def test_get_me_unauthorized(client):
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401


def test_update_me(client, auth_headers, test_user):
    response = client.patch(
        "/api/v1/auth/me",
        headers=auth_headers,
        json={
            "first_name": "Updated",
            "phone": "+1234567890"
        }
    )
    assert response.status_code == 200
    data = response.json()
    assert data["first_name"] == "Updated"
    assert data["phone"] == "+1234567890"


def test_change_password(client, auth_headers, test_user):
    response = client.post(
        "/api/v1/auth/change-password",
        headers=auth_headers,
        json={
            "old_password": "TestPassword123",
            "new_password": "NewPassword123"
        }
    )
    assert response.status_code == 200


def test_change_password_wrong_old(client, auth_headers):
    response = client.post(
        "/api/v1/auth/change-password",
        headers=auth_headers,
        json={
            "old_password": "WrongPassword123",
            "new_password": "NewPassword123"
        }
    )
    assert response.status_code == 400


def test_forgot_password(client, test_user):
    response = client.post(
        "/api/v1/auth/forgot-password",
        json={"email": test_user.email}
    )
    assert response.status_code == 200


def test_forgot_password_nonexistent(client):
    response = client.post(
        "/api/v1/auth/forgot-password",
        json={"email": "nonexistent@example.com"}
    )
    assert response.status_code == 200


def test_reset_password(client, db_session):
    user = User(
        email="reset@example.com",
        password_hash=get_password_hash("OldPassword123"),
        first_name="Reset",
        last_name="User",
        is_active=True,
        is_verified=True,
        password_reset_token="test_reset_token",
        password_reset_expires=datetime.utcnow() + timedelta(hours=1),
    )
    db_session.add(user)
    db_session.commit()

    response = client.post(
        "/api/v1/auth/reset-password",
        json={
            "token": "test_reset_token",
            "new_password": "NewPassword123"
        }
    )
    assert response.status_code == 200


def test_verify_email(client, db_session):
    user = User(
        email="verify@example.com",
        password_hash=get_password_hash("Password123"),
        first_name="Verify",
        last_name="User",
        is_active=True,
        is_verified=False,
        email_verification_token="test_verify_token",
    )
    db_session.add(user)
    db_session.commit()

    response = client.post(
        "/api/v1/auth/verify-email",
        json={"token": "test_verify_token"}
    )
    assert response.status_code == 200


def test_logout(client, auth_headers):
    login_response = client.post(
        "/api/v1/auth/login",
        data={
            "username": "test@example.com",
            "password": "TestPassword123"
        }
    )
    refresh_token = login_response.json()["refresh_token"]

    response = client.post(
        "/api/v1/auth/logout",
        headers=auth_headers,
        json={"refresh_token": refresh_token}
    )
    assert response.status_code == 200


def test_logout_all(client, auth_headers):
    response = client.post("/api/v1/auth/logout-all", headers=auth_headers)
    assert response.status_code == 200


def test_get_sessions(client, auth_headers):
    response = client.get("/api/v1/auth/sessions", headers=auth_headers)
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_create_api_key(client, auth_headers):
    response = client.post(
        "/api/v1/auth/api-keys",
        headers=auth_headers,
        json={
            "name": "Test API Key",
            "scopes": ["read", "write"],
            "expires_in_days": 30
        }
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Test API Key"
    assert "key" in data


def test_get_api_keys(client, auth_headers):
    response = client.get("/api/v1/auth/api-keys", headers=auth_headers)
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_revoke_api_key(client, auth_headers):
    create_response = client.post(
        "/api/v1/auth/api-keys",
        headers=auth_headers,
        json={
            "name": "Test API Key",
            "scopes": ["read"]
        }
    )
    api_key_id = create_response.json()["id"]

    response = client.delete(
        f"/api/v1/auth/api-keys/{api_key_id}",
        headers=auth_headers
    )
    assert response.status_code == 200


def test_list_users_as_admin(client, db_session):
    # Create admin role if it doesn't exist
    admin_role = db_session.query(Role).filter(Role.name == "admin").first()
    if not admin_role:
        admin_role = Role(
            name="admin",
            description="Administrator with full access",
            is_active=True,
        )
        db_session.add(admin_role)
        db_session.commit()
        db_session.refresh(admin_role)

    admin = User(
        email="admin@test.com",
        password_hash=get_password_hash("AdminPassword123"),
        first_name="Admin",
        last_name="User",
        is_active=True,
        is_verified=True,
    )
    db_session.add(admin)
    db_session.commit()

    user_role = UserRoleLink(user_id=admin.id, role_id=admin_role.id)
    db_session.add(user_role)
    db_session.commit()

    token = create_access_token(data={"sub": str(admin.id)})
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get("/api/v1/auth/users", headers=headers)
    assert response.status_code == 200
    assert isinstance(response.json(), list)