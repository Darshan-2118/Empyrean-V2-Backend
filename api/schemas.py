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
# not advertise strength the hash cannot provide. The ``max_length=72`` Field
# bounds count *characters*; bcrypt truncates at 72 *bytes*, so a multi-byte
# password must also be validated byte-length in a validator (M-14).
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


def _validate_password_bytes(v: str) -> str:
    """Reject a password whose UTF-8 encoding exceeds bcrypt's 72-byte limit.

    ``max_length=72`` on the ``Field`` counts *characters*; bcrypt hashes and
    compares at most 72 *bytes*, so e.g. a 72-char password of multi-byte
    characters silently prefix-collides with a shorter one. Validate the byte
    length explicitly to enforce the real limit (M-14).
    """
    if len(v.encode("utf-8")) > MAX_PASSWORD_LEN:
        raise ValueError(
            f"password is too long: bcrypt supports at most "
            f"{MAX_PASSWORD_LEN} bytes in UTF-8"
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

    @field_validator("password")
    @classmethod
    def _password_within_bcrypt_bytes(cls, v: str) -> str:
        return _validate_password_bytes(v)


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=1, max_length=MAX_PASSWORD_LEN)

    @field_validator("password")
    @classmethod
    def _password_within_bcrypt_bytes(cls, v: str) -> str:
        return _validate_password_bytes(v)


class RefreshRequest(BaseModel):
    # M-13: cap the token length so an oversized body is rejected by the schema
    # (the request-body cap in app.py is the outer defense). Generated refresh
    # tokens are ~86 chars, so 256 is comfortably above the real size.
    refresh_token: str = Field(..., min_length=1, max_length=256)


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

    @field_validator("current_password")
    @classmethod
    def _current_password_within_bcrypt_bytes(cls, v: str) -> str:
        return _validate_password_bytes(v)

    @field_validator("new_password")
    @classmethod
    def _new_password_within_bcrypt_bytes(cls, v: str) -> str:
        return _validate_password_bytes(v)


# ── Reading schemas ────────────────────────────────────────────────────────────


class LatestReading(BaseModel):
    """Latest enriched reading for one node (GET /readings/latest)."""

    node_id: str
    time: datetime
    temperature: float | None = None
    humidity: float | None = None
    pressure: float | None = None
    pm25: float | None = None
    pm10: float | None = None
    battery_v: float | None = None
    fuzzy_score: float | None = None
    aqi: int | None = None
    aqi_category: str | None = None
    is_anomaly: bool = False

    @field_serializer("time")
    def _iso_datetime(self, value: datetime) -> str | None:
        # Serialize to ISO 8601 with trailing Z (docs contract).
        return value.isoformat().replace("+00:00", "Z") if value else None


class HistoryBucket(BaseModel):
    """One time bucket of aggregated readings (GET /readings/history)."""

    bucket: datetime
    node_id: str
    avg_temperature: float | None = None
    avg_humidity: float | None = None
    avg_pm25: float | None = None
    avg_pm10: float | None = None
    avg_aqi: float | None = None
    max_aqi: int | None = None
    min_aqi: int | None = None
    reading_count: int = 0

    @field_serializer("bucket")
    def _iso_datetime(self, value: datetime) -> str | None:
        # Serialize to ISO 8601 with trailing Z (docs contract).
        return value.isoformat().replace("+00:00", "Z") if value else None


# ── Forecast schemas ───────────────────────────────────────────────────────────


class ForecastPoint(BaseModel):
    """One time-stamped AQI forecast value (GET /forecast)."""

    time: datetime
    aqi: float

    @field_serializer("time")
    def _iso_datetime(self, value: datetime) -> str | None:
        # Serialize to ISO 8601 with trailing Z (docs contract).
        return value.isoformat().replace("+00:00", "Z") if value else None


class ForecastResponse(BaseModel):
    """A 60-minute AQI forecast for one node (GET /forecast).

    ``points`` is accepted from either cached ISO strings or datetimes;
    pydantic v2 coerces the former to ``ForecastPoint.time``.
    """

    node_id: str
    horizon_minutes: int
    points: list[ForecastPoint]
