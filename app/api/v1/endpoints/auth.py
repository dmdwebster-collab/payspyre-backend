from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.core.auth import (
    get_current_user,
    get_current_active_user,
    require_roles,
    auth_rate_limiter,
    rate_limit
)
from app.core.config import settings
from app.db.base import get_db
from app.models.user import User
from app.schemas.auth import (
    UserCreate,
    UserResponse,
    UserUpdate,
    LoginResponse,
    RefreshTokenRequest,
    RefreshTokenResponse,
    PasswordResetRequest,
    PasswordResetConfirm,
    PasswordChangeRequest,
    EmailVerificationRequest,
    ApiKeyCreate,
    ApiKeyResponse,
    ApiKeyUpdate,
    SessionResponse,
    UserMeResponse,
    MessageResponse
)
from app.services.auth_service import AuthService
from app.services.email_service import email_service

router = APIRouter()

email_service.configure(settings.RESEND_API_KEY, settings.RESEND_FROM_EMAIL)


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    user_data: UserCreate,
    request: Request,
    db: Session = Depends(get_db)
):
    await rate_limit(auth_rate_limiter)(request)

    auth_service = AuthService(db)
    try:
        user = auth_service.create_user(user_data)

        verification_url = f"{settings.cors_origins_list[0]}/verify-email?token={user.email_verification_token}"
        await email_service.send_verification_email(
            to=user.email,
            name=user.first_name,
            verification_url=verification_url
        )

        return auth_service._user_to_response(user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/login", response_model=LoginResponse)
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    await rate_limit(auth_rate_limiter)(request)

    auth_service = AuthService(db)
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    try:
        result = auth_service.authenticate_user(
            email=form_data.username,
            password=form_data.password,
            ip_address=ip_address,
            user_agent=user_agent,
            device_info={"type": "web"}
        )
        if result:
            return result
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect email or password",
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.post("/refresh", response_model=RefreshTokenResponse)
async def refresh_token(
    refresh_data: RefreshTokenRequest,
    request: Request,
    db: Session = Depends(get_db)
):
    await rate_limit(auth_rate_limiter)(request)

    auth_service = AuthService(db)
    ip_address = request.client.host if request.client else None

    result = auth_service.refresh_access_token(refresh_data.refresh_token, ip_address)
    if result:
        return result

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired refresh token",
    )


