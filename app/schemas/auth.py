from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator


class RoleBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    description: Optional[str] = None


class RoleCreate(RoleBase):
    permissions: list[str] = []


class RoleResponse(RoleBase):
    id: UUID
    is_system: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PermissionBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    resource: str = Field(..., min_length=1, max_length=50)
    action: str = Field(..., min_length=1, max_length=50)


class PermissionCreate(PermissionBase):
    pass


class PermissionResponse(PermissionBase):
    id: UUID
    created_at: datetime

    class Config:
        from_attributes = True


class UserBase(BaseModel):
    email: EmailStr
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    phone: Optional[str] = Field(None, max_length=20)


class UserCreate(UserBase):
    password: str = Field(..., min_length=8, max_length=100)
    roles: list[str] = ["patient"]

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v


class UserUpdate(BaseModel):
    first_name: Optional[str] = Field(None, min_length=1, max_length=100)
    last_name: Optional[str] = Field(None, min_length=1, max_length=100)
    phone: Optional[str] = Field(None, max_length=20)


class UserResponse(UserBase):
    id: UUID
    is_active: bool
    is_verified: bool
    roles: list[RoleResponse]
    created_at: datetime
    updated_at: datetime
    last_login: Optional[datetime]

    class Config:
        from_attributes = True


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserResponse


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class RefreshTokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8, max_length=100)

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v


class PasswordChangeRequest(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=8, max_length=100)

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v


class EmailVerificationRequest(BaseModel):
    token: str


class ApiKeyBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    scopes: Optional[list[str]] = None


class ApiKeyCreate(ApiKeyBase):
    expires_in_days: Optional[int] = Field(None, ge=1, le=365)


class ApiKeyResponse(ApiKeyBase):
    id: UUID
    key: str
    is_active: bool
    last_used: Optional[datetime]
    expires_at: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True


class ApiKeyUpdate(BaseModel):
    is_active: Optional[bool] = None


class SessionResponse(BaseModel):
    id: UUID
    device_info: Optional[dict]
    ip_address: Optional[str]
    is_active: bool
    expires_at: datetime
    created_at: datetime
    revoked_at: Optional[datetime]

    class Config:
        from_attributes = True


class TokenPayload(BaseModel):
    sub: UUID
    exp: int
    type: str = "access"


class UserMeResponse(UserResponse):
    pass


class MessageResponse(BaseModel):
    message: str