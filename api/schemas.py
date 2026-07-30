"""
Pydantic schemas for request validation and response serialization.

Split into ``Auth*`` and ``Profile*`` namespaces so importing routes
only pull in what they need.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


# ── Auth schemas ───────────────────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    username: str = Field(
        ..., min_length=3, max_length=50, description="Unique login name"
    )
    email: EmailStr = Field(..., max_length=255)
    password: str = Field(
        ..., min_length=6, max_length=128, description="Plain-text password"
    )


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=1)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int  # seconds
    role: str
    user: "UserBrief"


class UserBrief(BaseModel):
    id: int
    username: str
    email: str
    role: str


# ── Profile schemas ────────────────────────────────────────────────────────────


class ProfileResponse(BaseModel):
    id: int
    username: str
    email: str
    role: str
    notification_prefs: dict
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UpdateProfileRequest(BaseModel):
    username: str | None = Field(None, min_length=3, max_length=50)
    email: EmailStr | None = Field(None, max_length=255)
    notification_prefs: dict | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6, max_length=128)
