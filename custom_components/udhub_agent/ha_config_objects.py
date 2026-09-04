"""Automation / script / scene / blueprint config surfaces."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify_name(name: str, fallback: str = "item") -> str:
    raw = str(name or fallback).strip().lower()
    ascii_slug = _SLUG_RE.sub("_", raw).strip("_")[:40]
    return ascii_slug or fallback


def _state_items(hass: HomeAssistant, domain: str) -> list[dict[str, Any]]:
    items = []
    for st in hass.states.async_all(domain):
        items.append(
            {
                "entity_id": st.entity_id,
                "name": st.name,
                "state": st.state,
                "attributes": {
                    k: st.attributes.get(k)
                    for k in ("friendly_name", "last_triggered", "mode", "current", "id")
                    if k in st.attributes
                },
            }
        )
    return items


def _resolve_collection(hass: HomeAssistant, domain: str):
    """Return HA storage collection for automation/script/scene."""
    data = hass.data.get(domain)
    if data is not None:
        if hasattr(data, "async_create_item"):
            return data
        if isinstance(data, dict):
            for v in data.values():
                if hasattr(v, "async_create_item"):
                    return v
    return None


async def _resolve_item_id(
    hass: HomeAssistant, payload: dict[str, Any]
) -> str | None:
    item_id = payload.get("id") or payload.get("item_id") or payload.get("entity_id")
    if not item_id:
        return None
    if "." in str(item_id):
        try:
            from homeassistant.helpers import entity_registry as er

            ent = er.async_get(hass).async_get(str(item_id))
            if ent:
                return str(ent.unique_id)
        except Exception:  # noqa: BLE001
            pass
    return str(item_id)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _yaml_scan_paths(hass: HomeAssistant, domain: str) -> list[Path]:
    root = Path(hass.config.config_dir)
    paths: list[Path] = []
    if domain == "automation":
        for name in ("automations.yaml", "automations.yml"):
            p = root / name
            if p.is_file():
                paths.append(p)
        auto_dir = root / "automations"
        if auto_dir.is_dir():
            paths.extend(sorted(auto_dir.glob("*.yaml")))
            paths.extend(sorted(auto_dir.glob("*.yml")))
    elif domain == "scene":
        for name in ("scenes.yaml", "scenes.yml"):
            p = root / name
            if p.is_file():
                paths.append(p)
    elif domain == "script":
        for name in ("scripts.yaml", "scripts.yml"):
            p = root / name
            if p.is_file():
                paths.append(p)
        script_dir = root / "scripts"
        if script_dir.is_dir():
            paths.extend(sorted(script_dir.glob("*.yaml")))
            paths.extend(sorted(script_dir.glob("*.yml")))
    pkg = root / "packages"
    if pkg.is_dir():
        paths.extend(sorted(pkg.glob("**/*.yaml")))
        paths.extend(sorted(pkg.glob("**/*.yml")))
    return paths


def _extract_yaml_domain_items(raw: Any, domain: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if domain == "automation":
        if isinstance(raw, list):
            for row in raw:
                if isinstance(row, dict):
                    items.append(dict(row))
            return items
        if isinstance(raw, dict):
            block = raw.get("automation")
            if isinstance(block, list):
                for row in block:
                    if isinstance(row, dict):
                        items.append(dict(row))
            elif isinstance(block, dict):
                for key, row in block.items():
                    if isinstance(row, dict):
                        cfg = dict(row)
                        cfg.setdefault("id", key)
                        items.append(cfg)
    elif domain == "scene":
        if isinstance(raw, dict):
            block = raw.get("scene")
            if isinstance(block, list):
                for row in block:
                    if isinstance(row, dict):
                        items.append(dict(row))
            elif isinstance(block, dict):
                for key, row in block.items():
                    if isinstance(row, dict):
                        cfg = dict(row)
                        cfg.setdefault("id", key)
                        items.append(cfg)
    elif domain == "script":
        if isinstance(raw, dict):
            block = raw.get("script")
            if isinstance(block, list):
                for row in block:
                    if isinstance(row, dict):
                        items.append(dict(row))
            elif isinstance(block, dict):
                for key, row in block.items():
                    if isinstance(row, dict):
                        cfg = dict(row)
                        cfg.setdefault("id", key)
                        items.append(cfg)
    return items


def _config_matches_entity(
    cfg: dict[str, Any],
    *,
    unique_id: str | None,
    entity_id: str,
    domain: str,
) -> bool:
    cid = cfg.get("id")
    if unique_id is not None and cid is not None and str(cid) == str(unique_id):
        return True
    slug = entity_id.split(".", 1)[-1] if "." in entity_id else entity_id
    if domain == "automation":
        alias = str(cfg.get("alias") or "")
        if alias and _slugify_name(alias, "auto") == slug:
            return True
    if domain == "scene":
        name = str(cfg.get("name") or "")
        if name and _slugify_name(name, "scene") == slug:
            return True
        if cid is not None and str(cid) == slug:
            return True
    if domain == "script":
        alias = str(cfg.get("alias") or "")
        if alias and _slugify_name(alias, "script") == slug:
            return True
        if cid is not None and str(cid) == slug:
            return True
    return False


def _scan_yaml_configs_sync(paths: list[Path], domain: str) -> list[dict[str, Any]]:
    import yaml

    found: list[dict[str, Any]] = []
    for path in paths:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _LOGGER.debug("yaml read failed: %s", path, exc_info=True)
            continue
        for cfg in _extract_yaml_domain_items(raw, domain):
            found.append({**cfg, "_source_path": str(path)})
    return found


def _read_storage_config_sync(
    config_dir: str, domain: str, unique_id: str
) -> dict[str, Any] | None:
    path = Path(config_dir) / ".storage" / domain
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    items = (data.get("data") or {}).get("items") or []
    if not isinstance(items, list):
        return None
    for row in items:
        if isinstance(row, dict) and str(row.get("id")) == str(unique_id):
            return dict(row)
    return None


async def _entity_registry_row(
    hass: HomeAssistant, entity_id: str
) -> tuple[str | None, str | None]:
    try:
        from homeassistant.helpers import entity_registry as er

        ent = er.async_get(hass).async_get(str(entity_id))
        if not ent:
            return None, None
        return str(ent.unique_id) if ent.unique_id else None, str(ent.entity_id)
    except Exception:  # noqa: BLE001
        return None, None


async def _get_config_from_yaml_or_storage(
    hass: HomeAssistant, domain: str, entity_id: str
) -> dict[str, Any] | None:
    unique_id, eid = await _entity_registry_row(hass, entity_id)
    if not eid:
        eid = entity_id

    if unique_id:
        stored = await hass.async_add_executor_job(
            _read_storage_config_sync,
            hass.config.config_dir,
            domain,
            unique_id,
        )
        if stored:
            stored["_config_source"] = "storage_file"
            return stored

    paths = _yaml_scan_paths(hass, domain)
    if paths:
        rows = await hass.async_add_executor_job(
            _scan_yaml_configs_sync, paths, domain
        )
        for cfg in rows:
            if _config_matches_entity(
                cfg, unique_id=unique_id, entity_id=eid, domain=domain
            ):
                out = dict(cfg)
                out.pop("_source_path", None)
                out["_config_source"] = "yaml"
                return out
    return None


async def _get_collection_config(
    hass: HomeAssistant, domain: str, payload: dict[str, Any]
) -> dict[str, Any]:
    entity_id = payload.get("entity_id") or payload.get("id")
    if entity_id and "." in str(entity_id):
        yaml_cfg = await _get_config_from_yaml_or_storage(
            hass, domain, str(entity_id)
        )
        if yaml_cfg:
            src = yaml_cfg.pop("_config_source", "yaml")
            cfg_id = yaml_cfg.get("id")
            return {
                "status": "ok",
                "config": _json_safe(yaml_cfg),
                "id": str(cfg_id) if cfg_id is not None else None,
                "entity_id": str(entity_id),
                "source": src,
            }

    coll = _resolve_collection(hass, domain)
    if coll is None:
        if entity_id:
            yaml_cfg = await _get_config_from_yaml_or_storage(
                hass, domain, str(entity_id)
            )
            if yaml_cfg:
                src = yaml_cfg.pop("_config_source", "yaml")
                cfg_id = yaml_cfg.get("id")
                return {
                    "status": "ok",
                    "config": _json_safe(yaml_cfg),
                    "id": str(cfg_id) if cfg_id is not None else None,
                    "entity_id": str(entity_id),
                    "source": src,
                }
        return {
            "status": "failed",
            "error": "config_collection_unavailable",
            "domain": domain,
        }
    item_id = await _resolve_item_id(hass, payload)
    if not item_id:
        return {"status": "failed", "error": "missing_id"}
    source = None
    getter = getattr(coll, "async_get_item", None)
    if callable(getter):
        try:
            source = await getter(str(item_id))
        except Exception:  # noqa: BLE001
            source = None
    if source is None:
        for it in list(getattr(coll, "items", None) or []):
            if isinstance(it, dict) and str(it.get("id")) == str(item_id):
                source = it
                break
    if not isinstance(source, dict) and entity_id and "." in str(entity_id):
        yaml_cfg = await _get_config_from_yaml_or_storage(
            hass, domain, str(entity_id)
        )
        if yaml_cfg:
            src = yaml_cfg.pop("_config_source", "yaml")
            cfg_id = yaml_cfg.get("id")
            return {
                "status": "ok",
                "config": _json_safe(yaml_cfg),
                "id": str(cfg_id) if cfg_id is not None else None,
                "entity_id": str(entity_id),
                "source": src,
            }
    if not isinstance(source, dict):
        return {"status": "failed", "error": "not_found", "id": str(item_id)}
    return {
        "status": "ok",
        "config": _json_safe(source),
        "id": str(item_id),
        "source": "collection",
    }


async def _automation_trace(
    hass: HomeAssistant, action: str, payload: dict[str, Any]
) -> dict[str, Any]:
    entity_id = payload.get("entity_id") or payload.get("item_id")
    if not entity_id:
        return {"status": "failed", "error": "missing_entity_id"}
    key = str(entity_id)
    try:
        from homeassistant.components.trace.util import async_get_trace, async_list_traces
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": f"trace_unavailable:{exc}"}

    trace_keys: list[str] = [key]
    uid, _ = await _entity_registry_row(hass, key)
    if uid and uid not in trace_keys:
        trace_keys.append(uid)
    cfg_got = await _get_collection_config(hass, "automation", {"entity_id": key})
    if cfg_got.get("status") == "ok":
        cfg_id = cfg_got.get("id")
        if cfg_id and str(cfg_id) not in trace_keys:
            trace_keys.append(str(cfg_id))
        cfg = cfg_got.get("config") or {}
        if isinstance(cfg, dict):
            alias = cfg.get("alias")
            if alias:
                slug = _slugify_name(str(alias), "auto")
                ent_slug = key.split(".", 1)[-1] if "." in key else key
                if slug and slug not in trace_keys and slug != ent_slug:
                    trace_keys.append(slug)

    if action == "trace_list":
        traces: list[dict[str, Any]] = []
        resolved_key = key
        for trace_key in trace_keys:
            try:
                batch = await async_list_traces(hass, "automation", trace_key)
            except Exception:  # noqa: BLE001
                batch = []
            if batch:
                traces = batch
                resolved_key = trace_key
                break
        limit = int(payload.get("limit") or 10)
        return {
            "status": "ok",
            "entity_id": resolved_key,
            "trace_keys_tried": trace_keys,
            "traces": traces[:limit],
        }

    if action == "trace_get":
        run_id = payload.get("run_id")
        if not run_id:
            return {"status": "failed", "error": "missing_run_id"}
        traces = None
        resolved_key = key
        for trace_key in trace_keys:
            try:
                traces = await async_get_trace(hass, trace_key, str(run_id))
            except Exception:  # noqa: BLE001
                traces = None
            if traces:
                resolved_key = trace_key
                break
        if not traces:
            return {
                "status": "failed",
                "error": "trace_not_found",
                "run_id": str(run_id),
                "trace_keys_tried": trace_keys,
            }
        out: dict[str, Any] = {}
        for section, trace in traces.items():
            if hasattr(trace, "as_dict"):
                out[section] = _json_safe(trace.as_dict())
            elif isinstance(trace, dict):
                out[section] = _json_safe(trace)
            else:
                out[section] = _json_safe(getattr(trace, "__dict__", str(trace)))
        return {"status": "ok", "entity_id": resolved_key, "run_id": str(run_id), "trace": out}

    return {"status": "failed", "error": "unsupported_action", "action": action}


def _patch_yaml_automation_initial_state_sync(
    path: Path, entity_id: str, unique_id: str | None, enabled: bool
) -> bool:
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    slug = entity_id.split(".", 1)[-1] if "." in entity_id else entity_id

    def patch_cfg(cfg: dict[str, Any]) -> bool:
        if not isinstance(cfg, dict):
            return False
        if _config_matches_entity(
            cfg, unique_id=unique_id, entity_id=entity_id, domain="automation"
        ) or _slugify_name(str(cfg.get("alias") or ""), "auto") == slug:
            cfg["initial_state"] = enabled
            return True
        return False

    matched = False
    if isinstance(raw, list):
        for cfg in raw:
            if patch_cfg(cfg):
                matched = True
                break
    elif isinstance(raw, dict):
        block = raw.get("automation")
        if isinstance(block, list):
            for cfg in block:
                if patch_cfg(cfg):
                    matched = True
                    break
        elif isinstance(block, dict):
            for key, cfg in block.items():
                if isinstance(cfg, dict):
                    if patch_cfg(cfg):
                        matched = True
                        break
    if not matched:
        return False
    try:
        path.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        return False
    return True


def _replace_yaml_domain_item_sync(
    path: Path,
    domain: str,
    entity_id: str,
    unique_id: str | None,
    new_cfg: dict[str, Any],
) -> bool:
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False

    replaced = False

    def merge_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
        merged = dict(cfg)
        merged.update(new_cfg)
        if cfg.get("id") is not None and "id" not in new_cfg:
            merged["id"] = cfg["id"]
        return merged

    def replace_in_list(items: list) -> None:
        nonlocal replaced
        for i, cfg in enumerate(items):
            if isinstance(cfg, dict) and _config_matches_entity(
                cfg, unique_id=unique_id, entity_id=entity_id, domain=domain
            ):
                items[i] = merge_cfg(cfg)
                replaced = True
                return

    if domain == "automation":
        if isinstance(raw, list):
            replace_in_list(raw)
        elif isinstance(raw, dict):
            block = raw.get("automation")
            if isinstance(block, list):
                replace_in_list(block)
            elif isinstance(block, dict):
                for key, cfg in block.items():
                    if isinstance(cfg, dict) and _config_matches_entity(
                        cfg, unique_id=unique_id, entity_id=entity_id, domain=domain
                    ):
                        block[key] = merge_cfg(cfg)
                        replaced = True
                        break
    elif domain == "scene":
        if isinstance(raw, dict):
            block = raw.get("scene")
            if isinstance(block, list):
                replace_in_list(block)
            elif isinstance(block, dict):
                for key, cfg in block.items():
                    if isinstance(cfg, dict) and _config_matches_entity(
                        cfg, unique_id=unique_id, entity_id=entity_id, domain=domain
                    ):
                        block[key] = merge_cfg(cfg)
                        replaced = True
                        break
    elif domain == "script":
        if isinstance(raw, dict):
            block = raw.get("script")
            if isinstance(block, list):
                replace_in_list(block)
            elif isinstance(block, dict):
                for key, cfg in block.items():
                    if isinstance(cfg, dict) and _config_matches_entity(
                        cfg, unique_id=unique_id, entity_id=entity_id, domain=domain
                    ):
                        block[key] = merge_cfg(cfg)
                        replaced = True
                        break

    if not replaced:
        return False
    try:
        path.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        return False
    return True


def _update_storage_config_sync(
    config_dir: str, domain: str, unique_id: str, new_cfg: dict[str, Any]
) -> bool:
    path = Path(config_dir) / ".storage" / domain
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    items = (data.get("data") or {}).get("items") or []
    if not isinstance(items, list):
        return False
    matched = False
    for i, row in enumerate(items):
        if isinstance(row, dict) and str(row.get("id")) == str(unique_id):
            merged = dict(row)
            merged.update(new_cfg)
            if row.get("id") is not None and "id" not in new_cfg:
                merged["id"] = row["id"]
            items[i] = merged
            matched = True
            break
    if not matched:
        return False
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        return False
    return True


async def _reload_domain(hass: HomeAssistant, domain: str) -> None:
    svc = {"automation": "automation", "scene": "scene", "script": "script"}.get(
        domain
    )
    if not svc:
        return
    try:
        await hass.services.async_call(svc, "reload", {}, blocking=True)
    except Exception:  # noqa: BLE001
        if domain == "automation":
            try:
                await hass.services.async_call(
                    "homeassistant", "reload_config_entry", {}, blocking=True
                )
            except Exception:  # noqa: BLE001
                pass


async def _update_source_config(
    hass: HomeAssistant, domain: str, payload: dict[str, Any]
) -> dict[str, Any]:
    entity_id = payload.get("entity_id")
    if not entity_id:
        return {"status": "failed", "error": "missing_entity_id"}
    eid = str(entity_id)
    # SaaS-managed packages entities must not be edited via HA in-place writeback
    object_id = eid.split(".", 1)[-1]
    unique_id_pre, _ = await _entity_registry_row(hass, eid)
    managed = object_id.startswith("udhub_") or (
        unique_id_pre is not None and str(unique_id_pre).startswith("udhub_")
    )
    if managed and not payload.get("allow_ha_writeback"):
        return {
            "status": "failed",
            "error": "managed_use_config_write",
            "hint": "托管实体请走 packages/config-write；紧急旁路可传 allow_ha_writeback=true",
            "authority": "saas",
            "entity_id": eid,
        }

    config = payload.get("config")
    yaml_text = payload.get("yaml")

    if yaml_text and domain == "automation":
        try:
            import yaml

            parsed = yaml.safe_load(str(yaml_text))
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": f"yaml_parse_failed:{exc}"}
        items = _extract_yaml_domain_items(
            parsed if isinstance(parsed, dict) else {"automation": parsed},
            domain,
        )
        if not items:
            return {"status": "failed", "error": "yaml_parse_empty"}
        config = items[0]

    if not config or not isinstance(config, dict):
        return {"status": "failed", "error": "missing_config"}

    got = await _get_collection_config(hass, domain, {"entity_id": eid})
    source = got.get("source") if got.get("status") == "ok" else None
    cfg_id = got.get("id") if got.get("status") == "ok" else None

    coll = _resolve_collection(hass, domain)
    item_id = await _resolve_item_id(hass, {"entity_id": eid, "id": cfg_id})
    if coll is not None and item_id and source in (None, "collection"):
        try:
            existing = None
            getter = getattr(coll, "async_get_item", None)
            if callable(getter):
                try:
                    existing = await getter(str(item_id))
                except Exception:  # noqa: BLE001
                    existing = None
            merged = dict(existing) if isinstance(existing, dict) else {}
            merged.update(config)
            if isinstance(existing, dict) and existing.get("id") is not None:
                merged["id"] = existing["id"]
            updated = await coll.async_update_item(str(item_id), merged)
            await _reload_domain(hass, domain)
            return {
                "status": "ok",
                "entity_id": eid,
                "mode": "collection",
                "authority": "ha_non_authoritative",
                "item": updated if isinstance(updated, dict) else {"id": str(item_id)},
            }
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("collection update failed, trying yaml/storage", exc_info=True)
            if source == "collection":
                return {"status": "failed", "error": str(exc)}

    unique_id, _ = await _entity_registry_row(hass, eid)
    if unique_id and source == "storage_file":
        ok = await hass.async_add_executor_job(
            _update_storage_config_sync,
            hass.config.config_dir,
            domain,
            unique_id,
            config,
        )
        if ok:
            await _reload_domain(hass, domain)
            return {
                "status": "ok",
                "entity_id": eid,
                "mode": "storage_file",
                "authority": "ha_non_authoritative",
                "id": unique_id,
            }

    paths = _yaml_scan_paths(hass, domain)
    patched_path: str | None = None
    for path in paths:
        ok = await hass.async_add_executor_job(
            _replace_yaml_domain_item_sync,
            path,
            domain,
            eid,
            unique_id,
            config,
        )
        if ok:
            patched_path = str(path)
            break

    if not patched_path:
        return {"status": "failed", "error": "source_not_found"}

    await _reload_domain(hass, domain)
    return {
        "status": "ok",
        "entity_id": eid,
        "mode": "yaml",
        "authority": "ha_non_authoritative",
        "path": patched_path,
    }


async def _disable_automation_source(
    hass: HomeAssistant, payload: dict[str, Any]
) -> dict[str, Any]:
    entity_id = payload.get("entity_id")
    if not entity_id:
        return {"status": "failed", "error": "missing_entity_id"}
    eid = str(entity_id)
    mode = str(payload.get("mode") or "disable_entity")
    if mode in ("disable_entity", "entity_off"):
        await hass.services.async_call(
            "automation", "turn_off", {"entity_id": eid}, blocking=True
        )
        return {"status": "ok", "entity_id": eid, "mode": "disable_entity"}
    if mode in ("disable_yaml", "yaml_off"):
        unique_id, _ = await _entity_registry_row(hass, eid)
        paths = _yaml_scan_paths(hass, "automation")
        patched = False
        patched_path: str | None = None
        for path in paths:
            ok = await hass.async_add_executor_job(
                _patch_yaml_automation_initial_state_sync,
                path,
                eid,
                unique_id,
                False,
            )
            if ok:
                patched = True
                patched_path = str(path)
                break
        if patched:
            try:
                await hass.services.async_call(
                    "homeassistant", "reload_config_entry", {}, blocking=True
                )
            except Exception:  # noqa: BLE001
                try:
                    await hass.services.async_call(
                        "automation", "reload", {}, blocking=True
                    )
                except Exception:  # noqa: BLE001
                    pass
        await hass.services.async_call(
            "automation", "turn_off", {"entity_id": eid}, blocking=True
        )
        return {
            "status": "ok",
            "entity_id": eid,
            "mode": "disable_yaml",
            "yaml_patched": patched,
            "path": patched_path,
        }
    return {"status": "failed", "error": "unsupported_mode", "mode": mode}


async def _export_managed_automation_yaml(
    hass: HomeAssistant, payload: dict[str, Any]
) -> dict[str, Any]:
    got = await _get_collection_config(hass, "automation", payload)
    if got.get("status") != "ok":
        return got
    cfg = dict(got.get("config") or {})
    title = str(
        payload.get("name")
        or cfg.get("alias")
        or cfg.get("description")
        or "automation"
    ).strip()
    slug = _slugify_name(title, "automation")
    new_id = f"udhub_{slug}"
    cfg.pop("id", None)
    cfg["id"] = new_id
    if "alias" not in cfg:
        cfg["alias"] = title
    if "description" not in cfg:
        cfg["description"] = "云枢托管（自 HA 转换）"
    try:
        import yaml

        content = yaml.safe_dump(
            {"automation": [cfg]},
            allow_unicode=True,
            sort_keys=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": f"yaml_export_failed:{exc}"}
    header = "# UDHUB managed automation — converted from HA\n"
    return {
        "status": "ok",
        "yaml": header + content,
        "automation_id": new_id,
        "name": title,
        "source": got.get("source"),
    }


async def _export_managed_scene_yaml(
    hass: HomeAssistant, payload: dict[str, Any]
) -> dict[str, Any]:
    got = await _get_collection_config(hass, "scene", payload)
    if got.get("status") != "ok":
        return got
    cfg = dict(got.get("config") or {})
    title = str(
        payload.get("name") or cfg.get("name") or cfg.get("id") or "scene"
    ).strip()
    slug = _slugify_name(title, "scene")
    new_id = f"udhub_{slug}"
    entities = cfg.get("entities") or {}
    if not isinstance(entities, dict):
        entities = {}
    scene_item: dict[str, Any] = {
        "id": new_id,
        "name": title if title else f"UDHUB · {slug}",
        "entities": entities,
    }
    try:
        import yaml

        content = yaml.safe_dump(
            {"scene": [scene_item]},
            allow_unicode=True,
            sort_keys=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": f"yaml_export_failed:{exc}"}
    header = "# UDHUB managed scene — converted from HA\n"
    return {
        "status": "ok",
        "yaml": header + content,
        "scene_id": new_id,
        "name": scene_item["name"],
        "source": got.get("source"),
    }


async def _collection_crud(
    hass: HomeAssistant,
    domain: str,
    action: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Best-effort storage collection CRUD for automation/script/scene."""
    if action == "get_config":
        return await _get_collection_config(hass, domain, payload)
    coll = _resolve_collection(hass, domain)
    if action == "list":
        return {"status": "ok", "items": _state_items(hass, domain), "domain": domain}
    if action == "get":
        entity_id = payload.get("entity_id") or payload.get("id")
        if not entity_id:
            return {"status": "failed", "error": "missing_entity_id"}
        st = hass.states.get(str(entity_id))
        if not st:
            return {"status": "failed", "error": "not_found"}
        return {
            "status": "ok",
            "item": {
                "entity_id": st.entity_id,
                "state": st.state,
                "attributes": dict(st.attributes),
            },
        }
    if action in ("trigger", "run", "apply"):
        # automation.trigger / script.turn_on / scene.turn_on
        entity_id = payload.get("entity_id") or payload.get("id")
        if not entity_id:
            return {"status": "failed", "error": "missing_entity_id"}
        svc = {"automation": "trigger", "script": "turn_on", "scene": "turn_on"}.get(
            domain
        )
        if not svc:
            return {"status": "failed", "error": "unsupported_domain", "domain": domain}
        data: dict[str, Any] = {"entity_id": str(entity_id)}
        if domain == "automation" and payload.get("skip_condition") is not False:
            data["skip_condition"] = True
        await hass.services.async_call(domain, svc, data, blocking=True)
        return {"status": "ok", "entity_id": str(entity_id), "action": action}
    if coll is None:
        return {
            "status": "failed",
            "error": "config_collection_unavailable",
            "hint": "use config_write packages or ha_ws allowlisted types",
            "domain": domain,
        }
    if action == "create":
        cfg = payload.get("config") or payload.get("data") or {}
        if not isinstance(cfg, dict):
            return {"status": "failed", "error": "config_must_be_object"}
        try:
            created = await coll.async_create_item(cfg)
            return {"status": "ok", "item": created if isinstance(created, dict) else {"id": str(created)}}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc)}
    if action == "update":
        item_id = payload.get("id") or payload.get("item_id")
        cfg = payload.get("config") or payload.get("data") or {}
        if not item_id:
            return {"status": "failed", "error": "missing_id"}
        try:
            updated = await coll.async_update_item(str(item_id), cfg)
            return {"status": "ok", "item": updated}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc)}
    if action == "delete":
        item_id = payload.get("id") or payload.get("item_id") or payload.get("entity_id")
        if not item_id:
            return {"status": "failed", "error": "missing_id"}
        item_id = await _resolve_item_id(hass, {"entity_id": item_id, "id": item_id})
        if not item_id:
            return {"status": "failed", "error": "missing_id"}
        try:
            await coll.async_delete_item(str(item_id))
            return {"status": "ok", "id": item_id}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc)}
    if action == "duplicate":
        item_id = payload.get("id") or payload.get("item_id") or payload.get("entity_id")
        if not item_id:
            return {"status": "failed", "error": "missing_id"}
        item_id = await _resolve_item_id(hass, {"entity_id": item_id, "id": item_id})
        if not item_id:
            return {"status": "failed", "error": "missing_id"}
        try:
            source = None
            getter = getattr(coll, "async_get_item", None)
            if callable(getter):
                try:
                    source = await getter(str(item_id))
                except Exception:  # noqa: BLE001
                    source = None
            if source is None:
                for it in list(getattr(coll, "items", None) or []):
                    if isinstance(it, dict) and str(it.get("id")) == str(item_id):
                        source = it
                        break
            if not isinstance(source, dict):
                return {"status": "failed", "error": "not_found", "id": str(item_id)}
            copy_item = dict(source)
            copy_item.pop("id", None)
            new_name = f"{copy_item.get('name') or item_id} 副本"
            copy_item["name"] = new_name
            aliases = [a for a in (copy_item.get("alias") or []) if isinstance(a, str)]
            if "alias" in copy_item:
                copy_item["alias"] = aliases + [new_name]
            created = await coll.async_create_item(copy_item)
            return {
                "status": "ok",
                "item": created if isinstance(created, dict) else {"id": str(created)},
                "source_id": str(item_id),
            }
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc)}
    return {"status": "failed", "error": "unsupported_action", "action": action}


