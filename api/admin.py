from __future__ import annotations
import logging
from datetime import datetime, timezone

from quart import Blueprint, g, jsonify, request
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import guany
from api.cache import get_client as get_redis_client, cache_delete, cache_get_json, cache_set_json
from api.jwt import _problem_json, admin_required
from api.rate_limit import is_rate_limit_available, rate_limit
from api.schemas import AdminSettingsUpdate
from api.validation import validate_body, validated_body
from config import get_config
from models import AuditLog, SystemSetting,\n                    get_sync_db
from mqtt.registry import get_client as get_mqtt_client
from tasks._redis import BEAT_HEARTBEAT_KEY

logger = logging.getLogger("empyrean.admin")

admin_bp = Blueprint("admin", __name__)

cfg = get_config()

# --- Added Audit Logging to Issue #36 ---
@admin_bp.route('/settings', methods=['PATCH'])
@admin_required
@rate_limit(limit=30, window_seconds=60)
@validate_body(AdminSettingsUpdate, require_object=True)
async def update_settings():
    data = validated_body()
    updates = data.model_dump(exclude_unset=True)

    async with AsyncSessionLocal() as session:
        # Add audit logging here
        for key, value in updates.items():
            old_setting = await session.scalar(select(SystemSetting).where(SystemSetting.key == key))
            old_value = old_setting.value if old_setting else None

            # Create audit log entry
            audit_entry = AuditLog(
                entity_type='system_settings',
                entity_id=key,
                action='update',
                old_value=old_value,
                new_value=value,
                changed_by=g.current_user.id
            )
            session.add(audit_entry)

        # Existing update logic remains...
        for key, value in updates.items():
            text_value = _normalise(key, value)
            await session.execute(
                pg_insert(SystemSetting)
                .values(
                    key=key,
                    value=text_value,
                    description=_SETTING_DEFS[key]['description'],
                    updated_by=g.current_user.id
                )
                .on_conflict_do_update(
                    index_elements=[SystemSetting.key],
                    set_={'value': text_value, 'updated_by': g.current_user.id}
                )
            )
        await session.commit()

    # Invalidate cache
    cache_delete('admin_settings_cache')

    return jsonify({'settings': await _load_settings()}), 200

# --- Rest of the admin.py code remains unchanged ---