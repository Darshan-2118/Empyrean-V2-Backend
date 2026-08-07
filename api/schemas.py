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


def _normalise_username(v: object) -> object:
    """Normalise a raw username before the schema constraints run.

    Called from ``mode="before"`` validators so the ``min_length`` /
    ``max_length`` constraints see the *normalised* value — otherwise padding
    like ``"  a  "`` passes the raw ``min_length=3`` check and is then stripped
    to a 1-char username here, bypassing the documented minimum. ``v`` is the
    raw input and may be any type: non-strings are returned unchanged so
    pydantic's core ``str`` validation rejects them with its standard error
    (never call ``.strip()`` on a non-string). Strings are stripped of
    surrounding whitespace and rejected if they contain illegal characters.
    """
    if not isinstance(v, str):
        # Non-string input: let pydantic's core str validation produce its
        # standard error rather than raising AttributeError from .strip().
        return v
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

    @field_validator("username", mode="before")
    @classmethod
    def _strip_username(cls, v: object) -> object:
        # mode="before" so min_length/max_length see the normalised value —
        # padding like "  a  " must not pass the 3-char minimum and then be
        # stripped to a 1-char username here.
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

    @field_validator("username", mode="before")
    @classmethod
    def _strip_username(cls, v: object) -> object:
        # mode="before" so min_length/max_length see the normalised value —
        # padding like "  a  " must not pass the 3-char minimum and then be
        # stripped to a 1-char username here. ``None`` (the optional-username
        # default) flows straight through _normalise_username unchanged.
        return _normalise_username(v)

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


# ── Node schemas ──────────────────────────────────────────────────────────────


class NodeResponse(BaseModel):
    """One sensor node's metadata (GET /nodes)."""

    node_id: str
    name: str | None = None
    location_name: str | None = None
    lat: float | None = None
    lon: float | None = None
    firmware_version: str | None = None
    reading_interval: int
    is_active: bool = True
    registered_at: datetime
    last_seen: datetime | None = None

    @field_serializer("registered_at", "last_seen")
    def _iso_datetime(self, value: datetime | None) -> str | None:
        return value.isoformat().replace("+00:00", "Z") if value else None


class RegisterNodeRequest(BaseModel):
    """Body for POST /nodes (self-service registration)."""

    node_id: str = Field(..., min_length=1, max_length=50)
    name: str | None = Field(None, max_length=100)
    location_name: str | None = Field(None, max_length=200)
    lat: float | None = None
    lon: float | None = None
    firmware_version: str | None = Field(None, max_length=50)
    reading_interval: int = Field(30, ge=1, le=86400)

    @field_validator("node_id")
    @classmethod
    def _node_id_safe(cls, v: str) -> str:
        import re
        # Mirror of mqtt/config._NODE_ID_RE: a crafted id must not be able to
        # inject MQTT path segments or wildcards.
        if not re.fullmatch(r"^[A-Za-z0-9_-]{1,50}$", v):
            raise ValueError(
                "node_id may contain only letters, digits, '_', '-' (1-50 chars)"
            )
        return v


class UpdateNodeRequest(BaseModel):
    """Body for PATCH /nodes/:node_id (admin re-configuration; all optional)."""

    name: str | None = Field(None, max_length=100)
    location_name: str | None = Field(None, max_length=200)
    lat: float | None = None
    lon: float | None = None
    firmware_version: str | None = Field(None, max_length=50)
    reading_interval: int | None = Field(None, ge=1, le=86400)
    is_active: bool | None = None


# ── Alert schemas ─────────────────────────────────────────────────────────────


class AlertResponse(BaseModel):
    """One threshold-breach alert (GET /alerts, PATCH acknowledge)."""

    alert_id: int
    node_id: str
    parameter: str
    value: float
    threshold: float
    severity: str
    message: str | None = None
    triggered_at: datetime
    acknowledged_at: datetime | None = None
    acknowledged_by: int | None = None

    @field_serializer("triggered_at", "acknowledged_at")
    def _iso_datetime(self, value: datetime | None) -> str | None:
        return value.isoformat().replace("+00:00", "Z") if value else None
