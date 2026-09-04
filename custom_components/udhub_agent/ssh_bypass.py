"""Probe / ensure SSH diagnostic bypass via Supervisor (HAOS / Supervised)."""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

# Official Terminal & SSH + common community Advanced SSH
SSH_ADDON_SLUGS = ("core_ssh", "a0d7b954_ssh")
SUPERVISOR_BASE = "http://supervisor"


def _supervisor_token() -> str | None:
    return os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")


async def _supervisor_get(
    hass: HomeAssistant, path: str
) -> dict[str, Any] | None:
    token = _supervisor_token()
    if not token:
        return None
    session = async_get_clientsession(hass)
    url = f"{SUPERVISOR_BASE}{path}"
    try:
        async with session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status >= 400:
                _LOGGER.debug(
                    "UDHUB supervisor GET %s → HTTP %s", path, resp.status
                )
                return None
            data = await resp.json(content_type=None)
            if isinstance(data, dict) and "data" in data:
                return data.get("data") if isinstance(data.get("data"), dict) else data
            return data if isinstance(data, dict) else None
    except Exception:
        _LOGGER.debug("UDHUB supervisor GET %s failed", path, exc_info=True)
        return None


async def _supervisor_get_raw(
    hass: HomeAssistant, path: str
) -> dict[str, Any] | list[Any] | str | None:
    """GET Supervisor path; preserve non-dict data (e.g. add-on log text)."""
    import json as _json

    token = _supervisor_token()
    if not token:
        return None
    session = async_get_clientsession(hass)
    url = f"{SUPERVISOR_BASE}{path}"
    try:
        async with session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status >= 400:
                _LOGGER.debug(
                    "UDHUB supervisor GET %s → HTTP %s", path, resp.status
                )
                return None
            text = await resp.text()
            stripped = text.lstrip()
            if stripped.startswith(("{", "[")):
                try:
                    data = _json.loads(text)
                except Exception:
                    return text
                if isinstance(data, dict) and "data" in data:
                    inner = data.get("data")
                    if isinstance(inner, (dict, list, str)):
                        return inner
                    return data
                if isinstance(data, (dict, list, str)):
                    return data
                return text
            return text
    except Exception:
        _LOGGER.debug("UDHUB supervisor GET %s failed", path, exc_info=True)
        return None


async def _supervisor_post(
    hass: HomeAssistant, path: str, body: dict[str, Any] | None = None
) -> tuple[bool, str | None]:
    token = _supervisor_token()
    if not token:
        return False, "no_supervisor_token"
    session = async_get_clientsession(hass)
    url = f"{SUPERVISOR_BASE}{path}"
    try:
        async with session.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json=body or {},
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                return False, f"http_{resp.status}:{text[:200]}"
            return True, None
    except Exception as exc:
        return False, str(exc)


async def probe_ssh_bypass(hass: HomeAssistant) -> dict[str, Any]:
    """Return ssh bypass capability snapshot for cloud heartbeat / UI.

    status:
      unsupported — no Supervisor (e.g. Container)
      missing — Supervisor ok, SSH add-on not installed
      stopped — installed but not started
      enabled — installed and started
      unknown — probe failed
    """
    install_type = "Unknown"
    try:
        from homeassistant.helpers import system_info as si

        info = await si.async_get_system_info(hass)
        install_type = str(info.get("installation_type") or "Unknown")
    except Exception:
        pass

    token = _supervisor_token()
    if not token:
        # Container / Core-only: cannot use Add-ons
        return {
            "status": "unsupported",
            "install_type": install_type,
            "addon_slug": None,
            "detail": "no_supervisor_token",
            "supported": False,
            "enabled": False,
        }

    addons_data = await _supervisor_get(hass, "/addons")
    if addons_data is None:
        return {
            "status": "unknown",
            "install_type": install_type,
            "addon_slug": None,
            "detail": "supervisor_addons_unreachable",
            "supported": True,
            "enabled": False,
        }

    addons = addons_data.get("addons") if isinstance(addons_data, dict) else None
    if not isinstance(addons, list):
        # Some HA versions return list at top level after unwrap
        addons = addons_data if isinstance(addons_data, list) else []

    found: dict[str, Any] | None = None
    for row in addons:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or "")
        if slug in SSH_ADDON_SLUGS:
            # Prefer started one
            state = str(row.get("state") or "").lower()
            if found is None or state == "started":
                found = row
            if state == "started":
                break

    if found is None:
        return {
            "status": "missing",
            "install_type": install_type,
            "addon_slug": None,
            "detail": "ssh_addon_not_installed",
            "supported": True,
            "enabled": False,
        }

    slug = str(found.get("slug") or "")
    state = str(found.get("state") or "").lower()
    if state == "started":
        return {
            "status": "enabled",
            "install_type": install_type,
            "addon_slug": slug,
            "detail": f"{slug}:started",
            "supported": True,
            "enabled": True,
        }
    return {
        "status": "stopped",
        "install_type": install_type,
        "addon_slug": slug,
        "detail": f"{slug}:{state or 'stopped'}",
        "supported": True,
        "enabled": False,
    }


async def ensure_ssh_bypass(hass: HomeAssistant) -> dict[str, Any]:
    """Install (if needed) and start official core_ssh add-on.

    Requires Supervisor. Returns probe snapshot plus ensure result fields.
    """
    probe = await probe_ssh_bypass(hass)
    if not probe.get("supported"):
        return {**probe, "ensure": "skipped_unsupported"}

    if probe.get("status") == "enabled":
        return {**probe, "ensure": "already_enabled"}

    slug = probe.get("addon_slug") or "core_ssh"

    if probe.get("status") == "missing":
        ok, err = await _supervisor_post(
            hass, f"/store/addons/{slug}/install", {}
        )
        if not ok:
            # Fallback deprecated path
            ok2, err2 = await _supervisor_post(
                hass, f"/addons/{slug}/install", {}
            )
            if not ok2:
                return {
                    **probe,
                    "ensure": "install_failed",
                    "error": err or err2,
                }
        probe = await probe_ssh_bypass(hass)
        slug = probe.get("addon_slug") or slug

    if probe.get("status") == "enabled":
        return {**probe, "ensure": "installed_and_running"}

    ok, err = await _supervisor_post(hass, f"/addons/{slug}/start", {})
    if not ok:
        return {**probe, "ensure": "start_failed", "error": err}

    probe = await probe_ssh_bypass(hass)
    return {
        **probe,
        "ensure": "started" if probe.get("enabled") else "start_uncertain",
    }
