"""Supervisor API helpers for HAOS / Supervised (PRD §5.4G)."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .ssh_bypass import _supervisor_get, _supervisor_post, _supervisor_token

_LOGGER = logging.getLogger(__name__)


async def probe_supervisor(hass: HomeAssistant) -> dict[str, Any]:
    token = _supervisor_token()
    if not token:
        return {
            "supported": False,
            "detail": "no_supervisor_token",
        }
    info = await _supervisor_get(hass, "/info")
    core = await _supervisor_get(hass, "/core/info")
    host = await _supervisor_get(hass, "/host/info")
    return {
        "supported": True,
        "supervisor": info,
        "core": core,
        "host": host,
    }


async def list_backups(hass: HomeAssistant) -> dict[str, Any]:
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    data = await _supervisor_get(hass, "/backups")
    if data is None:
        return {"supported": True, "backups": [], "detail": "unreachable"}
    backups = data.get("backups") if isinstance(data.get("backups"), list) else []
    return {"supported": True, "backups": backups}


async def create_backup(hass: HomeAssistant, name: str | None = None) -> dict[str, Any]:
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    body: dict[str, Any] = {"compressed": True}
    if name:
        body["name"] = name
    ok, err = await _supervisor_post(hass, "/backups/new/full", body)
    if not ok:
        return {"supported": True, "status": "failed", "error": err}
    return {"supported": True, "status": "ok"}


async def restart_core(hass: HomeAssistant) -> dict[str, Any]:
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    ok, err = await _supervisor_post(hass, "/core/restart", {})
    if not ok:
        return {"supported": True, "status": "failed", "error": err}
    return {"supported": True, "status": "ok"}


# ── Add-on management ───────────────────────────────────────────────


async def list_addons(hass: HomeAssistant) -> dict[str, Any]:
    """List all installed and available add-ons."""
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    data = await _supervisor_get(hass, "/addons")
    if data is None:
        return {"supported": True, "addons": [], "detail": "unreachable"}
    addons = data.get("addons") if isinstance(data, dict) and "addons" in data else data
    if not isinstance(addons, list):
        addons = []
    return {"supported": True, "addons": addons}


async def install_addon(
    hass: HomeAssistant, slug: str
) -> dict[str, Any]:
    """Install an add-on by slug."""
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    ok, err = await _supervisor_post(hass, f"/store/addons/{slug}/install", {})
    if not ok:
        # Fallback deprecated path
        ok2, err2 = await _supervisor_post(hass, f"/addons/{slug}/install", {})
        if not ok2:
            return {"supported": True, "status": "failed", "error": err or err2}
    return {"supported": True, "status": "ok", "slug": slug}


async def uninstall_addon(
    hass: HomeAssistant, slug: str
) -> dict[str, Any]:
    """Uninstall an add-on by slug."""
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    ok, err = await _supervisor_post(hass, f"/addons/{slug}/uninstall", {})
    if not ok:
        return {"supported": True, "status": "failed", "error": err}
    return {"supported": True, "status": "ok", "slug": slug}


async def start_addon(hass: HomeAssistant, slug: str) -> dict[str, Any]:
    """Start an installed add-on."""
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    ok, err = await _supervisor_post(hass, f"/addons/{slug}/start", {})
    if not ok:
        return {"supported": True, "status": "failed", "error": err}
    return {"supported": True, "status": "ok", "slug": slug}


async def stop_addon(hass: HomeAssistant, slug: str) -> dict[str, Any]:
    """Stop a running add-on."""
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    ok, err = await _supervisor_post(hass, f"/addons/{slug}/stop", {})
    if not ok:
        return {"supported": True, "status": "failed", "error": err}
    return {"supported": True, "status": "ok", "slug": slug}


async def restart_addon(hass: HomeAssistant, slug: str) -> dict[str, Any]:
    """Restart a running add-on."""
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    ok, err = await _supervisor_post(hass, f"/addons/{slug}/restart", {})
    if not ok:
        return {"supported": True, "status": "failed", "error": err}
    return {"supported": True, "status": "ok", "slug": slug}


# ── Host & OS ────────────────────────────────────────────────────────


async def host_info(hass: HomeAssistant) -> dict[str, Any]:
    """Get host system information."""
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    data = await _supervisor_get(hass, "/host/info")
    if data is None:
        return {"supported": True, "status": "failed", "error": "unreachable"}
    return {"supported": True, "host": data}


async def host_reboot(hass: HomeAssistant) -> dict[str, Any]:
    """Reboot the host machine (HAOS / Supervised only)."""
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    ok, err = await _supervisor_post(hass, "/host/reboot", {})
    if not ok:
        return {"supported": True, "status": "failed", "error": err}
    return {"supported": True, "status": "ok"}


async def os_info(hass: HomeAssistant) -> dict[str, Any]:
    """Get OS information (HAOS only)."""
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    data = await _supervisor_get(hass, "/os/info")
    if data is None:
        return {"supported": True, "status": "failed", "error": "unreachable"}
    return {"supported": True, "os": data}


async def os_update(hass: HomeAssistant, version: str | None = None) -> dict[str, Any]:
    """Update HA OS to latest or specific version."""
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    body: dict[str, Any] = {}
    if version:
        body["version"] = version
    ok, err = await _supervisor_post(hass, "/os/update", body)
    if not ok:
        return {"supported": True, "status": "failed", "error": err}
    return {"supported": True, "status": "ok"}


async def core_update(hass: HomeAssistant, version: str | None = None) -> dict[str, Any]:
    """Update Home Assistant Core via Supervisor."""
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    body: dict[str, Any] = {}
    if version:
        body["version"] = version
    ok, err = await _supervisor_post(hass, "/core/update", body)
    if not ok:
        return {"supported": True, "status": "failed", "error": err}
    return {"supported": True, "status": "ok"}


async def addon_update(hass: HomeAssistant, slug: str) -> dict[str, Any]:
    """Update a single Add-on via Supervisor."""
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    if not slug:
        return {"supported": True, "status": "failed", "error": "slug_required"}
    ok, err = await _supervisor_post(hass, f"/addons/{slug}/update", {})
    if not ok:
        return {"supported": True, "status": "failed", "error": err, "slug": slug}
    return {"supported": True, "status": "ok", "slug": slug}


async def network_info(hass: HomeAssistant) -> dict[str, Any]:
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    data = await _supervisor_get(hass, "/network/info")
    if data is None:
        return {"supported": True, "status": "failed", "error": "unreachable"}
    return {
        "supported": True,
        "status": "ok",
        "network": data,
        # Supervisor writable subset (POST /network/interface/{iface}/update)
        "writable": {
            "ipv4": ["method", "address", "gateway", "nameservers"],
            "ipv6": ["method", "address", "gateway", "nameservers"],
            "wifi": ["mode", "ssid", "auth", "psk"],
            "enabled": True,
            "notes": [
                "仅 HAOS/Supervised；Container 无 Supervisor 网络 API",
                "改 IP/网关可能导致远程断连，需现场或带外恢复",
                "POST 会替换对应块（缺省字段会被清空），请一次写全",
            ],
        },
    }


async def network_update(
    hass: HomeAssistant,
    interface: str,
    *,
    ipv4: dict[str, Any] | None = None,
    ipv6: dict[str, Any] | None = None,
    wifi: dict[str, Any] | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Update one host interface via Supervisor Network API (max writable subset)."""
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    iface = str(interface or "").strip()
    if not iface:
        return {"supported": True, "status": "failed", "error": "interface_required"}

    body: dict[str, Any] = {}
    if ipv4 is not None:
        if not isinstance(ipv4, dict):
            return {"supported": True, "status": "failed", "error": "ipv4_must_be_object"}
        normalized = _normalize_ip_setting(ipv4)
        if normalized:
            body["ipv4"] = normalized
    if ipv6 is not None:
        if not isinstance(ipv6, dict):
            return {"supported": True, "status": "failed", "error": "ipv6_must_be_object"}
        normalized = _normalize_ip_setting(ipv6)
        if normalized:
            body["ipv6"] = normalized
    if wifi is not None:
        if not isinstance(wifi, dict):
            return {"supported": True, "status": "failed", "error": "wifi_must_be_object"}
        wifi_body: dict[str, Any] = {}
        for key in ("mode", "ssid", "auth", "psk"):
            if key in wifi and wifi[key] is not None:
                wifi_body[key] = wifi[key]
        if wifi_body:
            body["wifi"] = wifi_body
    if enabled is not None:
        body["enabled"] = bool(enabled)

    if not body:
        return {
            "supported": True,
            "status": "failed",
            "error": "empty_update",
            "hint": "至少提供 ipv4 / ipv6 / wifi / enabled 之一",
        }

    ok, err = await _supervisor_post(
        hass, f"/network/interface/{iface}/update", body
    )
    if not ok:
        return {"supported": True, "status": "failed", "error": err, "interface": iface}
    return {"supported": True, "status": "ok", "interface": iface, "applied": body}


