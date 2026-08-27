"""
Empyrean database models.

Usage::

    from models import Base, User
"""

# Re-export the base so callers can do ``from models import Base``
# (L29: dispose_engines re-exported for symmetry with get_sync_db).
from models.base import (
    Base,
    dispose_engines,
    get_sync_db,
)

# Model classes (order doesn't matter — SQLAlchemy resolves string-based
# forward references lazily the first time models are used).
from models.user import User
from models.refresh_token import RefreshToken
from models.node import Node
from models.reading import SensorReading
from models.aggregate import HourlyAgg
from models.alert import Alert
from models.setting import SystemSetting, AuditLog

__all__ = [
    "Base",
    "User",
    "RefreshToken",
    "Node",
    "SensorReading",
    "HourlyAgg",
    "Alert",
    "SystemSetting",
    "AuditLog",
    "dispose_engines",
    "get_sync_db",
]
