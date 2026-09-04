"""HA object CRUD for Agent↔HA parity (helpers / groups / labels).

Uses HA registries and group services — not .storage direct writes.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

HELPER_DOMAINS = frozenset(
    {
        "input_boolean",
        "input_button",
        "input_number",
        "input_text",
        "input_select",
        "input_datetime",
        "counter",
        "timer",
        "schedule",
    }
)


def _slugify(name: str) -> str:
    raw = re.sub(r"[^a-zA-Z0-9_]+", "_", (name or "").strip().lower())
    raw = re.sub(r"_+", "_", raw).strip("_")
    return raw or "udhub"


def _helper_item_from_payload(domain: str, payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    item: dict[str, Any] = {"name": name or "UDHUB Helper"}
    if payload.get("icon"):
        item["icon"] = str(payload["icon"])
    if domain == "input_number":
        item["min"] = float(payload.get("min", 0))
        item["max"] = float(payload.get("max", 100))
        item["step"] = float(payload.get("step", 1))
        if payload.get("unit_of_measurement") is not None:
            item["unit_of_measurement"] = str(payload["unit_of_measurement"])
        if payload.get("mode"):
            item["mode"] = str(payload["mode"])
        if payload.get("initial") is not None:
            item["initial"] = float(payload["initial"])
    elif domain == "input_text":
        if payload.get("min") is not None:
            item["min"] = int(payload["min"])
        if payload.get("max") is not None:
            item["max"] = int(payload["max"])
        if payload.get("mode"):
            item["mode"] = str(payload["mode"])
        if payload.get("initial") is not None:
            item["initial"] = str(payload["initial"])
    elif domain == "input_select":
        options = payload.get("options") or []
        if not isinstance(options, list) or not options:
            options = ["option_1", "option_2"]
        item["options"] = [str(o) for o in options]
        if payload.get("initial") is not None:
            item["initial"] = str(payload["initial"])
    elif domain == "input_datetime":
        item["has_date"] = bool(payload.get("has_date", True))
        item["has_time"] = bool(payload.get("has_time", True))
    elif domain == "counter":
        if payload.get("initial") is not None:
            item["initial"] = int(payload["initial"])
        if payload.get("step") is not None:
            item["step"] = int(payload["step"])
        if payload.get("minimum") is not None:
            item["minimum"] = int(payload["minimum"])
        if payload.get("maximum") is not None:
            item["maximum"] = int(payload["maximum"])
    elif domain == "timer":
        if payload.get("duration"):
            item["duration"] = str(payload["duration"])
    return item


async def _get_helper_collection(hass: HomeAssistant, domain: str) -> Any | None:
    data = hass.data.get(domain)
    if data is None:
        return None
    # Modern helpers store Collection under DOMAIN key or nested.
    if hasattr(data, "async_create_item"):
        return data
    if isinstance(data, dict):
        for key in ("collection", "storage_collection", domain):
            coll = data.get(key)
            if coll is not None and hasattr(coll, "async_create_item"):
                return coll
        for val in data.values():
            if hasattr(val, "async_create_item"):
                return val
    return None


async def list_helpers(hass: HomeAssistant) -> dict[str, Any]:
    from homeassistant.helpers import entity_registry as er

    ent_reg = er.async_get(hass)
    items: list[dict[str, Any]] = []
    for entry in ent_reg.entities.values():
        if entry.domain not in HELPER_DOMAINS:
            continue
        items.append(
            {
                "entity_id": entry.entity_id,
                "domain": entry.domain,
                "unique_id": entry.unique_id,
                "name": entry.name or entry.original_name,
                "disabled": entry.disabled_by is not None,
                "labels": list(getattr(entry, "labels", None) or []),
                "area_id": entry.area_id,
            }
        )
    return {"status": "ok", "helpers": items}


async def create_helper(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    domain = str(payload.get("domain") or "").strip()
    if domain not in HELPER_DOMAINS:
        return {"status": "failed", "error": "unsupported_helper_domain", "domain": domain}
    if not str(payload.get("name") or "").strip():
        return {"status": "failed", "error": "missing_name"}

    # Ensure domain is loaded
    if domain not in hass.config.components:
        try:
            from homeassistant.setup import async_setup_component

            await async_setup_component(hass, domain, {})
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": f"setup_failed:{exc}"}

    coll = await _get_helper_collection(hass, domain)
    if coll is None:
        return {
            "status": "failed",
            "error": "helper_collection_unavailable",
            "domain": domain,
            "hint": "HA helper storage collection not found; upgrade HA or create via UI once",
        }

    item = _helper_item_from_payload(domain, payload)
    try:
        created = await coll.async_create_item(item)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.exception("helper_create failed domain=%s", domain)
        return {"status": "failed", "error": str(exc), "domain": domain}

    entity_id = None
    if isinstance(created, dict):
        entity_id = created.get("entity_id") or created.get("id")
        uid = created.get("id") or created.get("unique_id")
    else:
        uid = getattr(created, "id", None) or getattr(created, "unique_id", None)
        entity_id = getattr(created, "entity_id", None)

    if not entity_id and uid:
        entity_id = f"{domain}.{uid}"

    # Optional area assignment
    area_id = payload.get("area_id")
    area_name = payload.get("area_name") or payload.get("room")
    if entity_id and (area_id or area_name):
        try:
            from homeassistant.helpers import entity_registry as er
            from homeassistant.helpers import area_registry as ar

            ent_reg = er.async_get(hass)
            if area_name and not area_id:
                area_reg = ar.async_get(hass)
                for area in area_reg.areas.values():
                    if area.name == area_name:
                        area_id = area.id
                        break
                if not area_id:
                    created_area = area_reg.async_create(str(area_name))
                    area_id = created_area.id
            if area_id:
                ent_reg.async_update_entity(entity_id, area_id=str(area_id))
        except Exception:  # noqa: BLE001
            _LOGGER.debug("helper area assign failed", exc_info=True)

    return {
        "status": "ok",
        "domain": domain,
        "entity_id": entity_id,
        "item": created if isinstance(created, dict) else {"id": uid, "name": item["name"]},
    }


async def update_helper(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    domain = str(payload.get("domain") or "").strip()
    item_id = payload.get("id") or payload.get("unique_id") or payload.get("item_id")
    entity_id = payload.get("entity_id")
    if entity_id and not domain:
        domain = str(entity_id).split(".", 1)[0]
    if domain not in HELPER_DOMAINS:
        return {"status": "failed", "error": "unsupported_helper_domain"}

    if not item_id and entity_id:
        from homeassistant.helpers import entity_registry as er

        ent = er.async_get(hass).async_get(str(entity_id))
        if ent:
            item_id = ent.unique_id

    if not item_id:
        return {"status": "failed", "error": "missing_id_or_entity_id"}

    coll = await _get_helper_collection(hass, domain)
    if coll is None or not hasattr(coll, "async_update_item"):
        return {"status": "failed", "error": "helper_collection_unavailable"}

    changes = _helper_item_from_payload(domain, payload)
    # Don't force-rename if name omitted
    if not payload.get("name"):
        changes.pop("name", None)
    try:
        updated = await coll.async_update_item(str(item_id), changes)
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}
    return {"status": "ok", "domain": domain, "id": item_id, "item": updated}


async def delete_helper(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    domain = str(payload.get("domain") or "").strip()
    item_id = payload.get("id") or payload.get("unique_id") or payload.get("item_id")
    entity_id = payload.get("entity_id")
    if entity_id and not domain:
        domain = str(entity_id).split(".", 1)[0]
    if domain not in HELPER_DOMAINS:
        return {"status": "failed", "error": "unsupported_helper_domain"}

    if not item_id and entity_id:
        from homeassistant.helpers import entity_registry as er

        ent = er.async_get(hass).async_get(str(entity_id))
        if ent:
            item_id = ent.unique_id

    if not item_id:
        return {"status": "failed", "error": "missing_id_or_entity_id"}

    coll = await _get_helper_collection(hass, domain)
    if coll is None or not hasattr(coll, "async_delete_item"):
        return {"status": "failed", "error": "helper_collection_unavailable"}
    try:
        await coll.async_delete_item(str(item_id))
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}
    return {"status": "ok", "domain": domain, "id": item_id}


async def list_groups(hass: HomeAssistant) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for state in hass.states.async_all("group"):
        attrs = state.attributes or {}
        items.append(
            {
                "entity_id": state.entity_id,
                "name": state.name,
                "entity_id_list": list(attrs.get("entity_id") or []),
                "order": attrs.get("order"),
            }
        )
    return {"status": "ok", "groups": items}


async def upsert_group(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    object_id = str(payload.get("object_id") or "").strip() or _slugify(name or "udhub_group")
    entities = payload.get("entities") or payload.get("entity_ids") or []
    if not isinstance(entities, list) or not entities:
        return {"status": "failed", "error": "missing_entities"}
    entity_ids = [str(e) for e in entities if e]
    if len(entity_ids) < 1:
        return {"status": "failed", "error": "missing_entities"}

    data: dict[str, Any] = {
        "object_id": object_id,
        "entities": entity_ids,
        "all": bool(payload.get("all", False)),
    }
    if name:
        data["name"] = name
    if payload.get("icon"):
        data["icon"] = str(payload["icon"])

    try:
        await hass.services.async_call("group", "set", data, blocking=True)
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}

    entity_id = f"group.{object_id}"
    return {
        "status": "ok",
        "entity_id": entity_id,
        "object_id": object_id,
        "entities": entity_ids,
        "name": name or object_id,
    }


async def delete_group(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    object_id = str(payload.get("object_id") or "").strip()
    entity_id = str(payload.get("entity_id") or "").strip()
    if not object_id and entity_id.startswith("group."):
        object_id = entity_id.split(".", 1)[1]
    if not object_id:
        return {"status": "failed", "error": "missing_object_id"}
    try:
        await hass.services.async_call(
            "group", "remove", {"object_id": object_id}, blocking=True
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}
    return {"status": "ok", "object_id": object_id, "entity_id": f"group.{object_id}"}


def _label_to_dict(entry: Any) -> dict[str, Any]:
    return {
        "label_id": getattr(entry, "label_id", None),
        "name": getattr(entry, "name", None),
        "color": getattr(entry, "color", None),
        "icon": getattr(entry, "icon", None),
        "description": getattr(entry, "description", None),
    }


async def list_labels(hass: HomeAssistant) -> dict[str, Any]:
    try:
        from homeassistant.helpers import label_registry as lr
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": f"label_registry_unavailable:{exc}"}
    reg = lr.async_get(hass)
    items = [_label_to_dict(e) for e in reg.async_list_labels()]
    return {"status": "ok", "labels": items}


async def create_label(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        from homeassistant.helpers import label_registry as lr
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": f"label_registry_unavailable:{exc}"}
    name = str(payload.get("name") or "").strip()
    if not name:
        return {"status": "failed", "error": "missing_name"}
    reg = lr.async_get(hass)
    existing = reg.async_get_label_by_name(name)
    if existing:
        return {"status": "ok", "created": False, "label": _label_to_dict(existing)}
    try:
        entry = reg.async_create(
            name,
            color=payload.get("color"),
            icon=payload.get("icon"),
            description=payload.get("description"),
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}
    return {"status": "ok", "created": True, "label": _label_to_dict(entry)}


async def delete_label(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        from homeassistant.helpers import label_registry as lr
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": f"label_registry_unavailable:{exc}"}
    label_id = payload.get("label_id") or payload.get("id")
    name = payload.get("name")
    reg = lr.async_get(hass)
    if not label_id and name:
        entry = reg.async_get_label_by_name(str(name))
        label_id = entry.label_id if entry else None
    if not label_id:
        return {"status": "failed", "error": "missing_label_id"}
    try:
        reg.async_delete(str(label_id))
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}
    return {"status": "ok", "label_id": label_id}


async def assign_labels(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    """Assign labels to entity / device / area.

    Body: labels=[...] or label_ids=[...]
          mode=set|add|remove (default set)
          entity_id | device_id | area_id
    """
    try:
        from homeassistant.helpers import label_registry as lr
        from homeassistant.helpers import entity_registry as er
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import area_registry as ar
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": f"registry_unavailable:{exc}"}

    label_reg = lr.async_get(hass)
    raw_labels = payload.get("labels") or payload.get("label_ids") or []
    if not isinstance(raw_labels, list):
        return {"status": "failed", "error": "labels_must_be_list"}

    resolved: list[str] = []
    for item in raw_labels:
        s = str(item).strip()
        if not s:
            continue
        if label_reg.async_get_label(s):
            resolved.append(s)
            continue
        by_name = label_reg.async_get_label_by_name(s)
        if by_name:
            resolved.append(by_name.label_id)
            continue
        # auto-create by name for SaaS tag UX
        created = label_reg.async_create(s)
        resolved.append(created.label_id)

    mode = str(payload.get("mode") or "set").strip().lower()
    entity_id = payload.get("entity_id")
    device_id = payload.get("device_id")
    area_id = payload.get("area_id")

    def _merge(existing: set[str]) -> set[str]:
        if mode == "add":
            return set(existing) | set(resolved)
        if mode == "remove":
            return set(existing) - set(resolved)
        return set(resolved)

    try:
        if entity_id:
            ent_reg = er.async_get(hass)
            ent = ent_reg.async_get(str(entity_id))
            if not ent:
                return {"status": "failed", "error": "entity_not_found"}
            current = set(getattr(ent, "labels", None) or [])
            next_labels = _merge(current)
            ent_reg.async_update_entity(str(entity_id), labels=next_labels)
            return {
                "status": "ok",
                "target": "entity",
                "entity_id": entity_id,
                "labels": sorted(next_labels),
            }
        if device_id:
            dev_reg = dr.async_get(hass)
            dev = dev_reg.async_get(str(device_id))
            if not dev:
                return {"status": "failed", "error": "device_not_found"}
            current = set(getattr(dev, "labels", None) or [])
            next_labels = _merge(current)
            dev_reg.async_update_device(str(device_id), labels=next_labels)
            return {
                "status": "ok",
                "target": "device",
                "device_id": device_id,
                "labels": sorted(next_labels),
            }
        if area_id:
            area_reg = ar.async_get(hass)
            area = area_reg.async_get_area(str(area_id))
            if not area:
                return {"status": "failed", "error": "area_not_found"}
            current = set(getattr(area, "labels", None) or [])
            next_labels = _merge(current)
            area_reg.async_update(str(area_id), labels=next_labels)
            return {
                "status": "ok",
                "target": "area",
                "area_id": area_id,
                "labels": sorted(next_labels),
            }
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}

    return {"status": "failed", "error": "missing_entity_id_or_device_id_or_area_id"}


async def update_label(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        from homeassistant.helpers import label_registry as lr
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": f"label_registry_unavailable:{exc}"}
    label_id = payload.get("label_id") or payload.get("id")
    if not label_id:
        return {"status": "failed", "error": "missing_label_id"}
    reg = lr.async_get(hass)
    kwargs: dict[str, Any] = {}
    for key in ("name", "color", "icon", "description"):
        if key in payload:
            kwargs[key] = payload[key]
    try:
        entry = reg.async_update(str(label_id), **kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}
    return {"status": "ok", "label": _label_to_dict(entry)}


async def _list_linked_devices(hass: HomeAssistant, device_id: str) -> list[str]:
    """Return sibling device ids that share identifiers/connections (HA 2026.9)."""
    from homeassistant.helpers import device_registry as dr

    reg = dr.async_get(hass)
    device = reg.async_get(device_id)
    if not device:
        return []
    # Prefer registry helper when present
    helper = getattr(reg, "async_get_devices_sharing_identifier", None) or getattr(
        reg, "async_get_linked_devices", None
    )
    if callable(helper):
        try:
            result = helper(device_id)
            if hasattr(result, "__await__"):
                result = await result  # type: ignore[misc]
            if isinstance(result, (list, set, tuple)):
                return [str(x.id if hasattr(x, "id") else x) for x in result]
            if isinstance(result, dict) and "linked_devices" in result:
                return [str(x) for x in result["linked_devices"]]
        except Exception:  # noqa: BLE001
            _LOGGER.debug("linked devices helper failed", exc_info=True)

    # Fallback: match by identifiers / connections within other config entries
    linked: set[str] = set()
    my_idents = set(getattr(device, "identifiers", None) or set())
    my_conns = set(getattr(device, "connections", None) or set())
    my_ce = getattr(device, "config_entry_id", None)
    if my_ce is None:
        entries = getattr(device, "config_entries", None) or set()
        my_ce = next(iter(entries), None) if entries else None
    for other in reg.devices.values():
        if other.id == device_id:
            continue
        other_ce = getattr(other, "config_entry_id", None)
        if other_ce is None:
            oentries = getattr(other, "config_entries", None) or set()
            other_ce = next(iter(oentries), None) if oentries else None
        if my_ce and other_ce and str(my_ce) == str(other_ce):
            continue
        o_idents = set(getattr(other, "identifiers", None) or set())
        o_conns = set(getattr(other, "connections", None) or set())
        if my_idents & o_idents or my_conns & o_conns:
            linked.add(other.id)
    return sorted(linked)


async def _list_composite_splits(hass: HomeAssistant) -> dict[str, Any]:
    """Map pre-migration composite device ids → split devices (HA 2026.9)."""
    from homeassistant.helpers import device_registry as dr

    reg = dr.async_get(hass)
    helper = getattr(reg, "async_get_composite_device_splits", None) or getattr(
        dr, "async_get_composite_device_splits", None
    )
    if callable(helper):
        try:
            result = helper(reg) if helper is getattr(dr, "async_get_composite_device_splits", None) else helper()
            if hasattr(result, "__await__"):
                result = await result  # type: ignore[misc]
            if isinstance(result, dict):
                out: dict[str, Any] = {}
                for old_id, info in result.items():
                    if isinstance(info, dict):
                        out[str(old_id)] = {
                            "split_ids": [str(x) for x in (info.get("split_ids") or [])],
                            "primary_id": (
                                str(info["primary_id"])
                                if info.get("primary_id") is not None
                                else None
                            ),
                        }
                    else:
                        out[str(old_id)] = {"split_ids": [], "primary_id": None}
                return out
        except Exception:  # noqa: BLE001
            _LOGGER.debug("composite splits helper failed", exc_info=True)
    return {}


async def dispatch_registry(
    hass: HomeAssistant, action: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Unified registry actions for catalog kind=registry."""
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import area_registry as ar

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)

    floor_reg = None
    try:
        from homeassistant.helpers import floor_registry as fr

        floor_reg = fr.async_get(hass)
    except Exception:  # noqa: BLE001
        floor_reg = None

    cat_reg = None
    try:
        from homeassistant.helpers import category_registry as cr

        cat_reg = cr.async_get(hass)
    except Exception:  # noqa: BLE001
        cat_reg = None

    if action == "entity_list":
        items = []
        for e in ent_reg.entities.values():
            items.append(
                {
                    "entity_id": e.entity_id,
                    "domain": e.domain,
                    "name": e.name or e.original_name,
                    "disabled_by": str(e.disabled_by) if e.disabled_by else None,
                    "hidden_by": str(getattr(e, "hidden_by", None) or "") or None,
                    "area_id": e.area_id,
                    "device_id": e.device_id,
                    "labels": list(getattr(e, "labels", None) or []),
                    "categories": dict(getattr(e, "categories", None) or {}),
                    "aliases": list(getattr(e, "aliases", None) or []),
                }
            )
            if len(items) >= 2000:
                break
        return {"status": "ok", "entities": items}
    if action == "entity_get":
        eid = payload.get("entity_id")
        if not eid:
            return {"status": "failed", "error": "missing_entity_id"}
        e = ent_reg.async_get(str(eid))
        if not e:
            return {"status": "failed", "error": "not_found"}
        return {
            "status": "ok",
            "entity": {
                "entity_id": e.entity_id,
                "unique_id": e.unique_id,
                "platform": e.platform,
                "name": e.name,
                "area_id": e.area_id,
                "device_id": e.device_id,
                "disabled_by": str(e.disabled_by) if e.disabled_by else None,
                "labels": list(getattr(e, "labels", None) or []),
                "aliases": list(getattr(e, "aliases", None) or []),
            },
        }
    if action in ("entity_update", "entity_enable", "entity_disable", "entity_hide", "entity_unhide"):
        eid = payload.get("entity_id")
        if not eid:
            return {"status": "failed", "error": "missing_entity_id"}
        kwargs: dict[str, Any] = {}
        if action == "entity_disable":
            kwargs["disabled_by"] = er.RegistryEntryDisabler.USER
        elif action == "entity_enable":
            kwargs["disabled_by"] = None
        elif action == "entity_hide":
            try:
                kwargs["hidden_by"] = er.RegistryEntryHider.USER
            except Exception:  # noqa: BLE001
                kwargs["hidden_by"] = "user"
        elif action == "entity_unhide":
            kwargs["hidden_by"] = None
        else:
            for key in ("name", "area_id", "icon", "new_entity_id"):
                if key in payload:
                    kwargs[key] = payload[key]
            if "aliases" in payload and isinstance(payload["aliases"], list):
                kwargs["aliases"] = set(str(x) for x in payload["aliases"])
            if "labels" in payload and isinstance(payload["labels"], list):
                kwargs["labels"] = set(str(x) for x in payload["labels"])
        try:
            ent_reg.async_update_entity(str(eid), **kwargs)
            return {"status": "ok", "entity_id": eid, "action": action}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc)}
    if action == "entity_remove":
        eid = payload.get("entity_id")
        if not eid:
            return {"status": "failed", "error": "missing_entity_id"}
        try:
            ent_reg.async_remove(str(eid))
            return {"status": "ok", "entity_id": eid}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc)}

    if action == "device_list":
        from .device_registry_fields import serialize_device_row

        by_id = {d.id: d for d in dev_reg.devices.values()}
        items = [
            serialize_device_row(
                hass,
                d,
                include_labels=True,
                include_disabled=True,
                devices_by_id=by_id,
            )
            for d in dev_reg.devices.values()
        ]
        return {"status": "ok", "devices": items}
    if action == "device_get":
        from .device_registry_fields import serialize_device_row

        did = payload.get("device_id") or payload.get("id")
        d = dev_reg.async_get(str(did)) if did else None
        if not d:
            return {"status": "failed", "error": "not_found"}
        by_id = {x.id: x for x in dev_reg.devices.values()}
        return {
            "status": "ok",
            "device": serialize_device_row(
                hass, d, include_labels=True, include_disabled=True, devices_by_id=by_id
            ),
        }
    if action == "device_update":
        did = payload.get("device_id") or payload.get("id")
        if not did:
            return {"status": "failed", "error": "missing_device_id"}
        kwargs = {}
        if "name" in payload or "name_by_user" in payload:
            kwargs["name_by_user"] = payload.get("name_by_user") or payload.get("name")
        if "area_id" in payload:
            kwargs["area_id"] = payload.get("area_id")
        try:
            dev_reg.async_update_device(str(did), **kwargs)
            return {"status": "ok", "device_id": did}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc)}
    if action == "device_remove":
        from homeassistant.const import __version__ as core_version

        from .device_registry_fields import probe_ha_features

        features = probe_ha_features(hass, str(core_version))
        if not features.get("device_registry_remove"):
            return {
                "status": "failed",
                "error": "unsupported",
                "hint": "device_registry_remove requires Home Assistant Core ≥ 2026.9",
                "ha_features": features,
            }
        did = payload.get("device_id") or payload.get("id")
        if not did:
            return {"status": "failed", "error": "missing_device_id"}
        try:
            dev_reg.async_remove_device(str(did))
            return {"status": "ok", "device_id": did}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc)}

    if action == "list_linked_devices":
        from homeassistant.const import __version__ as core_version

        from .device_registry_fields import probe_ha_features

        features = probe_ha_features(hass, str(core_version))
        if not features.get("list_linked_devices"):
            return {
                "status": "failed",
                "error": "unsupported",
                "hint": "list_linked_devices requires Home Assistant Core ≥ 2026.9",
                "ha_features": features,
            }
        did = payload.get("device_id") or payload.get("id")
        if not did:
            return {"status": "failed", "error": "missing_device_id"}
        linked = await _list_linked_devices(hass, str(did))
        return {"status": "ok", "device_id": did, "linked_devices": linked}

    if action == "list_composite_splits":
        from homeassistant.const import __version__ as core_version

        from .device_registry_fields import probe_ha_features

        features = probe_ha_features(hass, str(core_version))
        if not features.get("list_composite_splits"):
            return {
                "status": "failed",
                "error": "unsupported",
                "hint": "list_composite_splits requires Home Assistant Core ≥ 2026.9",
                "ha_features": features,
            }
        splits = await _list_composite_splits(hass)
        return {"status": "ok", "composite_splits": splits}

    if action == "area_list":
        return {
            "status": "ok",
            "areas": [
                {
                    "id": a.id,
                    "name": a.name,
                    "floor_id": getattr(a, "floor_id", None),
                    "labels": list(getattr(a, "labels", None) or []),
                }
                for a in area_reg.areas.values()
            ],
        }
    if action == "area_create":
        name = payload.get("name") or payload.get("area_name")
        if not name:
            return {"status": "failed", "error": "missing_name"}
        created = area_reg.async_create(str(name))
        return {"status": "ok", "area_id": created.id, "name": created.name}
    if action == "area_update":
        aid = payload.get("area_id") or payload.get("id")
        if not aid:
            return {"status": "failed", "error": "missing_area_id"}
        kwargs = {}
        if "name" in payload:
            kwargs["name"] = payload["name"]
        if "floor_id" in payload:
            kwargs["floor_id"] = payload["floor_id"]
        try:
            area_reg.async_update(str(aid), **kwargs)
            return {"status": "ok", "area_id": aid}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc)}
    if action == "area_delete":
        aid = payload.get("area_id") or payload.get("id")
        if not aid:
            return {"status": "failed", "error": "missing_area_id"}
        try:
            area_reg.async_delete(str(aid))
            return {"status": "ok", "area_id": aid}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc)}

    if action == "floor_list":
        if floor_reg is None:
            return {"status": "failed", "error": "floor_registry_unavailable"}
        floors = getattr(floor_reg, "floors", {}) or {}
        return {
            "status": "ok",
            "floors": [
                {"floor_id": f.floor_id, "name": f.name, "level": getattr(f, "level", None)}
                for f in floors.values()
            ],
        }
    if action == "floor_create":
        if floor_reg is None:
            return {"status": "failed", "error": "floor_registry_unavailable"}
        name = payload.get("name") or payload.get("floor_name")
        if not name:
            return {"status": "failed", "error": "missing_name"}
        fl = floor_reg.async_create(str(name))
        return {"status": "ok", "floor_id": fl.floor_id, "name": fl.name}
    if action == "floor_update":
        if floor_reg is None:
            return {"status": "failed", "error": "floor_registry_unavailable"}
        fid = payload.get("floor_id") or payload.get("id")
        if not fid:
            return {"status": "failed", "error": "missing_floor_id"}
        kwargs = {}
        if "name" in payload:
            kwargs["name"] = payload["name"]
        if "level" in payload:
            kwargs["level"] = payload["level"]
        try:
            floor_reg.async_update(str(fid), **kwargs)
            return {"status": "ok", "floor_id": fid}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc)}
    if action == "floor_delete":
        if floor_reg is None:
            return {"status": "failed", "error": "floor_registry_unavailable"}
        fid = payload.get("floor_id") or payload.get("id")
        if not fid:
            return {"status": "failed", "error": "missing_floor_id"}
        try:
            floor_reg.async_delete(str(fid))
            return {"status": "ok", "floor_id": fid}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc)}
    if action == "area_set_floor":
        aid = payload.get("area_id")
        fid = payload.get("floor_id")
        if not aid:
            return {"status": "failed", "error": "missing_area_id"}
        try:
            area_reg.async_update(str(aid), floor_id=fid)
            return {"status": "ok", "area_id": aid, "floor_id": fid}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc)}

    if action.startswith("category_"):
        if cat_reg is None:
            return {"status": "failed", "error": "category_registry_unavailable"}
        scope = str(payload.get("scope") or "entity")
        if action == "category_list":
            cats = []
            try:
                # API varies by HA version
                data = getattr(cat_reg, "categories", {}) or {}
                scope_map = data.get(scope) if isinstance(data, dict) else data
                for cid, entry in (scope_map or {}).items():
                    cats.append(
                        {
                            "category_id": getattr(entry, "category_id", cid),
                            "name": getattr(entry, "name", str(cid)),
                            "scope": scope,
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                return {"status": "failed", "error": str(exc)}
            return {"status": "ok", "categories": cats, "scope": scope}
        if action == "category_create":
            name = payload.get("name")
            if not name:
                return {"status": "failed", "error": "missing_name"}
            try:
                entry = cat_reg.async_create(scope, str(name))
                return {
                    "status": "ok",
                    "category_id": getattr(entry, "category_id", None),
                    "name": getattr(entry, "name", name),
                }
            except Exception as exc:  # noqa: BLE001
                return {"status": "failed", "error": str(exc)}
        if action == "category_delete":
            cid = payload.get("category_id") or payload.get("id")
            if not cid:
                return {"status": "failed", "error": "missing_category_id"}
            try:
                cat_reg.async_delete(scope, str(cid))
                return {"status": "ok", "category_id": cid}
            except Exception as exc:  # noqa: BLE001
                return {"status": "failed", "error": str(exc)}
        if action == "category_update":
            cid = payload.get("category_id") or payload.get("id")
            if not cid:
                return {"status": "failed", "error": "missing_category_id"}
            try:
                cat_reg.async_update(scope, str(cid), name=payload.get("name"))
                return {"status": "ok", "category_id": cid}
            except Exception as exc:  # noqa: BLE001
                return {"status": "failed", "error": str(exc)}
        if action == "category_assign":
            eid = payload.get("entity_id")
            cid = payload.get("category_id")
            if not eid:
                return {"status": "failed", "error": "missing_entity_id"}
            try:
                cats = dict(getattr(ent_reg.async_get(str(eid)), "categories", None) or {})
                cats[scope] = cid
                ent_reg.async_update_entity(str(eid), categories=cats)
                return {"status": "ok", "entity_id": eid, "categories": cats}
            except Exception as exc:  # noqa: BLE001
                return {"status": "failed", "error": str(exc)}

    if action.startswith("zone_"):
        if action == "zone_list":
            from homeassistant.helpers import entity_registry as er

            ent_reg = er.async_get(hass)
            zones: list[dict[str, Any]] = []
            for st in hass.states.async_all("zone"):
                ent = ent_reg.async_get(st.entity_id)
                zones.append(
                    {
                        "entity_id": st.entity_id,
                        "name": st.name,
                        "latitude": st.attributes.get("latitude"),
                        "longitude": st.attributes.get("longitude"),
                        "radius": st.attributes.get("radius"),
                        "entry_id": (
                            str(ent.config_entry_id)
                            if ent and ent.config_entry_id
                            else None
                        ),
                        "unique_id": (
                            str(ent.unique_id) if ent and ent.unique_id else None
                        ),
                    }
                )
            return {"status": "ok", "zones": zones}
        # zone create/update/delete: zone is a config-flow integration since
        # HA 2023.6 — drive via kind=integration flow_start/options_flow_start
        # and integration.remove with the zone config entry.
        return {
            "status": "failed",
            "error": "zone_mutation_use_config_flow",
            "action": action,
            "hint": "zone is a config-entry integration: use kind=integration flow_start(domain=zone) to create, integration.remove(entry_id) to delete",
        }

    if action.startswith("person_"):
        if action == "person_list":
            from homeassistant.helpers import entity_registry as er

            ent_reg = er.async_get(hass)
            persons: list[dict[str, Any]] = []
            for st in hass.states.async_all("person"):
                ent = ent_reg.async_get(st.entity_id)
                persons.append(
                    {
                        "entity_id": st.entity_id,
                        "name": st.name,
                        "id": (
                            str(ent.unique_id) if ent and ent.unique_id else None
                        ),
                        "user_id": st.attributes.get("user_id"),
                        "device_trackers": st.attributes.get("device_trackers")
                        or [],
                    }
                )
            return {"status": "ok", "persons": persons}
        # create/update/delete via person storage collection (best effort)
        if action in ("person_create", "person_update", "person_delete"):
            from . import ha_config_objects

            sub = action.split("_", 1)[1]
            out = await ha_config_objects._collection_crud(hass, "person", sub, payload)
            out["catalog_kind"] = "registry"
            out["catalog_action"] = action
            return out
        return {
            "status": "failed",
            "error": "person_mutation_use_ha_ws_or_collection",
            "action": action,
        }

    return {"status": "failed", "error": "unsupported_action", "action": action}
