"""Cloud-triggered Agent self-update (overwrite custom_components/udhub_agent)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import AGENT_VERSION, DOMAIN

_LOGGER = logging.getLogger(__name__)


def purge_agent_modules() -> None:
    """Drop cached agent modules so a reload imports fresh files from disk.

    HA imports custom integrations as ``custom_components.<domain>`` (plus
    submodules); the bare ``<domain>`` prefix is covered as well since it
    appears in a few internal caches. Without this, ``async_reload`` hits
    stale ``sys.modules`` entries and keeps running the old version.
    """
    import sys

    prefixes = (DOMAIN, f"custom_components.{DOMAIN}")
    for key in list(sys.modules):
        for prefix in prefixes:
            if key == prefix or key.startswith(f"{prefix}."):
                sys.modules.pop(key, None)
                break


def purge_loader_caches(hass: HomeAssistant) -> None:
    """Purge HA loader-level component caches (the real culprit).

    ``homeassistant.loader.Integration`` keeps imported component modules in
    ``hass.data[DATA_COMPONENTS]`` and platform modules next to it. Those
    entries hold strong references to the *old* module objects, so
    ``async_reload`` → setup → ``async_get_component`` returns the stale
    module without touching disk, even after ``sys.modules`` was purged.
    Dropping the entries forces a fresh import in the subsequent setup.
    """
    try:
        from homeassistant.loader import (
            DATA_COMPONENTS,
            DATA_MISSING_PLATFORMS,
        )
    except Exception:  # pragma: no cover - older HA cores
        return

    for data_key in (DATA_COMPONENTS, DATA_MISSING_PLATFORMS):
        cache = hass.data.get(data_key)
        if not isinstance(cache, dict):
            continue
        for cache_key in list(cache):
            if cache_key == DOMAIN or str(cache_key).startswith(f"{DOMAIN}."):
                cache.pop(cache_key, None)


async def apply_agent_update(
    hass: HomeAssistant,
    entry: ConfigEntry,
    payload: dict[str, Any],
    *,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """Download release tarball, verify sha256, backup, extract, reload integration."""
    package_url = str(payload.get("package_url") or payload.get("url") or "").strip()
    expected_sha = str(payload.get("sha256") or "").strip().lower()
    target_version = str(payload.get("version") or "").strip()
    if not package_url or not expected_sha:
        return {"status": "failed", "error": "missing_package_url_or_sha256"}

    config_dir = getattr(hass.config, "config_dir", None)
    if not config_dir:
        path_fn = getattr(hass.config, "path", None)
        config_dir = path_fn() if callable(path_fn) else path_fn
    if not config_dir:
        return {"status": "failed", "error": "config_dir_unavailable"}
    config_path = Path(str(config_dir))
    target_dir = config_path / "custom_components" / DOMAIN
    target_dir.parent.mkdir(parents=True, exist_ok=True)

    owns_session = session is None
    if owns_session:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(hass)

    tmp_dir = Path(tempfile.mkdtemp(prefix="udhub_agent_update_"))
    archive_path = tmp_dir / "package.tar.gz"
    try:
        _LOGGER.info(
            "UDHUB agent_update downloading %s → %s",
            package_url,
            target_dir,
        )
        async with session.get(package_url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status >= 400:
                return {
                    "status": "failed",
                    "error": f"download_http_{resp.status}",
                }
            data = await resp.read()

        digest = hashlib.sha256(data).hexdigest()
        if digest.lower() != expected_sha.lower():
            return {
                "status": "failed",
                "error": "sha256_mismatch",
                "expected": expected_sha,
                "actual": digest,
            }

        archive_path.write_bytes(data)

        if target_dir.is_dir():
            backup = target_dir.with_name(f"{DOMAIN}.bak")
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            shutil.copytree(target_dir, backup)
            shutil.rmtree(target_dir)

        with tarfile.open(archive_path, "r:gz") as tar:
            members = [m for m in tar.getmembers() if m.name.startswith(f"{DOMAIN}/")]
            if not members:
                return {"status": "failed", "error": "invalid_archive_layout"}
            tar.extractall(path=target_dir.parent, members=members)

        if not target_dir.is_dir():
            return {"status": "failed", "error": "extract_failed"}

        # Drop cached modules so the subsequent reload imports files from disk.
        purge_agent_modules()
        purge_loader_caches(hass)

        _LOGGER.warning(
            "UDHUB agent_update package applied v%s (modules purged)",
            target_version or "?",
        )

        previous = str(payload.get("previous_version") or AGENT_VERSION)
        result = {
            "status": "ok",
            "previous_version": previous,
            "version": target_version or "unknown",
            "sha256": digest,
            "reloading": True,
        }
        return result
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _disk_manifest_version(hass: HomeAssistant) -> str:
    """Version written on disk (source of truth after package extract)."""
    try:
        config_dir = getattr(hass.config, "config_dir", None)
        if not config_dir:
            path_fn = getattr(hass.config, "path", None)
            config_dir = path_fn() if callable(path_fn) else path_fn
        if not config_dir:
            return ""
        man = Path(str(config_dir)) / "custom_components" / DOMAIN / "manifest.json"
        data = json.loads(man.read_text(encoding="utf-8"))
        return str(data.get("version") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _loaded_component_version(hass: HomeAssistant) -> str:
    """Return AGENT_VERSION of the currently cached component module."""
    import sys

    # Prefer live const module (survives some loader-cache shapes).
    for key in (
        f"custom_components.{DOMAIN}.const",
        f"{DOMAIN}.const",
    ):
        mod = sys.modules.get(key)
        ver = getattr(mod, "AGENT_VERSION", None) if mod is not None else None
        if ver:
            return str(ver)
    try:
        from homeassistant.loader import DATA_COMPONENTS

        comp = (hass.data.get(DATA_COMPONENTS) or {}).get(DOMAIN)
    except Exception:
        return "?"
    const_mod = getattr(comp, "const", None)
    return str(getattr(const_mod, "AGENT_VERSION", "?") or "?")


async def _fallback_unload_setup(
    hass: HomeAssistant, entry: ConfigEntry, target_version: str
) -> None:
    """Last-resort reload: explicit unload → purge → setup."""
    from homeassistant.loader import DATA_COMPONENTS

    _LOGGER.warning(
        "UDHUB agent_update fallback: explicit unload+setup entry=%s",
        entry.entry_id,
    )
    await hass.config_entries.async_unload(entry.entry_id)
    purge_agent_modules()
    purge_loader_caches(hass)
    await asyncio.sleep(0.5)
    await hass.config_entries.async_setup(entry.entry_id)
    ver = _loaded_component_version(hass)
    _LOGGER.warning(
        "UDHUB agent_update fallback done: loaded_version=%s target=v%s disk=%s",
        ver,
        target_version or "?",
        _disk_manifest_version(hass) or "?",
    )


async def _restart_ha_core(hass: HomeAssistant, *, reason: str) -> None:
    """Restart HA Core so extracted Agent files are actually imported."""
    _LOGGER.warning("UDHUB agent_update requesting HA restart (%s)", reason)
    try:
        await hass.services.async_call(
            "homeassistant", "restart", blocking=False
        )
    except Exception:  # noqa: BLE001
        _LOGGER.warning(
            "UDHUB agent_update homeassistant.restart failed", exc_info=True
        )


async def schedule_reload_after_update(
    hass: HomeAssistant, entry: ConfigEntry, *, target_version: str = ""
) -> None:
    """Reload the integration entry after package extract; restart if still stale.

    On HA 2026.9, entry reload / unload+setup often keeps stale modules in
    memory. If the running AGENT_VERSION still mismatches the on-disk
    manifest after both attempts, restart Core so the new Agent loads.
    """
    target = (target_version or "").strip() or _disk_manifest_version(hass) or AGENT_VERSION
    _LOGGER.warning(
        "UDHUB agent_update reload task started entry=%s target=v%s",
        entry.entry_id,
        target or "?",
    )
    await asyncio.sleep(0.8)
    purge_agent_modules()
    purge_loader_caches(hass)

    _LOGGER.warning(
        "UDHUB agent_update reloading integration entry=%s target=v%s "
        "(in_components=%s state=%s)",
        entry.entry_id,
        target or "?",
        DOMAIN in hass.config.components,
        entry.state,
    )
    try:
        ok = await hass.config_entries.async_reload(entry.entry_id)
    except Exception:
        _LOGGER.warning(
            "UDHUB agent_update async_reload FAILED", exc_info=True
        )
        # Still try fallback / restart rather than aborting silently.
        ok = False
        _LOGGER.warning("UDHUB agent_update continuing with fallback after reload error")
    loaded_ver = _loaded_component_version(hass)
    disk_ver = _disk_manifest_version(hass) or target
    _LOGGER.warning(
        "UDHUB agent_update async_reload returned ok=%s target=v%s "
        "state=%s loaded_version=%s disk=%s",
        ok,
        target or "?",
        entry.state,
        loaded_ver,
        disk_ver,
    )
    if loaded_ver != disk_ver and loaded_ver != target:
        await _fallback_unload_setup(hass, entry, target)
        loaded_ver = _loaded_component_version(hass)
        disk_ver = _disk_manifest_version(hass) or target

    if loaded_ver != disk_ver and loaded_ver != target:
        # Give WS a moment to flush command.result before Core goes down.
        await asyncio.sleep(1.5)
        await _restart_ha_core(
            hass,
            reason=f"loaded={loaded_ver} disk={disk_ver} target={target}",
        )
    else:
        _LOGGER.warning(
            "UDHUB agent_update reload OK loaded=v%s disk=v%s",
            loaded_ver,
            disk_ver,
        )
