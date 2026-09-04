"""HACS probe / list, plus curated repo direct install (no HACS dependency).

Curated install downloads the official GitHub release asset into
``custom_components/<domain>``. Security gates:

- Repository allowlist (Agent-side final gate; console mirrors the list)
- Explicit ``confirm=true`` (same pattern as gated backup restore)
- Extended tier enforced upstream by catalog dispatch
- sha256 verification against GitHub release digest
- zip path safety (reject absolute / ``..`` members)
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_MEMBERS = 2000

# Curated repositories — install/uninstall restricted to these entries.
# Keep in sync with console IntegrationBindPanel CURATED_REPOS.
CURATED_REPOS: dict[str, dict[str, str]] = {
    "xiaomi_home": {
        "repo": "XiaoMi/ha_xiaomi_home",
        "domain": "xiaomi_home",
        "asset": "xiaomi_home.zip",
        "label": "米家 Xiaomi Home",
    },
}


def _hacs(hass: HomeAssistant) -> Any | None:
    return hass.data.get("hacs")


def _config_path(hass: HomeAssistant) -> Path | None:
    config_dir = getattr(hass.config, "config_dir", None)
    if not config_dir:
        path_fn = getattr(hass.config, "path", None)
        config_dir = path_fn() if callable(path_fn) else path_fn
    return Path(str(config_dir)) if config_dir else None


def _resolve_curated(payload: dict[str, Any]) -> dict[str, str] | None:
    needle = str(
        payload.get("repository")
        or payload.get("repo")
        or payload.get("full_name")
        or payload.get("domain")
        or ""
    ).strip().lower()
    if not needle:
        return None
    for entry in CURATED_REPOS.values():
        if needle in (entry["repo"].lower(), entry["domain"]):
            return entry
    return None


def _installed_curated(hass: HomeAssistant) -> list[dict[str, Any]]:
    config_path = _config_path(hass)
    out: list[dict[str, Any]] = []
    for key, entry in CURATED_REPOS.items():
        item: dict[str, Any] = {
            "key": key,
            **entry,
            "installed": False,
            "version": None,
        }
        if config_path:
            manifest = (
                config_path
                / "custom_components"
                / entry["domain"]
                / "manifest.json"
            )
            if manifest.is_file():
                item["installed"] = True
                try:
                    item["version"] = json.loads(manifest.read_text()).get("version")
                except Exception:  # noqa: BLE001
                    pass
        out.append(item)
    return out


async def probe(hass: HomeAssistant) -> dict[str, Any]:
    hacs = _hacs(hass)
    curated = _installed_curated(hass)
    if hacs is None:
        # config entry presence
        entries = [
            e
            for e in hass.config_entries.async_entries()
            if e.domain == "hacs"
        ]
        return {
            "status": "ok",
            "installed": bool(entries),
            "detail": "config_entry" if entries else "not_installed",
            "curated": curated,
        }
    version = getattr(hacs, "version", None) or getattr(hacs, "system", None)
    return {
        "status": "ok",
        "installed": True,
        "version": str(version) if version else None,
        "curated": curated,
    }


async def list_repos(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    hacs = _hacs(hass)
    curated = _installed_curated(hass)
    if hacs is None:
        return {
            "status": "ok",
            "installed": False,
            "repositories": [],
            "curated": curated,
        }
    category = payload.get("category")
    repos = []
    try:
        data = getattr(hacs, "repositories", None) or getattr(hacs, "data", None)
        items = []
        if data is not None:
            items = getattr(data, "repositories", None) or list(
                getattr(data, "register", {}) or []
            )
            if isinstance(items, dict):
                items = list(items.values())
        for repo in list(items)[:300]:
            cat = getattr(repo, "category", None) or getattr(repo, "data", {}).get(
                "category"
            )
            if category and cat != category:
                continue
            repos.append(
                {
                    "id": getattr(repo, "data", {}).get("id")
                    if isinstance(getattr(repo, "data", None), dict)
                    else getattr(repo, "id", None),
                    "full_name": getattr(repo, "data", {}).get("full_name")
                    if isinstance(getattr(repo, "data", None), dict)
                    else getattr(repo, "full_name", str(repo)),
                    "category": cat,
                    "installed": getattr(repo, "data", {}).get("installed")
                    if isinstance(getattr(repo, "data", None), dict)
                    else getattr(repo, "installed", None),
                    "version_installed": getattr(repo, "data", {}).get(
                        "version_installed"
                    )
                    if isinstance(getattr(repo, "data", None), dict)
                    else None,
                }
            )
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("hacs list failed", exc_info=True)
        return {"status": "failed", "error": str(exc), "installed": True}
    return {
        "status": "ok",
        "installed": True,
        "repositories": repos,
        "curated": _installed_curated(hass),
    }


async def _fetch_latest_release(
    session: aiohttp.ClientSession, repo: str
) -> dict[str, Any]:
    async with session.get(
        f"{GITHUB_API}/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json"},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"github_api_http_{resp.status}")
        return await resp.json()


def _extract_release_zip(
    zip_path: Path, config_path: Path, domain: str
) -> dict[str, Any]:
    """Blocking: validate layout, backup old dir, extract curated members."""
    prefix = f"custom_components/{domain}/"
    target = config_path / "custom_components" / domain
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if len(names) > MAX_MEMBERS:
            raise ValueError("too_many_members")
        matched = [n for n in names if n.startswith(prefix)]
        if not matched:
            raise ValueError("invalid_archive_layout")
        for n in matched:
            if n.startswith(("/", "\\")) or ".." in Path(n).parts:
                raise ValueError("unsafe_member_path")
        if target.is_dir():
            backup = target.with_name(f"{domain}.bak")
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            shutil.copytree(target, backup)
            shutil.rmtree(target)
        zf.extractall(path=config_path, members=matched)
    return {"files": len(matched)}


async def install_repo(
    hass: HomeAssistant, payload: dict[str, Any]
) -> dict[str, Any]:
    """Download curated repo latest release and extract into custom_components."""
    if payload.get("confirm") is not True:
        return {
            "status": "denied",
            "error": "install_requires_confirm",
            "hint": "pass confirm=true after UI secondary confirmation",
        }
    entry = _resolve_curated(payload)
    if not entry:
        return {
            "status": "denied",
            "error": "repo_not_in_allowlist",
            "allowlist": [e["repo"] for e in CURATED_REPOS.values()],
        }

    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    session = async_get_clientsession(hass)
    repo = entry["repo"]
    asset_name = entry["asset"]

    try:
        release = await _fetch_latest_release(session, repo)
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}

    tag = str(release.get("tag_name") or "")
    asset = next(
        (
            a
            for a in release.get("assets") or []
            if a.get("name") == asset_name
        ),
        None,
    )
    if not asset:
        return {
            "status": "failed",
            "error": "asset_not_found",
            "tag": tag,
            "asset": asset_name,
        }
    url = asset["browser_download_url"]
    digest_header = str(asset.get("digest") or "")
    expected_sha = (
        digest_header.removeprefix("sha256:").strip().lower() or None
    )

    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=180)
        ) as resp:
            if resp.status >= 400:
                return {
                    "status": "failed",
                    "error": f"download_http_{resp.status}",
                }
            data = await resp.read()
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": f"download_failed: {exc}"}

    if len(data) > MAX_ZIP_BYTES:
        return {"status": "failed", "error": "asset_too_large"}

    digest = hashlib.sha256(data).hexdigest()
    if expected_sha and digest != expected_sha:
        return {
            "status": "failed",
            "error": "sha256_mismatch",
            "expected": expected_sha,
            "actual": digest,
        }

    config_path = _config_path(hass)
    if not config_path:
        return {"status": "failed", "error": "config_dir_unavailable"}

    tmp_dir = Path(tempfile.mkdtemp(prefix="udhub_repo_install_"))
    zip_path = tmp_dir / asset_name
    zip_path.write_bytes(data)
    try:
        try:
            stats = await hass.async_add_executor_job(
                _extract_release_zip, zip_path, config_path, entry["domain"]
            )
        except (ValueError, zipfile.BadZipFile) as exc:
            return {"status": "failed", "error": str(exc)}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Best-effort manifest version after extract
    manifest_path = (
        config_path
        / "custom_components"
        / entry["domain"]
        / "manifest.json"
    )
    manifest_version = None
    try:
        manifest_version = json.loads(manifest_path.read_text()).get("version")
    except Exception:  # noqa: BLE001
        pass

    _LOGGER.info(
        "UDHUB curated install ok repo=%s tag=%s domain=%s",
        repo,
        tag,
        entry["domain"],
    )
    return {
        "status": "ok",
        "domain": entry["domain"],
        "repo": repo,
        "version": manifest_version or tag or "unknown",
        "release_tag": tag,
        "sha256": digest,
        "files": stats.get("files"),
        "needs_restart": True,
        "hint": "restart HA (ha_system.restart) to load the new integration",
        "curated": _installed_curated(hass),
    }


async def uninstall_repo(
    hass: HomeAssistant, payload: dict[str, Any]
) -> dict[str, Any]:
    """Remove a curated integration directory (restart required to unload)."""
    if payload.get("confirm") is not True:
        return {
            "status": "denied",
            "error": "uninstall_requires_confirm",
            "hint": "pass confirm=true after UI secondary confirmation",
        }
    entry = _resolve_curated(payload)
    if not entry:
        return {
            "status": "denied",
            "error": "repo_not_in_allowlist",
            "allowlist": [e["repo"] for e in CURATED_REPOS.values()],
        }
    config_path = _config_path(hass)
    if not config_path:
        return {"status": "failed", "error": "config_dir_unavailable"}
    target = config_path / "custom_components" / entry["domain"]
    if not target.is_dir():
        return {"status": "failed", "error": "not_installed"}
    await hass.async_add_executor_job(shutil.rmtree, target)
    _LOGGER.info(
        "UDHUB curated uninstall ok repo=%s domain=%s",
        entry["repo"],
        entry["domain"],
    )
    return {
        "status": "ok",
        "domain": entry["domain"],
        "needs_restart": True,
        "curated": _installed_curated(hass),
    }


async def dispatch(hass: HomeAssistant, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action == "probe":
        return await probe(hass)
    if action == "list":
        return await list_repos(hass, payload)
    if action in ("install", "update"):
        # update == install latest release
        return await install_repo(hass, payload)
    if action == "uninstall":
        return await uninstall_repo(hass, payload)
    return {"status": "failed", "error": "unsupported_action", "action": action}
