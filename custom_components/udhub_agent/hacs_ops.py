"""HACS probe / list, plus curated repo install via SaaS mirror (GitHub fallback).

Curated install prefers ``GET /api/v1/agent/integration-mirrors/{domain}/latest``
from the Agent's configured cloud URL, then falls back to GitHub Releases.
Security gates:

- Repository allowlist (Agent-side final gate; console mirrors the list)
- Explicit ``confirm=true`` (same pattern as gated backup restore)
- Extended tier enforced upstream by catalog dispatch
- sha256 verification against SaaS manifest or GitHub release digest
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

from .const import DEFAULT_CLOUD_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
MAX_ZIP_BYTES = 50 * 1024 * 1024
MAX_MEMBERS = 2000

# Curated repositories — install/uninstall restricted to these entries.
# Keep in sync with console LocalIntegrationLibraryPanel CURATED_REPOS.
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


def _cloud_base_url(hass: HomeAssistant) -> str:
    """Prefer the live Agent config entry cloud URL."""
    store = hass.data.get(DOMAIN) or {}
    if isinstance(store, dict):
        for entry_data in store.values():
            if not isinstance(entry_data, dict):
                continue
            url = str(entry_data.get("cloud_url") or "").strip().rstrip("/")
            if url:
                return url.split(",")[0].strip().rstrip("/")
            client = entry_data.get("client")
            if client is not None:
                try:
                    url = str(getattr(client, "cloud_url", "") or "").strip().rstrip("/")
                    if url:
                        return url.split(",")[0].strip().rstrip("/")
                except Exception:  # noqa: BLE001
                    pass
    return DEFAULT_CLOUD_URL.rstrip("/")


def _extract_release_zip(
    zip_path: Path, config_path: Path, domain: str
) -> dict[str, Any]:
    """Blocking: validate layout, backup old dir, extract curated members.

    Supports both official layouts:
    - Prefixed: ``custom_components/<domain>/...``
    - Flat release asset: files at zip root (e.g. XiaoMi/ha_xiaomi_home)
    """
    prefix = f"custom_components/{domain}/"
    target = config_path / "custom_components" / domain
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if len(names) > MAX_MEMBERS:
            raise ValueError("too_many_members")
        for n in names:
            if n.startswith(("/", "\\")) or ".." in Path(n).parts:
                raise ValueError("unsafe_member_path")

        matched = [n for n in names if n.startswith(prefix)]
        flat = False
        if not matched:
            # Flat zip: treat non-directory members as domain root files.
            matched = [
                n
                for n in names
                if n and not n.endswith("/") and not n.startswith("__MACOSX/")
            ]
            if not matched:
                raise ValueError("invalid_archive_layout")
            # Require a manifest so we don't unpack random archives.
            if "manifest.json" not in matched and not any(
                n.endswith("/manifest.json") for n in matched
            ):
                raise ValueError("invalid_archive_layout")
            flat = True

        if target.is_dir():
            backup = target.with_name(f"{domain}.bak")
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            shutil.copytree(target, backup)
            shutil.rmtree(target)

        if flat:
            target.mkdir(parents=True, exist_ok=True)
            for n in matched:
                # Strip accidental top-level domain folder if present.
                rel = n
                if rel.startswith(f"{domain}/"):
                    rel = rel[len(domain) + 1 :]
                if not rel or rel.endswith("/"):
                    continue
                dest = target / rel
                if ".." in dest.relative_to(target).parts:
                    raise ValueError("unsafe_member_path")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(n) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
        else:
            zf.extractall(path=config_path, members=matched)
    return {"files": len(matched), "layout": "flat" if flat else "prefixed"}


async def _try_saas_mirror(
    session: aiohttp.ClientSession,
    cloud_base: str,
    entry: dict[str, str],
) -> dict[str, Any] | None:
    """Fetch latest package metadata + bytes from SaaS mirror; None if unavailable."""
    domain = entry["domain"]
    meta_url = f"{cloud_base}/api/v1/agent/integration-mirrors/{domain}/latest"
    try:
        async with session.get(
            meta_url, timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            if resp.status >= 400:
                _LOGGER.info(
                    "UDHUB mirror miss domain=%s http=%s", domain, resp.status
                )
                return None
            body = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.info("UDHUB mirror meta failed domain=%s: %s", domain, exc)
        return None

    pkg = body.get("package") if isinstance(body, dict) else None
    if not isinstance(pkg, dict):
        return None
    download_url = str(pkg.get("download_url") or "").strip()
    expected_sha = str(pkg.get("sha256") or "").strip().lower()
    asset_name = str(pkg.get("asset") or entry["asset"]).strip()
    if not download_url or not expected_sha:
        return None
    if asset_name != entry["asset"]:
        _LOGGER.warning(
            "UDHUB mirror asset mismatch domain=%s want=%s got=%s",
            domain,
            entry["asset"],
            asset_name,
        )
        return None

    try:
        async with session.get(
            download_url, timeout=aiohttp.ClientTimeout(total=180)
        ) as resp:
            if resp.status >= 400:
                _LOGGER.info(
                    "UDHUB mirror download miss domain=%s http=%s",
                    domain,
                    resp.status,
                )
                return None
            data = await resp.read()
    except Exception as exc:  # noqa: BLE001
        _LOGGER.info("UDHUB mirror download failed domain=%s: %s", domain, exc)
        return None

    if len(data) > MAX_ZIP_BYTES:
        return None
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha:
        _LOGGER.warning(
            "UDHUB mirror sha mismatch domain=%s expected=%s actual=%s",
            domain,
            expected_sha[:12],
            digest[:12],
        )
        return None

    return {
        "data": data,
        "sha256": digest,
        "tag": str(pkg.get("release_tag") or pkg.get("version") or ""),
        "version_hint": str(pkg.get("version") or ""),
        "source": "saas_mirror",
        "download_url": download_url,
    }


async def _try_github_release(
    session: aiohttp.ClientSession,
    entry: dict[str, str],
) -> dict[str, Any]:
    release = await _fetch_latest_release(session, entry["repo"])
    tag = str(release.get("tag_name") or "")
    asset = next(
        (
            a
            for a in release.get("assets") or []
            if a.get("name") == entry["asset"]
        ),
        None,
    )
    if not asset:
        raise RuntimeError("asset_not_found")
    url = asset["browser_download_url"]
    digest_header = str(asset.get("digest") or "")
    expected_sha = (
        digest_header.removeprefix("sha256:").strip().lower() or None
    )
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=180)) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"download_http_{resp.status}")
        data = await resp.read()
    if len(data) > MAX_ZIP_BYTES:
        raise RuntimeError("asset_too_large")
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha and digest != expected_sha:
        raise RuntimeError("sha256_mismatch")
    return {
        "data": data,
        "sha256": digest,
        "tag": tag,
        "version_hint": tag.lstrip("vV"),
        "source": "github",
        "download_url": url,
    }


async def install_repo(
    hass: HomeAssistant, payload: dict[str, Any]
) -> dict[str, Any]:
    """Download curated package (SaaS mirror first) and extract into custom_components."""
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
    cloud_base = _cloud_base_url(hass)
    prefer_github = str(payload.get("source") or "").lower() in (
        "github",
        "gh",
        "upstream",
    )

    fetched: dict[str, Any] | None = None
    errors: list[str] = []
    if not prefer_github:
        fetched = await _try_saas_mirror(session, cloud_base, entry)
        if fetched is None:
            errors.append("saas_mirror_unavailable")
    if fetched is None:
        try:
            fetched = await _try_github_release(session, entry)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            return {
                "status": "failed",
                "error": "download_failed",
                "detail": errors,
            }

    data = fetched["data"]
    digest = fetched["sha256"]
    tag = fetched["tag"]
    asset_name = entry["asset"]

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
        "UDHUB curated install ok domain=%s tag=%s source=%s",
        entry["domain"],
        tag,
        fetched.get("source"),
    )
    return {
        "status": "ok",
        "domain": entry["domain"],
        "repo": entry["repo"],
        "version": manifest_version or fetched.get("version_hint") or tag or "unknown",
        "release_tag": tag,
        "sha256": digest,
        "files": stats.get("files"),
        "layout": stats.get("layout"),
        "source": fetched.get("source"),
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
