"""
Pydantic schemas for request validation and response serialization.

Split into ``Auth*`` and ``Profile*`` namespaces so importing routes
only pull in what they need.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import (
    BaseModel,
    EmailStr,
    Field,
    field_serializer,
    field_validator,
)

# bcrypt ignores everything past 72 bytes — cap passwords so the schema does
# not advertise strength the hash cannot provide.
MAX_PASSWORD_LEN = 72

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _normalise_username(v: str) -> str:
    """Strip surrounding whitespace and reject usernames with illegal chars."""
    v = v.strip()
    if not _USERNAME_RE.fullmatch(v):
        raise ValueError(
            "username may contain only letters, digits, and underscores"
        )
    return v


# ── Auth schemas ───────────────────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    username: str = Field(
        ..., min_length=3, max_length=50, description="Unique login name"
    )
    email: EmailStr = Field(..., max_length=255)
    password: str = Field(
        ..., min_length=6, max_length=MAX_PASSWORD_LEN,
        description="Plain-text password",
    )

    @field_validator("username")
    @classmethod
    def _strip_username(cls, v: str) -> str:
        return _normalise_username(v)

    @field_validator("email")
    @classmethod
    def _lower_email(cls, v: str) -> str:
        return str(v).lower()


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=1, max_length=MAX_PASSWORD_LEN)


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

    @field_serializer("created_at", "updated_at", "last_login_at")
    def _iso_datetime(self, value: datetime | None) -> str | None:
        # Serialize to ISO 8601 (docs contract) rather than Quart's RFC1123.
        return value.isoformat().replace("+00:00", "Z") if value else None


class UpdateProfileRequest(BaseModel):
    username: str | None = Field(None, min_length=3, max_length=50)
    email: EmailStr | None = Field(None, max_length=255)
    notification_prefs: dict | None = None

    @field_validator("username")
    @classmethod
    def _strip_username(cls, v: str | None) -> str | None:
        return _normalise_username(v) if v is not None else None

    @field_validator("email")
    @classmethod
    def _lower_email(cls, v: str | None) -> str | None:
        return str(v).lower() if v is not None else None


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=6, max_length=MAX_PASSWORD_LEN)
