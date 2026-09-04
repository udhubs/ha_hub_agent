"""Agent capability catalog (catalog_version freeze).

SaaS must only call kinds/actions listed here. New console features should
not require Agent bumps until catalog_version increases.
"""

from __future__ import annotations

from typing import Any

CATALOG_VERSION = 8

# Generic kinds → allowed actions
GENERIC_KINDS: dict[str, list[str]] = {
    "registry": [
        "entity_list",
        "entity_get",
        "entity_update",
        "entity_remove",
        "entity_enable",
        "entity_disable",
        "entity_hide",
        "entity_unhide",
        "device_list",
        "device_get",
        "device_update",
        "device_remove",
        "list_linked_devices",
        "list_composite_splits",
        "area_list",
        "area_create",
        "area_update",
        "area_delete",
        "floor_list",
        "floor_create",
        "floor_update",
        "floor_delete",
        "area_set_floor",
        "category_list",
        "category_create",
        "category_update",
        "category_delete",
        "category_assign",
        "zone_list",
        "zone_create",
        "zone_update",
        "zone_delete",
        "person_list",
        "person_create",
        "person_update",
        "person_delete",
    ],
    "helper": ["list", "create", "update", "delete"],
    "group": ["list", "upsert", "delete"],
    "label": ["list", "create", "update", "delete", "assign"],
    "integration": [
        "list",
        "reload",
        "remove",
        "enable",
        "disable",
        "get",
        "options_get",
        "options_set",
        "rename_title",
        "devices_list",
        "entities_list",
        # Config Flow remote shell (local host bind)
        "handlers",
        "descriptions",
        "flow_start",
        "flow_step",
        "flow_abort",
        "flow_get",
        "flow_progress",
        "options_flow_start",
        "options_flow_step",
    ],
    "user": ["list", "create", "delete", "update"],
    "ha_system": [
        "restart",
        "check_config",
        "reload_core",
        "reload_domain",
        "reload_all",
        "config_get",
        "config_update",
        "analytics_get",
        "logger_set_default",
        "logger_set_level",
    ],
    "supervisor": [
        "probe",
        "core_restart",
        "core_info",
        "addon_list",
        "addon_install",
        "addon_uninstall",
        "addon_start",
        "addon_stop",
        "addon_restart",
        "addon_info",
        "addon_logs",
        "addon_options_get",
        "addon_options_set",
        "host_info",
        "host_reboot",
        "os_info",
        "os_update",
        "core_update",
        "addon_update",
        "network_info",
        "network_update",
        "hardware_info",
        "store_addons",
    ],
    "backup": [
        "list",
        "create_full",
        "create_partial",
        "info",
        "restore",
    ],
    "hacs": ["probe", "list", "install", "update", "uninstall"],
    "query": [
        "services_list",
        "states_get",
        "history",
        "logbook",
        "statistics",
        "energy_get",
    ],
    "event": ["fire"],
    "template": ["render"],
    "blueprint": ["list", "import", "delete"],
    "automation_config": [
        "list",
        "get",
        "get_config",
        "create",
        "update",
        "delete",
        "duplicate",
        "trigger",
        "trace_list",
        "trace_get",
        "export_managed_yaml",
        "disable_source",
        "update_source",
    ],
    "script_config": [
        "list",
        "get",
        "get_config",
        "create",
        "update",
        "delete",
        "duplicate",
        "run",
        "export_managed_yaml",
        "update_source",
    ],
    "scene_config": [
        "list",
        "get",
        "get_config",
        "create",
        "update",
        "delete",
        "apply",
        "export_managed_yaml",
        "update_source",
    ],
    "config_snapshot": ["collect"],
    "file": ["read", "write", "delete", "list"],
    "ha_ws": ["call"],
    # Passthrough / already shipped
    "call_service": ["call"],
    "config_write": ["write", "delete"],
    "scene_create": ["create"],
    "registry_update": ["update"],
    "sync_full": ["run"],
    "agent_update": ["run"],
    "agent_ping": ["run"],
    "ssh_bypass_probe": ["run"],
    "ssh_bypass_ensure": ["run"],
    "packages_probe": ["run"],
    "packages_ensure": ["run"],
    "shell_command": ["run"],
}