async def _export_managed_script_yaml(
    hass: HomeAssistant, payload: dict[str, Any]
) -> dict[str, Any]:
    got = await _get_collection_config(hass, "script", payload)
    if got.get("status") != "ok":
        return got
    cfg = dict(got.get("config") or {})
    title = str(
        payload.get("name")
        or cfg.get("alias")
        or cfg.get("description")
        or payload.get("entity_id")
        or "script"
    ).strip()
    slug = _slugify_name(title, "script")
    # HA scripts.yaml is mapping: key → config
    key = f"udhub_{slug}"
    body = dict(cfg)
    body.pop("id", None)
    if "alias" not in body:
        body["alias"] = title
    try:
        import yaml

        content = yaml.safe_dump(
            {"script": {key: body}},
            allow_unicode=True,
            sort_keys=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": f"yaml_export_failed:{exc}"}
    header = "# UDHUB managed script — converted from HA\n"
    return {
        "status": "ok",
        "yaml": header + content,
        "script_id": key,
        "name": title,
        "source": got.get("source"),
    }


async def dispatch_automation(hass: HomeAssistant, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action in ("trace_list", "trace_get"):
        return await _automation_trace(hass, action, payload)
    if action == "export_managed_yaml":
        return await _export_managed_automation_yaml(hass, payload)
    if action == "disable_source":
        return await _disable_automation_source(hass, payload)
    if action == "update_source":
        return await _update_source_config(hass, "automation", payload)
    return await _collection_crud(hass, "automation", action, payload)


async def dispatch_script(hass: HomeAssistant, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action == "export_managed_yaml":
        return await _export_managed_script_yaml(hass, payload)
    if action == "update_source":
        return await _update_source_config(hass, "script", payload)
    return await _collection_crud(hass, "script", action, payload)


async def dispatch_scene(hass: HomeAssistant, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action == "export_managed_yaml":
        return await _export_managed_scene_yaml(hass, payload)
    if action == "update_source":
        return await _update_source_config(hass, "scene", payload)
    return await _collection_crud(hass, "scene", action, payload)


async def dispatch_blueprint(hass: HomeAssistant, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        from homeassistant.components import blueprint
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": f"blueprint_unavailable:{exc}"}

    if action == "list":
        items: list[dict[str, Any]] = []
        try:
            # domain blueprints under hass.data
            for domain in ("automation", "script", "template"):
                store = hass.data.get(f"blueprint.{domain}") or hass.data.get(blueprint.DOMAIN)
                if store is None:
                    continue
                blueprints = getattr(store, "blueprints", None) or {}
                if isinstance(blueprints, dict):
                    for path, meta in blueprints.items():
                        items.append(
                            {
                                "domain": domain,
                                "path": str(path),
                                "name": getattr(meta, "blueprint", meta).get("name")
                                if isinstance(getattr(meta, "blueprint", meta), dict)
                                else str(path),
                            }
                        )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("blueprint list failed", exc_info=True)
        return {"status": "ok", "blueprints": items}
    if action == "import":
        url = payload.get("url") or payload.get("path")
        if not url:
            return {"status": "failed", "error": "missing_url"}

        import re

        import yaml

        # Download on the event loop (async), write on the executor.
        text = ""
        if str(url).startswith("/") and Path(str(url)).is_file():
            text = Path(str(url)).read_text()
        else:
            try:
                import aiohttp

                from homeassistant.helpers.aiohttp_client import (
                    async_get_clientsession,
                )

                session = async_get_clientsession(hass)
                async with session.get(
                    str(url), timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    if resp.status >= 400:
                        return {
                            "status": "failed",
                            "error": f"download_http_{resp.status}",
                            "url": url,
                        }
                    text = await resp.text()
            except Exception as exc:  # noqa: BLE001
                return {"status": "failed", "error": f"download_failed: {exc}"}

        try:
            meta = (yaml.safe_load(text) or {}).get("blueprint") or {}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": f"yaml_parse_failed: {exc}"}
        domain = payload.get("domain") or meta.get("domain")
        if not domain:
            return {
                "status": "failed",
                "error": "blueprint_domain_unknown",
                "hint": "pass domain in payload (automation / script / template)",
            }

        def _write() -> str:
            from pathlib import Path

            base = Path(hass.config.path("blueprints")) / str(domain)
            base.mkdir(parents=True, exist_ok=True)
            filename = str(payload.get("filename") or "").strip()
            if not filename:
                basename = re.split(r"[?#]", str(url).rstrip("/"))[-1]
                filename = basename.rsplit("/", 1)[-1] or "imported"
            filename = Path(filename).name  # strip any path parts
            if not filename.endswith((".yaml", ".yml")):
                filename += ".yaml"
            target = base / filename
            if target.exists() and not payload.get("overwrite"):
                return f"__exists__{filename}"
            target.write_text(text)
            return filename

        try:
            result_name = await hass.async_add_executor_job(_write)
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc)}
        if isinstance(result_name, str) and result_name.startswith("__exists__"):
            return {
                "status": "failed",
                "error": "blueprint_already_exists",
                "filename": result_name.replace("__exists__", ""),
                "hint": "pass overwrite=true to replace",
            }
        return {
            "status": "ok",
            "domain": domain,
            "path": f"blueprints/{domain}/{result_name}",
        }
    if action == "delete":
        path = str(payload.get("path") or "").strip()
        if not path:
            return {"status": "failed", "error": "missing_path"}

        def _delete() -> None:
            from pathlib import Path

            rel = path.removeprefix("blueprints/").lstrip("/")
            parts = Path(rel).parts
            if not parts or any(p in ("..", "") for p in parts):
                raise ValueError("unsafe_path")
            if len(parts) < 2:
                raise ValueError("path_must_include_domain")
            target = Path(hass.config.path("blueprints")).joinpath(*parts)
            base = Path(hass.config.path("blueprints"))
            if base not in target.parents and target.parent != base:
                raise ValueError("unsafe_path")
            if not target.is_file():
                raise FileNotFoundError("not_found")
            target.unlink()

        try:
            await hass.async_add_executor_job(_delete)
        except FileNotFoundError:
            return {"status": "failed", "error": "not_found", "path": path}
        except ValueError as exc:
            return {"status": "failed", "error": str(exc), "path": path}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc)}
        return {"status": "ok", "path": path}
    return {"status": "failed", "error": "unsupported_action", "action": action}
