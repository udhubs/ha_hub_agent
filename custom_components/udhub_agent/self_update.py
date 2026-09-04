"""Cloud-triggered Agent self-update (overwrite custom_components/udhub_agent)."""

from __future__ import annotations

import asyncio
import hashlib
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


def _loaded_component_version(hass: HomeAssistant) -> str:
    """Return AGENT_VERSION of the currently cached component module."""
    try:
        from homeassistant.loader import DATA_COMPONENTS

        comp = (hass.data.get(DATA_COMPONENTS) or {}).get(DOMAIN)
    except Exception:
        return "?"
    const_mod = getattr(comp, "const", None)
    return str(getattr(const_mod, "AGENT_VERSION", "?"))


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
    comp = (hass.data.get(DATA_COMPONENTS) or {}).get(DOMAIN)
    const_mod = getattr(comp, "const", None)
    ver = str(getattr(const_mod, "AGENT_VERSION", "?"))
    _LOGGER.warning(
        "UDHUB agent_update fallback done: loaded_version=%s target=v%s",
        ver,
        target_version or "?",
    )


async def schedule_reload_after_update(
    hass: HomeAssistant, entry: ConfigEntry, *, target_version: str = ""
) -> None:
    """Reload the integration entry after package extract (no Core restart).

    Full ``homeassistant.restart`` is disruptive and must be operator-confirmed
    from the console when entry reload does not pick up the new version.
    """
    # WARNING level on purpose: supervisor /core/logs only surfaces >=WARNING,
    # and this task runs detached so its fate would otherwise be unobservable.
    _LOGGER.warning(
        "UDHUB agent_update reload task started entry=%s target=v%s",
        entry.entry_id,
        target_version or "?",
    )
    await asyncio.sleep(0.8)
    purge_agent_modules()
    purge_loader_caches(hass)

    _LOGGER.warning(
        "UDHUB agent_update reloading integration entry=%s target=v%s "
        "(in_components=%s state=%s)",
        entry.entry_id,
        target_version or "?",
        DOMAIN in hass.config.components,
        entry.state,
    )
    try:
        ok = await hass.config_entries.async_reload(entry.entry_id)
    except Exception:
        _LOGGER.warning(
            "UDHUB agent_update async_reload FAILED", exc_info=True
        )
        raise
    loaded_ver = _loaded_component_version(hass)
    _LOGGER.warning(
        "UDHUB agent_update async_reload returned ok=%s target=v%s "
        "state=%s loaded_version=%s",
        ok,
        target_version or "?",
        entry.state,
        loaded_ver,
    )
    if loaded_ver != (target_version or AGENT_VERSION):
        await _fallback_unload_setup(hass, entry, target_version)
