"""HA device registry field helpers + capability probes (doc 20 v0.3 §2.1 / §7)."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.core import HomeAssistant

_HA_VER_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")


def parse_ha_version(version: str | None) -> tuple[int, int, int] | None:
    if not version:
        return None
    m = _HA_VER_RE.match(str(version).strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def ha_version_at_least(version: str | None, major: int, minor: int, patch: int = 0) -> bool:
    parsed = parse_ha_version(version)
    if not parsed:
        return False
    return parsed >= (major, minor, patch)


def device_config_entry_id(device: Any) -> str | None:
    """Prefer 2026.8+ singular field; fallback to primary / first of config_entries."""
    ce = getattr(device, "config_entry_id", None)
    if ce:
        return str(ce)
    primary = getattr(device, "primary_config_entry", None)
    if primary:
        return str(primary)
    entries = getattr(device, "config_entries", None) or set()
    if entries:
        return str(next(iter(entries)))
    return None


def device_config_subentry_id(device: Any) -> str | None:
    sub = getattr(device, "config_subentry_id", None)
    if sub is not None:
        return str(sub) if sub else None
    # legacy map: {config_entry_id: [subentry_ids]}
    mapping = getattr(device, "config_entries_subentries", None) or {}
    ce = device_config_entry_id(device)
    if ce and isinstance(mapping, dict):
        subs = mapping.get(ce) or []
        if subs:
            return str(next(iter(subs)))
    return None


def device_parent_id(device: Any) -> str | None:
    pid = getattr(device, "parent_device_id", None)
    return str(pid) if pid else None


def _area_name(hass: HomeAssistant, area_id: str | None) -> str | None:
    if not area_id:
        return None
    try:
        from homeassistant.helpers import area_registry as ar

        registry = ar.async_get(hass)
        area = registry.async_get_area(area_id) if registry is not None else None
        return area.name if area else None
    except Exception:  # noqa: BLE001
        return None


def _serialize_set_of_tuples(value: Any) -> list[list[str]]:
    """HA identifiers/connections are frozensets of tuples → JSON lists."""
    out: list[list[str]] = []
    for item in value or set():
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append([str(item[0]), str(item[1])])
        elif isinstance(item, (list, tuple)) and len(item) == 1:
            out.append([str(item[0])])
        else:
            out.append([str(item)])
    out.sort(key=lambda x: (x[0], x[1] if len(x) > 1 else ""))
    return out


def serialize_device_row(
    hass: HomeAssistant,
    device: Any,
    *,
    include_room: bool = False,
    include_labels: bool = False,
    include_disabled: bool = False,
    devices_by_id: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Common device export for sync / snapshot / registry list (v0.3 schema)."""
    parent_id = device_parent_id(device)
    area_id = resolve_area_with_parent(device, devices_by_id)
    row: dict[str, Any] = {
        "id": device.id,
        "name": getattr(device, "name_by_user", None)
        or getattr(device, "name", None)
        or device.id,
        "manufacturer": getattr(device, "manufacturer", None),
        "model": getattr(device, "model", None),
        "sw_version": getattr(device, "sw_version", None),
        "via_device_id": getattr(device, "via_device_id", None),
        "area_id": area_id,
        "config_entry_id": device_config_entry_id(device),
        "config_subentry_id": device_config_subentry_id(device),
        "parent_device_id": parent_id,
        "is_child": bool(parent_id),
        # Logical-ID remapping fingerprint (doc 20 §7)
        "identifiers": _serialize_set_of_tuples(
            getattr(device, "identifiers", None)
        ),
        "connections": _serialize_set_of_tuples(
            getattr(device, "connections", None)
        ),
    }
    entry_type = getattr(device, "entry_type", None)
    if entry_type is not None:
        # DeviceEntryType.SERVICE → "service"; plain device often None
        row["entry_type"] = str(getattr(entry_type, "value", entry_type)).lower()
    else:
        row["entry_type"] = "device"
    if include_room:
        row["room"] = _area_name(hass, area_id)
    if include_labels:
        row["labels"] = list(getattr(device, "labels", None) or [])
    if include_disabled:
        disabled = getattr(device, "disabled_by", None)
        row["disabled_by"] = str(disabled) if disabled else None
    return row


def resolve_area_with_parent(
    device: Any, devices_by_id: dict[str, Any] | None = None
) -> str | None:
    """Child with null area_id inherits parent area (HA 2026.9 semantics)."""
    area_id = getattr(device, "area_id", None)
    if area_id:
        return area_id
    parent_id = device_parent_id(device)
    if not parent_id or not devices_by_id:
        return None
    parent = devices_by_id.get(parent_id)
    if parent is None:
        return None
    return getattr(parent, "area_id", None)


def probe_ha_features(hass: HomeAssistant, ha_version: str | None) -> dict[str, Any]:
    """Capability probe for SaaS version matrix (doc 20 §7)."""
    from homeassistant.const import __version__ as core_version

    ver = ha_version or str(core_version)
    has_single_entry_attr = False
    has_parent_attr = False
    try:
        from homeassistant.helpers import device_registry as dr

        reg = dr.async_get(hass)
        sample = next(iter(reg.devices.values()), None) if reg else None
        if sample is not None:
            has_single_entry_attr = hasattr(sample, "config_entry_id")
            has_parent_attr = hasattr(sample, "parent_device_id")
        else:
            # Empty registry: infer from HA version / DeviceEntry type hints
            has_single_entry_attr = ha_version_at_least(ver, 2026, 8)
            has_parent_attr = ha_version_at_least(ver, 2026, 9)
    except Exception:  # noqa: BLE001
        has_single_entry_attr = ha_version_at_least(ver, 2026, 8)
        has_parent_attr = ha_version_at_least(ver, 2026, 9)

    ge_2026_9 = ha_version_at_least(ver, 2026, 9)
    # WS commands introduced in 2026.9; also check handlers when possible
    ws_types = set()
    try:
        handlers = hass.data.get("websocket_api") or {}
        if isinstance(handlers, dict):
            for key in handlers:
                if isinstance(key, str) and key.startswith("config/device_registry/"):
                    ws_types.add(key)
    except Exception:  # noqa: BLE001
        pass

    def _ws_or_ver(cmd: str) -> bool:
        return cmd in ws_types or ge_2026_9

    registry_schema = (
        "single_entry_2026_8"
        if has_single_entry_attr or ha_version_at_least(ver, 2026, 8)
        else "legacy_multi_entry"
    )

    return {
        "ha_version": ver,
        "registry_schema": registry_schema,
        "child_devices": bool(has_parent_attr or ge_2026_9),
        "device_registry_remove": _ws_or_ver("config/device_registry/remove"),
        "list_linked_devices": _ws_or_ver(
            "config/device_registry/list_linked_devices"
        ),
        "list_composite_splits": _ws_or_ver(
            "config/device_registry/list_composite_splits"
        ),
    }