@router.post("/logout", response_model=MessageResponse)
async def logout(
    refresh_data: RefreshTokenRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    auth_service = AuthService(db)
    if auth_service.logout_user(refresh_data.refresh_token):
        return MessageResponse(message="Successfully logged out")
    raise HTTPException(status_code=400, detail="Invalid session")


@router.post("/logout-all", response_model=MessageResponse)
async def logout_all(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    auth_service = AuthService(db)
    count = auth_service.revoke_all_sessions(current_user.id)
    return MessageResponse(message=f"Revoked {count} sessions")


@router.get("/me", response_model=UserMeResponse)
async def get_me(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    auth_service = AuthService(db)
    return auth_service._user_to_response(current_user)


@router.patch("/me", response_model=UserResponse)
async def update_me(
    user_data: UserUpdate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    auth_service = AuthService(db)
    updated_user = auth_service.update_user(current_user.id, user_data)
    if updated_user:
        return auth_service._user_to_response(updated_user)
    raise HTTPException(status_code=404, detail="User not found")


@router.post("/change-password", response_model=MessageResponse)
async def change_password(
    password_data: PasswordChangeRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    auth_service = AuthService(db)
    if auth_service.change_password(current_user.id, password_data.old_password, password_data.new_password):
        return MessageResponse(message="Password changed successfully")
    raise HTTPException(status_code=400, detail="Incorrect password")


@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(
    reset_data: PasswordResetRequest,
    request: Request,
    db: Session = Depends(get_db)
):
    await rate_limit(auth_rate_limiter)(request)

    auth_service = AuthService(db)
    user = auth_service.initiate_password_reset(reset_data.email)

    if user:
        reset_url = f"{settings.cors_origins_list[0]}/reset-password?token={user.password_reset_token}"
        await email_service.send_password_reset_email(
            to=user.email,
            name=user.first_name,
            reset_url=reset_url
        )

    return MessageResponse(message="If an account exists with this email, a password reset link has been sent")


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(
    reset_data: PasswordResetConfirm,
    db: Session = Depends(get_db)
):
    auth_service = AuthService(db)
    if auth_service.confirm_password_reset(reset_data.token, reset_data.new_password):
        return MessageResponse(message="Password reset successfully")
    raise HTTPException(status_code=400, detail="Invalid or expired reset token")


@router.post("/verify-email", response_model=MessageResponse)
async def verify_email(
    verification_data: EmailVerificationRequest,
    db: Session = Depends(get_db)
):
    auth_service = AuthService(db)
    if auth_service.verify_email(verification_data.token):
        return MessageResponse(message="Email verified successfully")
    raise HTTPException(status_code=400, detail="Invalid or expired verification token")


@router.get("/sessions", response_model=list[SessionResponse])
async def get_sessions(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    auth_service = AuthService(db)
    sessions = auth_service.get_user_sessions(current_user.id)
    return [
        SessionResponse(
            id=session.id,
            device_info=session.device_info,
            ip_address=session.ip_address,
            is_active=session.is_active,
            expires_at=session.expires_at,
            created_at=session.created_at,
            revoked_at=session.revoked_at,
        )
        for session in sessions
    ]


@router.delete("/sessions/{session_id}", response_model=MessageResponse)
async def revoke_session(
    session_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    auth_service = AuthService(db)
    if auth_service.revoke_session(current_user.id, session_id):
        return MessageResponse(message="Session revoked successfully")
    raise HTTPException(status_code=404, detail="Session not found")


@router.get("/api-keys", response_model=list[ApiKeyResponse])
async def get_api_keys(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    auth_service = AuthService(db)
    api_keys = auth_service.get_user_api_keys(current_user.id)
    return [
        ApiKeyResponse(
            id=key.id,
            key=key.key_hash,
            name=key.name,
            scopes=key.scopes,
            is_active=key.is_active,
            last_used=key.last_used,
            expires_at=key.expires_at,
            created_at=key.created_at,
        )
        for key in api_keys
    ]


@router.post("/api-keys", response_model=ApiKeyResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    api_key_data: ApiKeyCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    auth_service = AuthService(db)
    return auth_service.create_api_key(current_user.id, api_key_data)


@router.delete("/api-keys/{api_key_id}", response_model=MessageResponse)
async def revoke_api_key(
    api_key_id: UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db)
):
    auth_service = AuthService(db)
    if auth_service.revoke_api_key(current_user.id, api_key_id):
        return MessageResponse(message="API key revoked successfully")
    raise HTTPException(status_code=404, detail="API key not found")


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db)
):
    auth_service = AuthService(db)
    users = db.query(User).offset(skip).limit(limit).all()
    return [auth_service._user_to_response(user) for user in users]


@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: UUID,
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db)
):
    auth_service = AuthService(db)
    user = auth_service.get_user_by_id(user_id)
    if user:
        return auth_service._user_to_response(user)
    raise HTTPException(status_code=404, detail="User not found")


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: UUID,
    user_data: UserUpdate,
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db)
):
    auth_service = AuthService(db)
    updated_user = auth_service.update_user(user_id, user_data)
    if updated_user:
        return auth_service._user_to_response(updated_user)
    raise HTTPException(status_code=404, detail="User not found")


@router.post("/users/{user_id}/deactivate", response_model=MessageResponse)
async def deactivate_user(
    user_id: UUID,
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db)
):
    auth_service = AuthService(db)
    if auth_service.deactivate_user(user_id):
        return MessageResponse(message="User deactivated successfully")
    raise HTTPException(status_code=404, detail="User not found")


@router.post("/users/{user_id}/activate", response_model=MessageResponse)
async def activate_user(
    user_id: UUID,
    current_user: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db)
):
    auth_service = AuthService(db)
    if auth_service.activate_user(user_id):
        return MessageResponse(message="User activated successfully")
    raise HTTPException(status_code=404, detail="User not found")