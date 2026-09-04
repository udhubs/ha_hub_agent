"""Allowlisted HA Admin WebSocket proxy (catalog kind=ha_ws)."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .capability_catalog import HA_WS_ALLOWLIST

_LOGGER = logging.getLogger(__name__)


async def call_ha_ws(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    ws_type = str(payload.get("ws_type") or payload.get("type") or "").strip()
    if not ws_type:
        return {"status": "failed", "error": "missing_ws_type"}
    if ws_type not in HA_WS_ALLOWLIST:
        return {"status": "denied", "error": "ha_ws_type_not_allowed", "ws_type": ws_type}

    # Prefer mapping known types to local implementations (no raw WS needed).
    body = {k: v for k, v in payload.items() if k not in ("ws_type", "type", "kind", "action", "command_id")}

    try:
        if ws_type == "get_services":
            from . import ha_query

            return await ha_query.services_list(hass, body)
        if ws_type == "get_states":
            from . import ha_query

            return await ha_query.states_get(hass, body)
        if ws_type == "render_template":
            from . import ha_query

            return await ha_query.template_render(hass, body)
        if ws_type == "fire_event":
            from . import ha_query

            return await ha_query.event_fire(hass, body)
        if ws_type == "history/history_during_period":
            from . import ha_query

            return await ha_query.history_get(hass, body)
        if ws_type == "logbook/get_events":
            from . import ha_query

            return await ha_query.logbook_get(hass, body)
        if ws_type == "recorder/statistics_during_period":
            from . import ha_query

            return await ha_query.statistics_get(hass, body)
        if ws_type.startswith("config/entity_registry/"):
            from . import ha_objects

            action_map = {
                "config/entity_registry/list": "entity_list",
                "config/entity_registry/get": "entity_get",
                "config/entity_registry/update": "entity_update",
                "config/entity_registry/remove": "entity_remove",
            }
            return await ha_objects.dispatch_registry(hass, action_map[ws_type], body)
        if ws_type.startswith("config/device_registry/"):
            from . import ha_objects

            action_map = {
                "config/device_registry/list": "device_list",
                "config/device_registry/update": "device_update",
                "config/device_registry/remove": "device_remove",
                "config/device_registry/list_linked_devices": "list_linked_devices",
                "config/device_registry/list_composite_splits": "list_composite_splits",
            }
            return await ha_objects.dispatch_registry(hass, action_map[ws_type], body)
        if ws_type.startswith("config/area_registry/"):
            from . import ha_objects

            action_map = {
                "config/area_registry/list": "area_list",
                "config/area_registry/create": "area_create",
                "config/area_registry/update": "area_update",
                "config/area_registry/delete": "area_delete",
            }
            return await ha_objects.dispatch_registry(hass, action_map[ws_type], body)
        if ws_type.startswith("config/floor_registry/"):
            from . import ha_objects

            action_map = {
                "config/floor_registry/list": "floor_list",
                "config/floor_registry/create": "floor_create",
                "config/floor_registry/update": "floor_update",
                "config/floor_registry/delete": "floor_delete",
            }
            return await ha_objects.dispatch_registry(hass, action_map[ws_type], body)
        if ws_type.startswith("config/label_registry/"):
            from . import ha_objects

            if ws_type.endswith("/list"):
                return await ha_objects.list_labels(hass)
            if ws_type.endswith("/create"):
                return await ha_objects.create_label(hass, body)
            if ws_type.endswith("/delete"):
                return await ha_objects.delete_label(hass, body)
            if ws_type.endswith("/update"):
                return await ha_objects.update_label(hass, body)
        if ws_type.startswith("config/category_registry/"):
            from . import ha_objects

            action_map = {
                "config/category_registry/list": "category_list",
                "config/category_registry/create": "category_create",
                "config/category_registry/update": "category_update",
                "config/category_registry/delete": "category_delete",
            }
            return await ha_objects.dispatch_registry(hass, action_map[ws_type], body)
        if ws_type in ("config/config_entries/get",):
            entries = [
                {
                    "entry_id": e.entry_id,
                    "domain": e.domain,
                    "title": e.title,
                    "state": str(e.state) if e.state else None,
                    "disabled_by": str(e.disabled_by) if e.disabled_by else None,
                }
                for e in hass.config_entries.async_entries()
            ]
            return {"status": "ok", "entries": entries}
        if ws_type in ("config/config_entries/disable", "config/config_entries/enable"):
            entry_id = body.get("entry_id")
            if not entry_id:
                return {"status": "failed", "error": "missing_entry_id"}
            from homeassistant.config_entries import ConfigEntryDisabler

            disabled = (
                ConfigEntryDisabler.USER
                if ws_type.endswith("disable")
                else None
            )
            await hass.config_entries.async_set_disabled_by(str(entry_id), disabled)
            return {"status": "ok", "entry_id": entry_id}
        if ws_type.startswith("blueprint/"):
            from . import ha_config_objects

            action = ws_type.split("/", 1)[1]
            return await ha_config_objects.dispatch_blueprint(hass, action, body)
        if ws_type in ("automation/config", "script/config", "scene/config"):
            from . import ha_config_objects

            domain = ws_type.split("/", 1)[0]
            action = str(body.get("action") or "list")
            if domain == "automation":
                return await ha_config_objects.dispatch_automation(hass, action, body)
            if domain == "script":
                return await ha_config_objects.dispatch_script(hass, action, body)
            return await ha_config_objects.dispatch_scene(hass, action, body)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.exception("ha_ws call failed type=%s", ws_type)
        return {"status": "failed", "error": str(exc), "ws_type": ws_type}

    return {
        "status": "failed",
        "error": "ha_ws_not_implemented",
        "ws_type": ws_type,
        "hint": "type is allowlisted but no local mapper yet",
    }
