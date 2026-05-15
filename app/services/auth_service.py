from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import (
    get_password_hash,
    verify_password,
    create_access_token,
    create_refresh_token,
    generate_verification_token,
    generate_reset_token,
    hash_api_key,
    generate_api_key
)
from app.models.user import User, Session, ApiKey, Role, Permission, RolePermission, UserRoleLink
from app.schemas.auth import (
    UserCreate,
    UserUpdate,
    ApiKeyCreate,
    LoginResponse,
    RefreshTokenResponse,
    ApiKeyResponse
)


class AuthService:
    def __init__(self, db: Session):
        self.db = db

    def create_user(self, user_data: UserCreate) -> User:
        existing_user = self.db.query(User).filter(User.email == user_data.email).first()
        if existing_user:
            raise ValueError("Email already registered")

        user = User(
            email=user_data.email,
            password_hash=get_password_hash(user_data.password),
            first_name=user_data.first_name,
            last_name=user_data.last_name,
            phone=user_data.phone,
            is_active=True,
            is_verified=False,
            email_verification_token=generate_verification_token(),
        )

        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)

        for role_name in user_data.roles:
            role = self.db.query(Role).filter(Role.name == role_name).first()
            if role:
                user_role = UserRoleLink(user_id=user.id, role_id=role.id)
                self.db.add(user_role)

        self.db.commit()
        self.db.refresh(user)
        return user

    def authenticate_user(self, email: str, password: str, ip_address: Optional[str] = None,
                          user_agent: Optional[str] = None, device_info: Optional[dict] = None) -> Optional[LoginResponse]:
        user = self.db.query(User).filter(User.email == email).first()
        if not user or not verify_password(password, user.password_hash):
            return None

        if not user.is_active:
            raise ValueError("Account is disabled")

        if not user.is_verified:
            raise ValueError("Email not verified")

        refresh_token, expires_at = create_refresh_token({"sub": str(user.id)})

        session = Session(
            user_id=user.id,
            refresh_token=refresh_token,
            ip_address=ip_address,
            user_agent=user_agent,
            device_info=device_info,
            expires_at=expires_at,
            is_active=True,
        )
        self.db.add(session)

        user.last_login = datetime.utcnow()
        self.db.commit()
        self.db.refresh(user)

        access_token = create_access_token(data={"sub": str(user.id)})

        return LoginResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            user=self._user_to_response(user)
        )

    def refresh_access_token(self, refresh_token: str, ip_address: Optional[str] = None) -> Optional[RefreshTokenResponse]:
        session = self.db.query(Session).filter(
            Session.refresh_token == refresh_token,
            Session.is_active == True
        ).first()

        if not session:
            return None

        if session.expires_at < datetime.utcnow():
            session.is_active = False
            self.db.commit()
            return None

        user = self.db.query(User).filter(User.id == session.user_id).first()
        if not user or not user.is_active or not user.is_verified:
            return None

        new_refresh_token, new_expires_at = create_refresh_token({"sub": str(user.id)})

        session.refresh_token = new_refresh_token
        session.expires_at = new_expires_at
        session.ip_address = ip_address or session.ip_address

        self.db.commit()

        access_token = create_access_token(data={"sub": str(user.id)})

        return RefreshTokenResponse(
            access_token=access_token,
            refresh_token=new_refresh_token
        )

    def logout_user(self, refresh_token: str) -> bool:
        session = self.db.query(Session).filter(Session.refresh_token == refresh_token).first()
        if not session:
            return False

        session.is_active = False
        session.revoked_at = datetime.utcnow()
        self.db.commit()
        return True

    def revoke_all_sessions(self, user_id: UUID) -> int:
        sessions = self.db.query(Session).filter(
            Session.user_id == user_id,
            Session.is_active == True
        ).all()

        for session in sessions:
            session.is_active = False
            session.revoked_at = datetime.utcnow()

        self.db.commit()
        return len(sessions)

    def verify_email(self, token: str) -> bool:
        user = self.db.query(User).filter(User.email_verification_token == token).first()
        if not user:
            return False

        user.is_verified = True
        user.email_verification_token = None
        self.db.commit()
        return True

    def initiate_password_reset(self, email: str) -> Optional[User]:
        user = self.db.query(User).filter(User.email == email).first()
        if not user:
            return None

        user.password_reset_token = generate_reset_token()
        user.password_reset_expires = datetime.utcnow() + timedelta(hours=1)
        self.db.commit()
        self.db.refresh(user)
        return user

    def confirm_password_reset(self, token: str, new_password: str) -> bool:
        user = self.db.query(User).filter(
            User.password_reset_token == token,
            User.password_reset_expires > datetime.utcnow()
        ).first()

        if not user:
            return False

        user.password_hash = get_password_hash(new_password)
        user.password_reset_token = None
        user.password_reset_expires = None
        self.db.commit()

        self.revoke_all_sessions(user.id)
        return True

    def change_password(self, user_id: UUID, old_password: str, new_password: str) -> bool:
        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            return False

        if not verify_password(old_password, user.password_hash):
            return False

        user.password_hash = get_password_hash(new_password)
        self.db.commit()

        self.revoke_all_sessions(user.id)
        return True

    def get_user_by_id(self, user_id: UUID) -> Optional[User]:
        return self.db.query(User).filter(User.id == user_id).first()

    def update_user(self, user_id: UUID, user_data: UserUpdate) -> Optional[User]:
        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            return None

        if user_data.first_name is not None:
            user.first_name = user_data.first_name
        if user_data.last_name is not None:
            user.last_name = user_data.last_name
        if user_data.phone is not None:
            user.phone = user_data.phone

        self.db.commit()
        self.db.refresh(user)
        return user

    def deactivate_user(self, user_id: UUID) -> bool:
        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            return False

        user.is_active = False
        self.db.commit()

        self.revoke_all_sessions(user_id)
        return True

    def activate_user(self, user_id: UUID) -> bool:
        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            return False

        user.is_active = True
        self.db.commit()
        return True

    def create_api_key(self, user_id: UUID, api_key_data: ApiKeyCreate) -> ApiKeyResponse:
        raw_key = generate_api_key()
        key_hash = hash_api_key(raw_key)

        expires_at = None
        if api_key_data.expires_in_days:
            expires_at = datetime.utcnow() + timedelta(days=api_key_data.expires_in_days)

        api_key = ApiKey(
            user_id=user_id,
            key_hash=raw_key,
            name=api_key_data.name,
            scopes=api_key_data.scopes,
            expires_at=expires_at,
            is_active=True,
        )

        self.db.add(api_key)
        self.db.commit()
        self.db.refresh(api_key)

        return ApiKeyResponse(
            id=api_key.id,
            key=raw_key,
            name=api_key.name,
            scopes=api_key.scopes,
            is_active=api_key.is_active,
            last_used=api_key.last_used,
            expires_at=api_key.expires_at,
            created_at=api_key.created_at,
        )

    def revoke_api_key(self, user_id: UUID, api_key_id: UUID) -> bool:
        api_key = self.db.query(ApiKey).filter(
            ApiKey.id == api_key_id,
            ApiKey.user_id == user_id
        ).first()

        if not api_key:
            return False

        api_key.is_active = False
        api_key.revoked_at = datetime.utcnow()
        self.db.commit()
        return True

    def get_user_api_keys(self, user_id: UUID) -> list[ApiKey]:
        return self.db.query(ApiKey).filter(ApiKey.user_id == user_id).all()

    def get_user_sessions(self, user_id: UUID) -> list[Session]:
        return self.db.query(Session).filter(Session.user_id == user_id).order_by(Session.created_at.desc()).all()

    def revoke_session(self, user_id: UUID, session_id: UUID) -> bool:
        session = self.db.query(Session).filter(
            Session.id == session_id,
            Session.user_id == user_id
        ).first()

        if not session:
            return False

        session.is_active = False
        session.revoked_at = datetime.utcnow()
        self.db.commit()
        return True

    def has_permission(self, user: User, resource: str, action: str) -> bool:
        for user_role in user.roles:
            role = user_role.role
            for role_perm in role.permissions:
                perm = role_perm.permission
                if perm.resource == resource and perm.action == action:
                    return True
        return False

    def has_role(self, user: User, role_name: str) -> bool:
        return any(role.role.name == role_name for role in user.roles)

    def _user_to_response(self, user: User):
        from app.schemas.auth import UserResponse, RoleResponse, PermissionResponse

        role_responses = []
        for user_role in user.roles:
            role = user_role.role
            perm_responses = [
                PermissionResponse(
                    id=perm.id,
                    name=perm.name,
                    description=perm.description,
                    resource=perm.resource,
                    action=perm.action,
                    created_at=perm.created_at,
                )
                for role_perm in role.permissions
                for perm in [role_perm.permission]
            ]

            role_responses.append(
                RoleResponse(
                    id=role.id,
                    name=role.name,
                    description=role.description,
                    is_system=role.is_system,
                    created_at=role.created_at,
                    updated_at=role.updated_at,
                )
            )

        return UserResponse(
            id=user.id,
            email=user.email,
            first_name=user.first_name,
            last_name=user.last_name,
            phone=user.phone,
            is_active=user.is_active,
            is_verified=user.is_verified,
            roles=role_responses,
            created_at=user.created_at,
            updated_at=user.updated_at,
            last_login=user.last_login,
        )