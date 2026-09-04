"""Dispatch catalog generic kinds (kind + action) to HA surfaces."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .capability_catalog import is_action_allowed, resolve_kind_action

_LOGGER = logging.getLogger(__name__)


def _devices_for_config_entry(dev_reg: Any, entry_id: str) -> list[Any]:
    """List devices linked to a config entry.

    HA exposes this as a *module* helper ``device_registry.async_entries_for_config_entry``,
    not a DeviceRegistry method. Fall back to scanning ``devices`` for older cores.
    """
    from homeassistant.helpers import device_registry as dr

    helper = getattr(dr, "async_entries_for_config_entry", None)
    if callable(helper):
        return list(helper(dev_reg, entry_id))
    devices = getattr(dev_reg, "devices", None) or {}
    values = devices.values() if hasattr(devices, "values") else devices
    return [
        d
        for d in values
        if entry_id in (getattr(d, "config_entries", None) or set())
    ]


def _entities_for_config_entry(ent_reg: Any, entry_id: str) -> list[Any]:
    """List entities linked to a config entry (module helper, with fallback)."""
    from homeassistant.helpers import entity_registry as er

    helper = getattr(er, "async_entries_for_config_entry", None)
    if callable(helper):
        return list(helper(ent_reg, entry_id))
    entities = getattr(ent_reg, "entities", None) or {}
    values = entities.values() if hasattr(entities, "values") else entities
    return [
        e
        for e in values
        if getattr(e, "config_entry_id", None) == entry_id
    ]


def _entities_for_device(ent_reg: Any, device_id: str) -> list[Any]:
    """List entities linked to a device (module helper, with fallback)."""
    from homeassistant.helpers import entity_registry as er

    helper = getattr(er, "async_entries_for_device", None)
    if callable(helper):
        return list(helper(ent_reg, device_id))
    entities = getattr(ent_reg, "entities", None) or {}
    values = entities.values() if hasattr(entities, "values") else entities
    return [e for e in values if getattr(e, "device_id", None) == device_id]


async def dispatch_catalog(
    hass: HomeAssistant,
    *,
    kind: str,
    action: str | None,
    payload: dict[str, Any],
    require_supervisor_ops,
    require_extended_tier,
    result: dict[str, Any],
) -> bool:
    """Handle generic catalog kinds.

    Returns True if handled (result filled), False if caller should use legacy path.
    """
    gk, ga = resolve_kind_action(kind, action)
    # Only claim generic kinds that aren't pure legacy passthrough without router
    if gk in (
        "call_service",
        "config_write",
        "scene_create",
        "registry_update",
        "sync_full",
        "agent_update",
        "ssh_bypass_probe",
        "ssh_bypass_ensure",
        "packages_probe",
        "packages_ensure",
        "shell_command",
    ) and kind == gk:
        return False

    if not is_action_allowed(kind, ga):
        result["status"] = "denied"
        result["error"] = "action_not_in_catalog"
        result["kind"] = gk
        result["action"] = ga
        return True

    # Extended gate for most catalog surfaces
    if gk not in ("call_service",) and not require_extended_tier(payload, result):
        return True

    try:
        snap: dict[str, Any]
        if gk == "registry":
            from . import ha_objects

            snap = await ha_objects.dispatch_registry(hass, ga, payload)
        elif gk == "helper":
            from . import ha_objects

            if ga == "list":
                snap = await ha_objects.list_helpers(hass)
            elif ga == "create":
                snap = await ha_objects.create_helper(hass, payload)
            elif ga == "update":
                snap = await ha_objects.update_helper(hass, payload)
            elif ga == "delete":
                snap = await ha_objects.delete_helper(hass, payload)
            else:
                snap = {"status": "failed", "error": "unsupported_action"}
        elif gk == "group":
            from . import ha_objects

            if ga == "list":
                snap = await ha_objects.list_groups(hass)
            elif ga == "upsert":
                snap = await ha_objects.upsert_group(hass, payload)
            elif ga == "delete":
                snap = await ha_objects.delete_group(hass, payload)
            else:
                snap = {"status": "failed", "error": "unsupported_action"}
        elif gk == "label":
            from . import ha_objects

            if ga == "list":
                snap = await ha_objects.list_labels(hass)
            elif ga == "create":
                snap = await ha_objects.create_label(hass, payload)
            elif ga == "update":
                snap = await ha_objects.update_label(hass, payload)
            elif ga == "delete":
                snap = await ha_objects.delete_label(hass, payload)
            elif ga == "assign":
                snap = await ha_objects.assign_labels(hass, payload)
            else:
                snap = {"status": "failed", "error": "unsupported_action"}
        elif gk == "integration":
            snap = await _integration(hass, ga, payload)
        elif gk == "user":
            snap = await _user(hass, ga, payload)
        elif gk == "ha_system":
            snap = await _ha_system(hass, ga, payload)
        elif gk == "supervisor":
            if not require_supervisor_ops(result):
                return True
            snap = await _supervisor(hass, ga, payload)
        elif gk == "backup":
            if not require_supervisor_ops(result):
                return True
            snap = await _backup(hass, ga, payload)
        elif gk == "hacs":
            from . import hacs_ops

            snap = await hacs_ops.dispatch(hass, ga, payload)
        elif gk == "query":
            from . import ha_query

            snap = await ha_query.dispatch_query(hass, ga, payload)
        elif gk == "template":
            from . import ha_query

            snap = await ha_query.template_render(hass, payload)
        elif gk == "event":
            from . import ha_query

            snap = await ha_query.event_fire(hass, payload)
        elif gk == "blueprint":
            from . import ha_config_objects

            snap = await ha_config_objects.dispatch_blueprint(hass, ga, payload)
        elif gk == "automation_config":
            from . import ha_config_objects

            snap = await ha_config_objects.dispatch_automation(hass, ga, payload)
        elif gk == "script_config":
            from . import ha_config_objects

            snap = await ha_config_objects.dispatch_script(hass, ga, payload)
        elif gk == "scene_config":
            from . import ha_config_objects

            snap = await ha_config_objects.dispatch_scene(hass, ga, payload)
        elif gk == "config_snapshot":
            from . import config_snapshot

            snap = await config_snapshot.collect_config_snapshot(hass, payload)
        elif gk == "file":
            # handled by legacy file_* in cloud_client
            return False
        elif gk == "ha_ws":
            from . import ha_ws_proxy

            snap = await ha_ws_proxy.call_ha_ws(hass, payload)
        else:
            return False

        result.update(snap)
        if "status" not in result:
            result["status"] = "ok"
        result["catalog_kind"] = gk
        result["catalog_action"] = ga
        return True
    except Exception as exc:  # noqa: BLE001
        _LOGGER.exception("catalog dispatch failed %s.%s", gk, ga)
        result["status"] = "failed"
        result["error"] = str(exc)
        result["catalog_kind"] = gk
        result["catalog_action"] = ga
        return True


async def _integration(hass: HomeAssistant, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action == "list":
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        dev_reg = dr.async_get(hass)
        ent_reg = er.async_get(hass)
        items = []
        for entry in hass.config_entries.async_entries():
            items.append(
                {
                    "entry_id": entry.entry_id,
                    "domain": entry.domain,
                    "title": entry.title,
                    "state": str(entry.state) if entry.state else None,
                    "source": entry.source,
                    "disabled_by": str(entry.disabled_by) if entry.disabled_by else None,
                    "devices_count": len(
                        _devices_for_config_entry(dev_reg, entry.entry_id)
                    ),
                    "entities_count": len(
                        _entities_for_config_entry(ent_reg, entry.entry_id)
                    ),
                }
            )
        return {"status": "ok", "integrations": items}
    entry_id = payload.get("entry_id")
    domain = payload.get("domain")
    if action == "reload":
        if entry_id:
            await hass.config_entries.async_reload(str(entry_id))
            return {"status": "ok", "entry_id": entry_id}
        if domain:
            count = 0
            for e in hass.config_entries.async_entries(str(domain)):
                await hass.config_entries.async_reload(e.entry_id)
                count += 1
            return {"status": "ok", "domain": domain, "reloaded_count": count}
        return {"status": "failed", "error": "missing_entry_id_or_domain"}
    if action == "remove":
        if not entry_id:
            return {"status": "failed", "error": "missing_entry_id"}
        await hass.config_entries.async_remove(str(entry_id))
        return {"status": "ok", "entry_id": entry_id}
    if action in ("enable", "disable"):
        if not entry_id:
            return {"status": "failed", "error": "missing_entry_id"}
        from homeassistant.config_entries import ConfigEntryDisabler

        await hass.config_entries.async_set_disabled_by(
            str(entry_id),
            ConfigEntryDisabler.USER if action == "disable" else None,
        )
        return {"status": "ok", "entry_id": entry_id, "action": action}
    if action == "get":
        if not entry_id:
            return {"status": "failed", "error": "missing_entry_id"}
        e = hass.config_entries.async_get_entry(str(entry_id))
        if not e:
            return {"status": "failed", "error": "not_found"}
        return {
            "status": "ok",
            "entry": {
                "entry_id": e.entry_id,
                "domain": e.domain,
                "title": e.title,
                "data_keys": list((e.data or {}).keys()),
                "options_keys": list((e.options or {}).keys()),
            },
        }
    if action == "options_get":
        if not entry_id:
            return {"status": "failed", "error": "missing_entry_id"}
        e = hass.config_entries.async_get_entry(str(entry_id))
        if not e:
            return {"status": "failed", "error": "not_found"}
        return {"status": "ok", "options": dict(e.options or {})}
    if action == "options_set":
        if not entry_id:
            return {"status": "failed", "error": "missing_entry_id"}
        options = payload.get("options")
        if not isinstance(options, dict):
            return {"status": "failed", "error": "options_must_be_object"}
        hass.config_entries.async_update_entry(
            hass.config_entries.async_get_entry(str(entry_id)), options=options
        )
        return {"status": "ok", "entry_id": entry_id}
    if action == "rename_title":
        if not entry_id:
            return {"status": "failed", "error": "missing_entry_id"}
        title = payload.get("title")
        if not isinstance(title, str) or not title.strip():
            return {"status": "failed", "error": "invalid_title"}
        e = hass.config_entries.async_get_entry(str(entry_id))
        if not e:
            return {"status": "failed", "error": "not_found"}
        new_title = title.strip()
        hass.config_entries.async_update_entry(e, title=new_title)
        return {"status": "ok", "entry_id": entry_id, "title": new_title}

    if action in ("devices_list", "entities_list"):
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        target_ids: list[str] | None = None
        raw_ids = payload.get("entry_ids")
        if isinstance(raw_ids, list) and raw_ids:
            target_ids = [str(x) for x in raw_ids]
        elif entry_id:
            target_ids = [str(entry_id)]
        elif domain:
            target_ids = [
                e.entry_id for e in hass.config_entries.async_entries(str(domain))
            ]
        if not target_ids:
            return {"status": "failed", "error": "missing_entry_ids_or_domain"}

        dev_reg = dr.async_get(hass)
        ent_reg = er.async_get(hass)
        if action == "devices_list":
            seen: set[str] = set()
            devices: list[dict[str, Any]] = []
            for eid in target_ids:
                for d in _devices_for_config_entry(dev_reg, eid):
                    if d.id in seen:
                        continue
                    seen.add(d.id)
                    entities = _entities_for_device(ent_reg, d.id)
                    devices.append(
                        {
                            "id": d.id,
                            "name": d.name_by_user or d.name,
                            "manufacturer": d.manufacturer,
                            "model": d.model,
                            "area_id": d.area_id,
                            "disabled_by": str(d.disabled_by)
                            if d.disabled_by
                            else None,
                            "config_entry_ids": list(d.config_entries),
                            "entities_count": len(entities),
                        }
                    )
            devices.sort(key=lambda x: str(x.get("name") or "").lower())
            return {"status": "ok", "devices": devices, "count": len(devices)}

        seen_ent: set[str] = set()
        entities: list[dict[str, Any]] = []
        for eid in target_ids:
            for ent in _entities_for_config_entry(ent_reg, eid):
                if ent.entity_id in seen_ent:
                    continue
                seen_ent.add(ent.entity_id)
                domain_part = ent.entity_id.split(".", 1)[0] if ent.entity_id else ""
                state = hass.states.get(ent.entity_id)
                entities.append(
                    {
                        "entity_id": ent.entity_id,
                        "name": ent.name or ent.original_name,
                        "domain": domain_part,
                        "device_id": ent.device_id,
                        "disabled_by": str(ent.disabled_by)
                        if ent.disabled_by
                        else None,
                        "hidden_by": str(ent.hidden_by) if ent.hidden_by else None,
                        "state": state.state if state else None,
                    }
                )
        entities.sort(key=lambda x: str(x.get("entity_id") or ""))
        return {"status": "ok", "entities": entities, "count": len(entities)}

    from . import integration_flow

    if action == "handlers":
        return await integration_flow.list_handlers(hass)
    if action == "descriptions":
        return await integration_flow.list_descriptions(hass)
    if action == "flow_start":
        return await integration_flow.flow_start(hass, payload)
    if action == "flow_step":
        return await integration_flow.flow_step(hass, payload)
    if action == "flow_abort":
        return await integration_flow.flow_abort(hass, payload)
    if action == "flow_get":
        return await integration_flow.flow_get(hass, payload)
    if action == "flow_progress":
        return await integration_flow.flow_progress(hass, payload)
    if action == "options_flow_start":
        return await integration_flow.options_flow_start(hass, payload)
    if action == "options_flow_step":
        return await integration_flow.options_flow_step(hass, payload)
    return {"status": "failed", "error": "unsupported_action"}


async def _user(hass: HomeAssistant, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action == "list":
        users = await hass.auth.async_get_users()
        return {
            "status": "ok",
            "users": [
                {
                    "id": u.id,
                    "name": u.name,
                    "is_active": u.is_active,
                    "is_owner": u.is_owner,
                    "group_ids": [g.id for g in u.groups],
                }
                for u in users
            ],
        }
    if action == "create":
        name = payload.get("name")
        if not name:
            return {"status": "failed", "error": "missing_name"}
        user = await hass.auth.async_create_user(str(name), local_only=False)
        return {"status": "ok", "user_id": user.id, "name": name}
    if action == "delete":
        user_id = payload.get("user_id")
        if not user_id:
            return {"status": "failed", "error": "missing_user_id"}
        users = await hass.auth.async_get_users()
        target = next((u for u in users if u.id == user_id), None)
        if not target:
            return {"status": "failed", "error": "user_not_found"}
        if target.is_owner:
            return {"status": "denied", "error": "cannot_delete_owner"}
        await hass.auth.async_remove_user(target)
        return {"status": "ok", "user_id": user_id}
    if action == "update":
        user_id = payload.get("user_id")
        if not user_id:
            return {"status": "failed", "error": "missing_user_id"}
        users = await hass.auth.async_get_users()
        target = next((u for u in users if u.id == user_id), None)
        if not target:
            return {"status": "failed", "error": "user_not_found"}
        name = payload.get("name")
        group_ids = payload.get("group_ids")
        if name is None and group_ids is None:
            return {"status": "failed", "error": "nothing_to_update"}
        changed = False
        if name is not None and str(name).strip():
            target.name = str(name).strip()
            changed = True
        if isinstance(group_ids, list):
            groups = getattr(hass.auth, "groups", None) or []
            resolved = [g for g in groups if g.id in set(map(str, group_ids))]
            if len(resolved) != len(set(map(str, group_ids))):
                return {"status": "failed", "error": "unknown_group"}
            target.groups = resolved
            changed = True
        if not changed:
            return {"status": "failed", "error": "nothing_to_update"}
        store = getattr(hass.auth, "_store", None)
        if store is not None:
            await store.async_schedule_save()
        return {"status": "ok", "user_id": user_id, "name": target.name}
    return {"status": "failed", "error": "unsupported_action"}


async def _ha_system(hass: HomeAssistant, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action == "restart":
        await hass.services.async_call("homeassistant", "restart", {}, blocking=False)
        return {"status": "ok"}
    if action == "check_config":
        try:
            from homeassistant.helpers.check_config import async_check_ha_config_file

            res = await async_check_ha_config_file(hass)
            errors = []
            if res is not None:
                err_attr = getattr(res, "errors", None)
                if err_attr:
                    errors = [str(e) for e in err_attr]
                elif isinstance(res, list):
                    errors = [str(e) for e in res]
            if errors:
                return {"status": "failed", "error": "config_invalid", "errors": errors[:20]}
            return {"status": "ok"}
        except Exception:  # noqa: BLE001
            await hass.services.async_call("homeassistant", "check_config", {}, blocking=True)
            return {"status": "ok"}
    if action == "reload_core":
        await hass.services.async_call(
            "homeassistant", "reload_core_config", {}, blocking=True
        )
        return {"status": "ok"}
    if action == "reload_domain":
        domain = payload.get("domain")
        if not domain:
            return {"status": "failed", "error": "missing_domain"}
        if hass.services.has_service(str(domain), "reload"):
            await hass.services.async_call(str(domain), "reload", {}, blocking=True)
            return {"status": "ok", "domain": domain}
        return {
            "status": "failed",
            "error": "reload_service_missing",
            "hint": "use ha_system.reload_all or restart for full reload",
            "domain": domain,
        }
    if action == "reload_all":
        await hass.services.async_call(
            "homeassistant", "reload_all", {}, blocking=True
        )
        return {"status": "ok"}
    if action == "config_get":
        return {
            "status": "ok",
            "config": {
                "location_name": hass.config.location_name,
                "time_zone": hass.config.time_zone,
                "language": getattr(hass.config, "language", None),
                "latitude": hass.config.latitude,
                "longitude": hass.config.longitude,
                "elevation": hass.config.elevation,
                "unit_system": str(getattr(hass.config, "units", None)),
                "external_url": getattr(hass.config, "external_url", None),
                "internal_url": getattr(hass.config, "internal_url", None),
                "version": getattr(hass.config, "version", None)
                and str(hass.config.version),
            },
        }
    if action == "config_update":
        # Prefer HA websocket-equivalent helpers when available.
        updates: dict[str, Any] = {}
        for key in (
            "location_name",
            "time_zone",
            "language",
            "latitude",
            "longitude",
            "elevation",
        ):
            if key in payload:
                updates[key] = payload[key]
        if not updates:
            return {"status": "failed", "error": "nothing_to_update"}
        try:
            # HomeAssistantConfig exposes async_update / update in recent cores
            updater = getattr(hass.config, "async_update", None)
            if callable(updater):
                await updater(**updates)
            else:
                sync_upd = getattr(hass.config, "update", None)
                if callable(sync_upd):
                    sync_upd(**updates)
                else:
                    return {
                        "status": "failed",
                        "error": "config_update_unsupported",
                        "hint": "HA core too old for programmatic config update",
                    }
            return {"status": "ok", "updated": list(updates.keys()), "config": {
                "location_name": hass.config.location_name,
                "time_zone": hass.config.time_zone,
                "language": getattr(hass.config, "language", None),
                "latitude": hass.config.latitude,
                "longitude": hass.config.longitude,
                "elevation": hass.config.elevation,
            }}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc)}
    if action == "analytics_get":
        # Prefer on-disk preferences; no stable public write API → read-only probe.
        import json
        from pathlib import Path

        path = Path(hass.config.config_dir) / ".storage" / "analytics"
        if not path.is_file():
            path = Path(hass.config.config_dir) / ".storage" / "core.analytics"
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                data = raw.get("data", raw) if isinstance(raw, dict) else raw
                return {
                    "status": "ok",
                    "writable": False,
                    "source": str(path.name),
                    "analytics": data,
                    "hint": "分析偏好仅探测；写入需在 HA 前端「设置 → 系统 → 分析」完成",
                }
            except Exception as exc:  # noqa: BLE001
                return {"status": "failed", "error": str(exc)}
        return {
            "status": "ok",
            "writable": False,
            "analytics": None,
            "hint": "未找到 analytics 存储文件（可能从未启用）",
        }
    if action == "logger_set_default":
        level = str(payload.get("level") or "").strip().lower()
        if not level:
            return {"status": "failed", "error": "level_required"}
        if not hass.services.has_service("logger", "set_default_level"):
            return {
                "status": "failed",
                "error": "logger_service_missing",
                "hint": "需启用 logger 集成",
            }
        await hass.services.async_call(
            "logger", "set_default_level", {"level": level}, blocking=True
        )
        return {"status": "ok", "level": level}
    if action == "logger_set_level":
        level = str(payload.get("level") or "").strip().lower()
        domain = str(
            payload.get("domain") or payload.get("name") or payload.get("logger") or ""
        ).strip()
        if not level or not domain:
            return {"status": "failed", "error": "domain_and_level_required"}
        if not hass.services.has_service("logger", "set_level"):
            return {
                "status": "failed",
                "error": "logger_service_missing",
                "hint": "需启用 logger 集成",
            }
        await hass.services.async_call(
            "logger", "set_level", {domain: level}, blocking=True
        )
        return {"status": "ok", "domain": domain, "level": level}
    return {"status": "failed", "error": "unsupported_action"}


async def _supervisor(hass: HomeAssistant, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    from . import supervisor_ops

    slug = str(payload.get("slug") or "")
    if action == "probe":
        snap = await supervisor_ops.probe_supervisor(hass)
        return {
            "status": "ok" if snap.get("supported") else "failed",
            "supervisor": snap,
            **({"error": snap.get("detail")} if not snap.get("supported") else {}),
        }
    if action == "core_restart":
        return await supervisor_ops.restart_core(hass)
    if action == "core_info":
        snap = await supervisor_ops.probe_supervisor(hass)
        return {"status": "ok" if snap.get("supported") else "failed", "core": snap.get("core"), "supervisor": snap}
    if action == "addon_list":
        return await supervisor_ops.list_addons(hass)
    if action == "addon_install":
        return await supervisor_ops.install_addon(hass, slug)
    if action == "addon_uninstall":
        return await supervisor_ops.uninstall_addon(hass, slug)
    if action == "addon_start":
        return await supervisor_ops.start_addon(hass, slug)
    if action == "addon_stop":
        return await supervisor_ops.stop_addon(hass, slug)
    if action == "addon_restart":
        return await supervisor_ops.restart_addon(hass, slug)
    if action == "addon_info":
        return await supervisor_ops.addon_info(hass, slug)
    if action == "addon_logs":
        return await supervisor_ops.addon_logs(hass, slug)
    if action == "addon_options_get":
        return await supervisor_ops.addon_options_get(hass, slug)
    if action == "addon_options_set":
        options = payload.get("options") or {}
        if not isinstance(options, dict):
            return {"status": "failed", "error": "options_must_be_object"}
        return await supervisor_ops.addon_options_set(hass, slug, options)
    if action == "host_info":
        return await supervisor_ops.host_info(hass)
    if action == "host_reboot":
        return await supervisor_ops.host_reboot(hass)
    if action == "os_info":
        return await supervisor_ops.os_info(hass)
    if action == "os_update":
        return await supervisor_ops.os_update(
            hass, str(payload["version"]) if payload.get("version") else None
        )
    if action == "core_update":
        return await supervisor_ops.core_update(
            hass, str(payload["version"]) if payload.get("version") else None
        )
    if action == "addon_update":
        return await supervisor_ops.addon_update(hass, slug)
    if action == "network_info":
        return await supervisor_ops.network_info(hass)
    if action == "network_update":
        iface = str(payload.get("interface") or payload.get("iface") or "")
        enabled = payload.get("enabled")
        return await supervisor_ops.network_update(
            hass,
            iface,
            ipv4=payload.get("ipv4") if isinstance(payload.get("ipv4"), dict) else None,
            ipv6=payload.get("ipv6") if isinstance(payload.get("ipv6"), dict) else None,
            wifi=payload.get("wifi") if isinstance(payload.get("wifi"), dict) else None,
            enabled=bool(enabled) if enabled is not None else None,
        )
    if action == "hardware_info":
        return await supervisor_ops.hardware_info(hass)
    if action == "store_addons":
        return await supervisor_ops.store_addons(hass)
    return {"status": "failed", "error": "unsupported_action"}


async def _backup(hass: HomeAssistant, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    from . import supervisor_ops

    if action == "list":
        snap = await supervisor_ops.list_backups(hass)
        return {
            "status": "ok" if snap.get("supported") else "failed",
            "supervisor": snap,
        }
    if action == "create_full":
        return await supervisor_ops.create_backup(
            hass, str(payload["name"]) if payload.get("name") else None
        )
    if action == "create_partial":
        folders = payload.get("folders")
        return await supervisor_ops.create_partial_backup(
            hass,
            str(payload["name"]) if payload.get("name") else None,
            folders if isinstance(folders, list) else None,
        )
    if action == "info":
        slug = payload.get("slug") or payload.get("backup_id")
        if not slug:
            return {"status": "failed", "error": "missing_slug"}
        return await supervisor_ops.backup_info(hass, str(slug))
    if action == "restore":
        # Hard gate: require explicit confirm flag from SaaS
        if not payload.get("confirm"):
            return {
                "status": "denied",
                "error": "backup_restore_requires_confirm",
                "hint": "pass confirm=true after UI secondary confirmation",
            }
        slug = payload.get("slug") or payload.get("backup_id")
        if not slug:
            return {"status": "failed", "error": "missing_slug"}
        return await supervisor_ops.backup_restore(
            hass,
            str(slug),
            password=str(payload["password"]) if payload.get("password") else None,
        )
    return {"status": "failed", "error": "unsupported_action"}
