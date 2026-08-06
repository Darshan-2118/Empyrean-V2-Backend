"""
Empyrean database models.

Usage::

    from models import Base, User
"""

# Re-export the base so callers can do ``from models import Base``
from models.base import (
    Base,
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
    "get_sync_db",
]
