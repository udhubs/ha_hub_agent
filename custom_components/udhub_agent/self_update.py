"""Cloud-triggered Agent self-update (overwrite custom_components/udhub_agent).

OTA safety:
  1. sha256 verify before replace
  2. copy current tree to ``udhub_agent.bak``
  3. write ``udhub_agent.ota_pending.json`` marker
  4. extract + reload
  5. wait for post-reload cloud health (WS hello.ok)
  6. on timeout / setup failure → restore ``.bak`` and reload again

The reload+health task is started with an empty ``contextvars.Context`` so it
survives config-entry unload cancellation and keeps running the *pre-reload*
function objects even after modules on disk are replaced.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import AGENT_VERSION, DOMAIN

_LOGGER = logging.getLogger(__name__)

# After reload, require a fresh hello.ok within this window or roll back.
OTA_HEALTH_TIMEOUT_SEC = 90.0
OTA_HEALTH_POLL_SEC = 1.0
OTA_MARKER_NAME = f"{DOMAIN}.ota_pending.json"
OTA_BACKUP_NAME = f"{DOMAIN}.bak"


def purge_agent_modules() -> None:
    """Drop cached agent modules so a reload imports fresh files from disk."""
    import sys

    prefixes = (DOMAIN, f"custom_components.{DOMAIN}")
    for key in list(sys.modules):
        for prefix in prefixes:
            if key == prefix or key.startswith(f"{prefix}."):
                sys.modules.pop(key, None)
                break


def purge_loader_caches(hass: HomeAssistant) -> None:
    """Purge HA loader-level component caches."""
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


def _config_path(hass: HomeAssistant) -> Path | None:
    config_dir = getattr(hass.config, "config_dir", None)
    if not config_dir:
        path_fn = getattr(hass.config, "path", None)
        config_dir = path_fn() if callable(path_fn) else path_fn
    if not config_dir:
        return None
    return Path(str(config_dir))


def agent_dir(config_path: Path) -> Path:
    return config_path / "custom_components" / DOMAIN


def backup_dir(config_path: Path) -> Path:
    return config_path / "custom_components" / OTA_BACKUP_NAME


def marker_path(config_path: Path) -> Path:
    return config_path / "custom_components" / OTA_MARKER_NAME


def write_ota_marker(
    config_path: Path,
    *,
    target_version: str,
    previous_version: str,
    health_timeout_sec: float = OTA_HEALTH_TIMEOUT_SEC,
) -> None:
    payload = {
        "target_version": target_version,
        "previous_version": previous_version,
        "started_at": time.time(),
        "health_timeout_sec": health_timeout_sec,
    }
    path = marker_path(config_path)
    path.write_text(json.dumps(payload), encoding="utf-8")


def read_ota_marker(config_path: Path) -> dict[str, Any] | None:
    path = marker_path(config_path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _LOGGER.warning("UDHUB OTA marker unreadable; ignoring", exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def clear_ota_marker(config_path: Path) -> None:
    path = marker_path(config_path)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def restore_agent_backup(config_path: Path) -> bool:
    """Replace ``udhub_agent`` with ``udhub_agent.bak``. Returns True if restored."""
    target = agent_dir(config_path)
    backup = backup_dir(config_path)
    if not backup.is_dir():
        _LOGGER.error("UDHUB OTA rollback: backup missing at %s", backup)
        return False
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(backup, target)
    _LOGGER.warning("UDHUB OTA rollback: restored %s from %s", target, backup)
    return True


def _client_cloud_healthy(
    hass: HomeAssistant, entry: ConfigEntry, *, min_auth_ts: float
) -> bool:
    """True when entry client is connected and hello.ok happened after min_auth_ts."""
    bucket = (hass.data.get(DOMAIN) or {}).get(entry.entry_id) or {}
    client = bucket.get("client")
    if client is None:
        return False
    connected = bool(getattr(client, "_connected", False))
    auth_ts = float(getattr(client, "_last_auth_ok_ts", 0.0) or 0.0)
    return connected and auth_ts >= min_auth_ts


async def wait_for_cloud_health(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    timeout_sec: float,
    min_auth_ts: float,
) -> bool:
    deadline = time.monotonic() + max(5.0, timeout_sec)
    while time.monotonic() < deadline:
        if _client_cloud_healthy(hass, entry, min_auth_ts=min_auth_ts):
            return True
        await asyncio.sleep(OTA_HEALTH_POLL_SEC)
    return False


async def apply_agent_update(
    hass: HomeAssistant,
    entry: ConfigEntry,
    payload: dict[str, Any],
    *,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, Any]:
    """Download release tarball, verify sha256, backup, extract."""
    package_url = str(payload.get("package_url") or payload.get("url") or "").strip()
    expected_sha = str(payload.get("sha256") or "").strip().lower()
    target_version = str(payload.get("version") or "").strip()
    if not package_url or not expected_sha:
        return {"status": "failed", "error": "missing_package_url_or_sha256"}

    config_path = _config_path(hass)
    if not config_path:
        return {"status": "failed", "error": "config_dir_unavailable"}
    target_dir = agent_dir(config_path)
    target_dir.parent.mkdir(parents=True, exist_ok=True)

    if session is None:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(hass)

    health_timeout = float(
        payload.get("health_timeout_sec") or OTA_HEALTH_TIMEOUT_SEC
    )
    tmp_dir = Path(tempfile.mkdtemp(prefix="udhub_agent_update_"))
    archive_path = tmp_dir / "package.tar.gz"
    try:
        _LOGGER.info(
            "UDHUB agent_update downloading %s → %s",
            package_url,
            target_dir,
        )
        async with session.get(
            package_url, timeout=aiohttp.ClientTimeout(total=120)
        ) as resp:
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

        previous = str(payload.get("previous_version") or AGENT_VERSION)
        if target_dir.is_dir():
            backup = backup_dir(config_path)
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            shutil.copytree(target_dir, backup)
            shutil.rmtree(target_dir)

        with tarfile.open(archive_path, "r:gz") as tar:
            members = [
                m for m in tar.getmembers() if m.name.startswith(f"{DOMAIN}/")
            ]
            if not members:
                # Restore immediately if we already removed the live tree.
                if backup_dir(config_path).is_dir() and not target_dir.exists():
                    restore_agent_backup(config_path)
                return {"status": "failed", "error": "invalid_archive_layout"}
            tar.extractall(path=target_dir.parent, members=members)

        if not target_dir.is_dir():
            if backup_dir(config_path).is_dir():
                restore_agent_backup(config_path)
            return {"status": "failed", "error": "extract_failed"}

        write_ota_marker(
            config_path,
            target_version=target_version or "unknown",
            previous_version=previous,
            health_timeout_sec=health_timeout,
        )

        purge_agent_modules()
        purge_loader_caches(hass)

        _LOGGER.warning(
            "UDHUB agent_update package applied v%s (modules purged; health window %.0fs)",
            target_version or "?",
            health_timeout,
        )

        return {
            "status": "ok",
            "previous_version": previous,
            "version": target_version or "unknown",
            "sha256": digest,
            "reloading": True,
            "rollback_armed": True,
            "health_timeout_sec": health_timeout,
        }
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


async def _notify_rollback(
    hass: HomeAssistant, *, target_version: str, previous_version: str, reason: str
) -> None:
    try:
        from .notify import create_notification

        await create_notification(
            hass,
            title="UDHUB Agent 升级已自动回滚",
            message=(
                f"升级到 **v{target_version or '?'}** 后未能在时限内恢复云端连接"
                f"（{reason}）。\n\n"
                f"已从备份恢复 **v{previous_version or '?'}**。"
                "请检查新版本或联系云枢支持后再试。"
            ),
            notification_id=f"{DOMAIN}_ota_rollback",
        )
    except Exception:
        _LOGGER.debug("UDHUB OTA rollback notification failed", exc_info=True)


async def rollback_and_reload(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    reason: str,
    target_version: str = "",
    previous_version: str = "",
) -> bool:
    """Restore ``.bak`` and reload the integration entry."""
    config_path = _config_path(hass)
    if not config_path:
        return False
    marker = read_ota_marker(config_path) or {}
    prev = previous_version or str(marker.get("previous_version") or "")
    tgt = target_version or str(marker.get("target_version") or "")

    if not restore_agent_backup(config_path):
        return False

    purge_agent_modules()
    purge_loader_caches(hass)
    clear_ota_marker(config_path)

    try:
        await hass.config_entries.async_reload(entry.entry_id)
    except Exception:
        _LOGGER.warning(
            "UDHUB OTA rollback reload failed; trying unload+setup",
            exc_info=True,
        )
        try:
            await _fallback_unload_setup(hass, entry, prev or "rollback")
        except Exception:
            _LOGGER.exception("UDHUB OTA rollback unload+setup failed")
            return False

    await _notify_rollback(
        hass, target_version=tgt, previous_version=prev, reason=reason
    )
    _LOGGER.warning(
        "UDHUB OTA rollback complete reason=%s target=v%s previous=v%s",
        reason,
        tgt or "?",
        prev or "?",
    )
    return True


async def schedule_reload_after_update(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    target_version: str = "",
    rollback_on_failure: bool = False,
    health_timeout_sec: float = OTA_HEALTH_TIMEOUT_SEC,
) -> None:
    """Reload after package extract; optionally roll back if cloud health fails.

    Full ``homeassistant.restart`` is disruptive and must be operator-confirmed
    from the console when entry reload does not pick up the new version.
    """
    _LOGGER.warning(
        "UDHUB agent_update reload task started entry=%s target=v%s "
        "rollback=%s health_timeout=%.0fs",
        entry.entry_id,
        target_version or "?",
        rollback_on_failure,
        health_timeout_sec,
    )
    await asyncio.sleep(0.8)
    purge_agent_modules()
    purge_loader_caches(hass)

    min_auth_ts = time.monotonic()
    reload_error: str | None = None

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
    except Exception as exc:
        _LOGGER.warning(
            "UDHUB agent_update async_reload FAILED", exc_info=True
        )
        reload_error = f"reload_exception:{type(exc).__name__}"
        ok = False
        if not rollback_on_failure:
            raise

    if reload_error is None:
        loaded_ver = _loaded_component_version(hass)
        _LOGGER.warning(
            "UDHUB agent_update async_reload returned ok=%s target=v%s "
            "state=%s loaded_version=%s",
            ok,
            target_version or "?",
            entry.state,
            loaded_ver,
        )
        if loaded_ver != (target_version or AGENT_VERSION) and target_version not in (
            "",
            "supervisor-heal",
        ):
            try:
                await _fallback_unload_setup(hass, entry, target_version)
                min_auth_ts = time.monotonic()
            except Exception as exc:
                _LOGGER.warning(
                    "UDHUB agent_update fallback FAILED", exc_info=True
                )
                reload_error = f"fallback_exception:{type(exc).__name__}"

    if not rollback_on_failure:
        config_path = _config_path(hass)
        if config_path:
            clear_ota_marker(config_path)
        return

    config_path = _config_path(hass)
    if reload_error:
        await rollback_and_reload(
            hass,
            entry,
            reason=reload_error,
            target_version=target_version,
        )
        return

    healthy = await wait_for_cloud_health(
        hass,
        entry,
        timeout_sec=health_timeout_sec,
        min_auth_ts=min_auth_ts,
    )
    if healthy:
        if config_path:
            clear_ota_marker(config_path)
        _LOGGER.warning(
            "UDHUB agent_update health OK target=v%s (cloud hello.ok)",
            target_version or "?",
        )
        return

    await rollback_and_reload(
        hass,
        entry,
        reason=f"health_timeout_{int(health_timeout_sec)}s",
        target_version=target_version,
    )


def spawn_ota_health_watch_if_pending(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """On setup: if an OTA marker remains, watch health or roll back (boot recovery)."""
    import contextvars

    config_path = _config_path(hass)
    if not config_path:
        return
    marker = read_ota_marker(config_path)
    if not marker:
        return

    timeout = float(marker.get("health_timeout_sec") or OTA_HEALTH_TIMEOUT_SEC)
    started = float(marker.get("started_at") or 0.0)
    # If marker is already older than 2× window (crash mid-OTA), roll back ASAP.
    age = time.time() - started if started else 0.0
    target = str(marker.get("target_version") or "")
    previous = str(marker.get("previous_version") or "")

    async def _watch() -> None:
        if age > timeout * 2:
            _LOGGER.warning(
                "UDHUB OTA marker stale age=%.0fs — rolling back to v%s",
                age,
                previous or "?",
            )
            await rollback_and_reload(
                hass,
                entry,
                reason=f"stale_marker_{int(age)}s",
                target_version=target,
                previous_version=previous,
            )
            return

        min_auth_ts = time.monotonic() - 5.0
        remaining = max(15.0, timeout - age) if started else timeout
        healthy = await wait_for_cloud_health(
            hass, entry, timeout_sec=remaining, min_auth_ts=min_auth_ts
        )
        if healthy:
            clear_ota_marker(config_path)
            _LOGGER.warning(
                "UDHUB OTA pending marker cleared after healthy reconnect"
            )
            return
        await rollback_and_reload(
            hass,
            entry,
            reason="boot_health_timeout",
            target_version=target,
            previous_version=previous,
        )

    asyncio.Task(
        _watch(),
        name=f"udhub_ota_watch_{entry.entry_id}",
        context=contextvars.Context(),
    )
