"""Config snapshot for SaaS drift detection (doc 20 §4.1)."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_file_sync(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        st = path.stat()
        raw = path.read_bytes()
    except OSError:
        return None
    rel = str(path)
    return {
        "path": rel,
        "sha256": _sha256_bytes(raw),
        "size": len(raw),
        "mtime": int(st.st_mtime),
    }


def _collect_yaml_hashes_sync(config_dir: str) -> list[dict[str, Any]]:
    root = Path(config_dir)
    out: list[dict[str, Any]] = []
    packages = root / "packages"
    if packages.is_dir():
        for p in sorted(packages.rglob("*")):
            if p.suffix.lower() in (".yaml", ".yml") and p.is_file():
                row = _hash_file_sync(p)
                if row:
                    try:
                        row["path"] = str(p.relative_to(root))
                    except ValueError:
                        pass
                    out.append(row)
    for name in ("automations.yaml", "scenes.yaml", "scripts.yaml"):
        p = root / name
        row = _hash_file_sync(p)
        if row:
            row["path"] = name
            out.append(row)
    blueprints = root / "blueprints"
    if blueprints.is_dir():
        for p in sorted(blueprints.rglob("*.yaml")):
            if p.is_file():
                row = _hash_file_sync(p)
                if row:
                    try:
                        row["path"] = str(p.relative_to(root))
                    except ValueError:
                        pass
                    out.append(row)
            elif p.suffix.lower() == ".yml" and p.is_file():
                row = _hash_file_sync(p)
                if row:
                    try:
                        row["path"] = str(p.relative_to(root))
                    except ValueError:
                        pass
                    out.append(row)
    return out


# Cap .storage inventory: version manifest only (no file contents in snapshot payload).
_STORAGE_MAX_FILES = 200
_STORAGE_MAX_BYTES = 8 * 1024 * 1024  # skip hashing files larger than 8 MiB


def _collect_storage_manifest_sync(config_dir: str) -> list[dict[str, Any]]:
    """Inventory /config/.storage top-level files: name + mtime/size/sha256."""
    root = Path(config_dir)
    storage = root / ".storage"
    if not storage.is_dir():
        return []
    out: list[dict[str, Any]] = []
    try:
        entries = sorted(storage.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for p in entries:
        if len(out) >= _STORAGE_MAX_FILES:
            break
        if not p.is_file():
            # Skip auth/ directories; only top-level registry-like files
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        size = int(st.st_size)
        row: dict[str, Any] = {
            "name": p.name,
            "path": f".storage/{p.name}",
            "size": size,
            "mtime": int(st.st_mtime),
        }
        if size > _STORAGE_MAX_BYTES:
            row["sha256"] = None
            row["skipped"] = "too_large"
        else:
            hashed = _hash_file_sync(p)
            if hashed:
                row["sha256"] = hashed["sha256"]
            else:
                row["sha256"] = None
                row["skipped"] = "unreadable"
        out.append(row)
    return out


def _storage_fingerprint(rows: list[dict[str, Any]]) -> str:
    parts = [
        f"{r.get('name')}:{r.get('mtime')}:{r.get('size')}:{r.get('sha256') or '-'}"
        for r in sorted(rows, key=lambda x: str(x.get("name") or ""))
    ]
    return _sha256_text("|".join(parts))


def _registry_device_rows(hass: HomeAssistant) -> list[dict[str, Any]]:
    try:
        from homeassistant.helpers import device_registry as dr

        from .device_registry_fields import serialize_device_row

        reg = dr.async_get(hass)
        by_id = {d.id: d for d in reg.devices.values()}
        rows = []
        for d in reg.devices.values():
            if getattr(d, "disabled_by", None):
                continue
            payload = serialize_device_row(hass, d, devices_by_id=by_id)
            rows.append(
                {
                    **payload,
                    "fingerprint": _sha256_text(json.dumps(payload, sort_keys=True)),
                }
            )
        return rows
    except Exception:  # noqa: BLE001
        _LOGGER.debug("device registry snapshot failed", exc_info=True)
        return []


def _registry_entity_summary(hass: HomeAssistant) -> dict[str, Any]:
    try:
        from homeassistant.helpers import entity_registry as er

        reg = er.async_get(hass)
        total = 0
        disabled = 0
        by_domain: dict[str, int] = {}
        fingerprints: list[str] = []
        for e in reg.entities.values():
            total += 1
            if e.disabled_by:
                disabled += 1
            domain = e.entity_id.split(".", 1)[0]
            by_domain[domain] = by_domain.get(domain, 0) + 1
            if domain in ("automation", "scene", "script"):
                fp = {
                    "entity_id": e.entity_id,
                    "unique_id": e.unique_id,
                    "disabled_by": str(e.disabled_by) if e.disabled_by else None,
                    "area_id": e.area_id,
                }
                fingerprints.append(
                    _sha256_text(json.dumps(fp, sort_keys=True))
                )
        fingerprints.sort()
        return {
            "total": total,
            "disabled": disabled,
            "by_domain": by_domain,
            "linkage_fingerprint": _sha256_text("|".join(fingerprints)),
        }
    except Exception:  # noqa: BLE001
        _LOGGER.debug("entity registry snapshot failed", exc_info=True)
        return {"total": 0, "disabled": 0, "by_domain": {}, "linkage_fingerprint": ""}


def _area_rows(hass: HomeAssistant) -> list[dict[str, Any]]:
    try:
        from homeassistant.helpers import area_registry as ar

        reg = ar.async_get(hass)
        rows = []
        for a in reg.areas.values():
            payload = {"id": a.id, "name": a.name}
            rows.append(
                {
                    **payload,
                    "fingerprint": _sha256_text(json.dumps(payload, sort_keys=True)),
                }
            )
        return rows
    except Exception:  # noqa: BLE001
        return []


async def collect_config_snapshot(
    hass: HomeAssistant, _payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build snapshot: file hashes + .storage inventory + registry fingerprints."""
    config_dir = hass.config.config_dir
    files = await hass.async_add_executor_job(_collect_yaml_hashes_sync, config_dir)
    storage = await hass.async_add_executor_job(
        _collect_storage_manifest_sync, config_dir
    )
    devices = _registry_device_rows(hass)
    entities = _registry_entity_summary(hass)
    areas = _area_rows(hass)
    units = getattr(hass.config, "units", None)
    unit_name = getattr(units, "name", None) or getattr(units, "_name", None)
    system = {
        "location_name": str(hass.config.location_name or ""),
        "time_zone": str(hass.config.time_zone or ""),
        "language": str(getattr(hass.config, "language", None) or ""),
        "unit_system": str(unit_name or "unknown"),
        "latitude": round(float(hass.config.latitude), 6)
        if hass.config.latitude is not None
        else None,
        "longitude": round(float(hass.config.longitude), 6)
        if hass.config.longitude is not None
        else None,
        "elevation": int(hass.config.elevation)
        if hass.config.elevation is not None
        else None,
    }
    storage_fp = _storage_fingerprint(storage)
    manifest = {
        "files": files,
        "storage_fingerprint": storage_fp,
        "devices_count": len(devices),
        "devices_fingerprint": _sha256_text(
            "|".join(sorted(d["fingerprint"] for d in devices))
        ),
        "areas_fingerprint": _sha256_text(
            "|".join(sorted(a["fingerprint"] for a in areas))
        ),
        "entities": entities,
        "system": system,
    }
    return {
        "status": "ok",
        "snapshot": {
            "files": files,
            "storage": storage,
            "storage_fingerprint": storage_fp,
            "devices": devices[:500],
            "areas": areas,
            "entities": entities,
            "system": system,
            "manifest_sha256": _sha256_text(
                json.dumps(
                    {
                        "files": [(f.get("path"), f.get("sha256")) for f in files],
                        "storage_fingerprint": storage_fp,
                        "devices_fingerprint": manifest["devices_fingerprint"],
                        "areas_fingerprint": manifest["areas_fingerprint"],
                        "entities": entities,
                        "system": system,
                    },
                    sort_keys=True,
                )
            ),
        },
    }
