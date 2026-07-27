"""
Empyrean database models.

Usage::

    from models import Base, User, get_db
"""

# Re-export the base so callers can do ``from models import Base``
from models.base import (
    Base,
    DatabaseError,
    DuplicateError,
    NotFoundError,
    async_db_session,
    get_db,
    get_sync_db,
    retry_on_db_failure,
)

# Model classes (order doesn't matter — SQLAlchemy resolves string-based
# forward references lazily the first time models are used).
from models.user import User
from models.refresh_token import RefreshToken
from models.node import Node
from models.reading import SensorReading
from models.aggregate import HourlyAgg
from models.alert import Alert
from models.setting import SystemSetting

__all__ = [
    "Base",
    "User",
    "RefreshToken",
    "Node",
    "SensorReading",
    "HourlyAgg",
    "Alert",
    "SystemSetting",
    "DatabaseError",
    "DuplicateError",
    "NotFoundError",
    "get_db",
    "get_sync_db",
    "async_db_session",
    "retry_on_db_failure",
]