def _normalize_ip_setting(raw: dict[str, Any]) -> dict[str, Any]:
    """Map UI/cloud fields to Supervisor SCHEMA_UPDATE ipv4/ipv6 shape."""
    out: dict[str, Any] = {}
    method = raw.get("method")
    if method is not None:
        # Supervisor accepts auto/static/disabled (legacy dhcp → auto)
        m = str(method).strip().lower()
        if m == "dhcp":
            m = "auto"
        out["method"] = m
    if "address" in raw and raw["address"] is not None:
        addr = raw["address"]
        if isinstance(addr, str):
            out["address"] = [a.strip() for a in addr.split(",") if a.strip()]
        elif isinstance(addr, list):
            out["address"] = [str(a).strip() for a in addr if str(a).strip()]
        else:
            out["address"] = []
    if "gateway" in raw and raw["gateway"] is not None:
        gw = str(raw["gateway"]).strip()
        if gw:
            out["gateway"] = gw
    # Accept nameservers or legacy dns
    ns = raw.get("nameservers", raw.get("dns"))
    if ns is not None:
        if isinstance(ns, str):
            out["nameservers"] = [a.strip() for a in ns.replace(";", ",").split(",") if a.strip()]
        elif isinstance(ns, list):
            out["nameservers"] = [str(a).strip() for a in ns if str(a).strip()]
        else:
            out["nameservers"] = []
    return out