# Legacy explicit kinds → (generic_kind, action)
KIND_ALIASES: dict[str, tuple[str, str]] = {
    "helper_list": ("helper", "list"),
    "helper_create": ("helper", "create"),
    "helper_update": ("helper", "update"),
    "helper_delete": ("helper", "delete"),
    "group_list": ("group", "list"),
    "group_upsert": ("group", "upsert"),
    "group_delete": ("group", "delete"),
    "label_list": ("label", "list"),
    "label_create": ("label", "create"),
    "label_update": ("label", "update"),
    "label_delete": ("label", "delete"),
    "label_assign": ("label", "assign"),
    "integration_list": ("integration", "list"),
    "integration_reload": ("integration", "reload"),
    "integration_remove": ("integration", "remove"),
    "integration_rename_title": ("integration", "rename_title"),
    "integration_handlers": ("integration", "handlers"),
    "integration_flow_start": ("integration", "flow_start"),
    "integration_flow_step": ("integration", "flow_step"),
    "integration_flow_abort": ("integration", "flow_abort"),
    "integration_flow_progress": ("integration", "flow_progress"),
    "user_list": ("user", "list"),
    "user_create": ("user", "create"),
    "user_delete": ("user", "delete"),
    "ha_restart": ("ha_system", "restart"),
    "ha_check_config": ("ha_system", "check_config"),
    "ha_reload_core": ("ha_system", "reload_core"),
    "file_read": ("file", "read"),
    "file_write": ("file", "write"),
    "file_delete": ("file", "delete"),
    "file_list": ("file", "list"),
    "supervisor_probe": ("supervisor", "probe"),
    "supervisor_core_restart": ("supervisor", "core_restart"),
    "supervisor_backup_list": ("backup", "list"),
    "supervisor_backup_create": ("backup", "create_full"),
    "supervisor_addon_list": ("supervisor", "addon_list"),
    "supervisor_addon_install": ("supervisor", "addon_install"),
    "supervisor_addon_uninstall": ("supervisor", "addon_uninstall"),
    "supervisor_addon_start": ("supervisor", "addon_start"),
    "supervisor_addon_stop": ("supervisor", "addon_stop"),
    "supervisor_addon_restart": ("supervisor", "addon_restart"),
    "supervisor_host_info": ("supervisor", "host_info"),
    "supervisor_host_reboot": ("supervisor", "host_reboot"),
    "supervisor_os_info": ("supervisor", "os_info"),
    "supervisor_os_update": ("supervisor", "os_update"),
    "config_delete": ("config_write", "delete"),
    "sync_full": ("sync_full", "run"),
}

# Flat list of legacy kinds still accepted by dispatch
LEGACY_KIND_NAMES: list[str] = sorted(
    set(KIND_ALIASES.keys())
    | {
        "call_service",
        "config_write",
        "scene_create",
        "registry_update",
        "sync_full",
        "agent_update",
        "agent_ping",
        "ssh_bypass_probe",
        "ssh_bypass_ensure",
        "packages_probe",
        "packages_ensure",
        "shell_command",
    }
)

HA_WS_ALLOWLIST: frozenset[str] = frozenset(
    {
        "config/entity_registry/list",
        "config/entity_registry/get",
        "config/entity_registry/update",
        "config/entity_registry/remove",
        "config/device_registry/list",
        "config/device_registry/update",
        "config/device_registry/remove",
        "config/device_registry/list_linked_devices",
        "config/device_registry/list_composite_splits",
        "config/area_registry/list",
        "config/area_registry/create",
        "config/area_registry/update",
        "config/area_registry/delete",
        "config/floor_registry/list",
        "config/floor_registry/create",
        "config/floor_registry/update",
        "config/floor_registry/delete",
        "config/label_registry/list",
        "config/label_registry/create",
        "config/label_registry/update",
        "config/label_registry/delete",
        "config/category_registry/list",
        "config/category_registry/create",
        "config/category_registry/update",
        "config/category_registry/delete",
        "config/config_entries/get",
        "config/config_entries/disable",
        "config/config_entries/enable",
        "automation/config",
        "script/config",
        "scene/config",
        "blueprint/list",
        "blueprint/import",
        "blueprint/delete",
        "render_template",
        "history/history_during_period",
        "logbook/get_events",
        "recorder/statistics_during_period",
        "get_services",
        "get_states",
        "fire_event",
    }
)


def resolve_kind_action(kind: str, action: str | None) -> tuple[str, str]:
    """Map legacy or generic kind to (generic_kind, action)."""
    k = str(kind or "").strip()
    a = str(action or "").strip() if action else ""
    if k in KIND_ALIASES:
        gk, ga = KIND_ALIASES[k]
        return gk, a or ga
    if k in GENERIC_KINDS:
        if not a:
            # default action for passthrough kinds
            actions = GENERIC_KINDS[k]
            a = actions[0] if actions else "run"
        return k, a
    return k, a or "run"


def is_action_allowed(kind: str, action: str) -> bool:
    gk, ga = resolve_kind_action(kind, action)
    allowed = GENERIC_KINDS.get(gk)
    if not allowed:
        # unknown generic — deny unless legacy exact kind still handled elsewhere
        return gk in LEGACY_KIND_NAMES or kind in LEGACY_KIND_NAMES
    return ga in allowed


def build_manifest(agent_version: str) -> dict[str, Any]:
    return {
        "catalog_version": CATALOG_VERSION,
        "agent_version": agent_version,
        "kinds": {k: list(v) for k, v in GENERIC_KINDS.items()},
        "aliases": {k: {"kind": v[0], "action": v[1]} for k, v in KIND_ALIASES.items()},
        "legacy_kinds": list(LEGACY_KIND_NAMES),
        "ha_ws_allowlist": sorted(HA_WS_ALLOWLIST),
        "denylist_notes": [
            "secrets.yaml",
            ".storage/**",
            "deps/**",
            "www/**",
            "alarm_control_panel.*",
            "homeassistant.stop",
            "open_shell",
            "ungated_backup_restore",
            "frontend_tunnel",
            "mijia_cloud",
        ],
    }


def all_dispatch_kinds() -> list[str]:
    """Kinds cloud may put in allowed_command_kinds."""
    return sorted(set(LEGACY_KIND_NAMES) | set(GENERIC_KINDS.keys()) | set(KIND_ALIASES.keys()))