async def hardware_info(hass: HomeAssistant) -> dict[str, Any]:
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    data = await _supervisor_get(hass, "/hardware/info")
    if data is None:
        data = await _supervisor_get(hass, "/host/info")
    if data is None:
        return {"supported": True, "status": "failed", "error": "unreachable"}
    return {"supported": True, "status": "ok", "hardware": data}


async def store_addons(hass: HomeAssistant) -> dict[str, Any]:
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    data = await _supervisor_get(hass, "/store")
    if data is None:
        data = await _supervisor_get(hass, "/addons")
    if data is None:
        return {"supported": True, "status": "failed", "error": "unreachable"}
    addons: list[Any] = []
    if isinstance(data, dict):
        raw = data.get("addons") or data.get("repositories") or []
        if isinstance(raw, list):
            addons = raw
    return {"supported": True, "status": "ok", "store": addons[:200]}


async def addon_info(hass: HomeAssistant, slug: str) -> dict[str, Any]:
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    data = await _supervisor_get(hass, f"/addons/{slug}/info")
    if data is None:
        return {"supported": True, "status": "failed", "error": "unreachable"}
    return {"supported": True, "status": "ok", "addon": data}


async def addon_logs(hass: HomeAssistant, slug: str) -> dict[str, Any]:
    """Fetch add-on logs (Supervisor may return JSON data string or plain text)."""
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    from .ssh_bypass import _supervisor_get_raw

    raw = await _supervisor_get_raw(hass, f"/addons/{slug}/logs")
    if raw is None:
        return {"supported": True, "status": "failed", "error": "unreachable"}
    logs: Any = raw
    if isinstance(raw, dict):
        if "data" in raw and not isinstance(raw.get("data"), dict):
            logs = raw.get("data")
        elif "logs" in raw:
            logs = raw.get("logs")
    return {"supported": True, "status": "ok", "logs": logs}


async def addon_options_get(hass: HomeAssistant, slug: str) -> dict[str, Any]:
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    data = await _supervisor_get(hass, f"/addons/{slug}/options")
    if data is None:
        info = await _supervisor_get(hass, f"/addons/{slug}/info")
        data = (info or {}).get("options") if isinstance(info, dict) else None
    return {"supported": True, "status": "ok", "options": data}


async def addon_options_set(
    hass: HomeAssistant, slug: str, options: dict[str, Any]
) -> dict[str, Any]:
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    ok, err = await _supervisor_post(
        hass, f"/addons/{slug}/options", {"options": options}
    )
    if not ok:
        return {"supported": True, "status": "failed", "error": err}
    return {"supported": True, "status": "ok", "slug": slug}


async def backup_info(hass: HomeAssistant, slug: str) -> dict[str, Any]:
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    data = await _supervisor_get(hass, f"/backups/{slug}/info")
    if data is None:
        return {"supported": True, "status": "failed", "error": "unreachable"}
    return {"supported": True, "status": "ok", "backup": data}


async def backup_restore(
    hass: HomeAssistant,
    slug: str,
    *,
    password: str | None = None,
) -> dict[str, Any]:
    """Restore backup — caller must enforce elevated/debugging gates."""
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    body: dict[str, Any] = {}
    if password:
        body["password"] = password
    ok, err = await _supervisor_post(hass, f"/backups/{slug}/restore/full", body)
    if not ok:
        ok2, err2 = await _supervisor_post(hass, f"/backups/{slug}/restore", body)
        if not ok2:
            return {"supported": True, "status": "failed", "error": err or err2}
    return {"supported": True, "status": "ok", "slug": slug}


async def create_partial_backup(
    hass: HomeAssistant,
    name: str | None = None,
    folders: list[str] | None = None,
) -> dict[str, Any]:
    if not _supervisor_token():
        return {"supported": False, "detail": "no_supervisor_token"}
    body: dict[str, Any] = {"compressed": True}
    if name:
        body["name"] = name
    if folders:
        body["folders"] = folders
    ok, err = await _supervisor_post(hass, "/backups/new/partial", body)
    if not ok:
        return {"supported": True, "status": "failed", "error": err}
    return {"supported": True, "status": "ok"}
