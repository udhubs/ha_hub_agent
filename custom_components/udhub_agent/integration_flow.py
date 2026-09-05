"""Serialize and drive HA Config / Options Flows for cloud-side wizards."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow

_LOGGER = logging.getLogger(__name__)

# Placeholder tokens like {name} / {link_left} in HA translation strings.
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")

# HA does not persist progress description_placeholders across async_get polls.
# Cache auth URLs from the step that first showed them (Xiaomi OAuth etc.).
_FLOW_AUTH_CACHE = "udhub_flow_auth_urls"


def _auth_cache(hass: HomeAssistant) -> dict[str, str]:
    store = hass.data.setdefault(_FLOW_AUTH_CACHE, {})
    if not isinstance(store, dict):
        store = {}
        hass.data[_FLOW_AUTH_CACHE] = store
    return store


def _remember_auth_url(hass: HomeAssistant, flow: dict[str, Any]) -> None:
    flow_id = str(flow.get("flow_id") or "").strip()
    url = str(flow.get("auth_url") or flow.get("url") or "").strip()
    if flow_id and url.startswith(("http://", "https://")):
        _auth_cache(hass)[flow_id] = url


def _restore_auth_url(hass: HomeAssistant, flow: dict[str, Any]) -> None:
    flow_id = str(flow.get("flow_id") or "").strip()
    if not flow_id:
        return
    cached = _auth_cache(hass).get(flow_id)
    if not cached:
        return
    if not flow.get("auth_url"):
        flow["auth_url"] = cached
    if not flow.get("url"):
        flow["url"] = cached


def _forget_auth_url(hass: HomeAssistant, flow_id: str | None) -> None:
    if not flow_id:
        return
    _auth_cache(hass).pop(str(flow_id), None)


def _type_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, FlowResultType):
        return str(value.value)
    text = str(value).strip()
    # HA / Python may stringify missing type as "None"
    if text in ("None", "none", "null"):
        return ""
    return text


# Discovery sources — keep these on agent start; only purge leftover user wizards.
_DISCOVERY_SOURCES = frozenset(
    {
        "bluetooth",
        "dhcp",
        "discovery",
        "esphome",
        "hardware",
        "hassio",
        "homekit",
        "integration_discovery",
        "mqtt",
        "ssdp",
        "unignore",
        "usb",
        "zeroconf",
    }
)
_STALE_USER_SOURCES = frozenset({"user", "reauth", "reconfigure"})


def _schema_custom_serializer(ha_custom: Any, unsupported: Any):
    """Map bare Python types before HA's helper (Xiaomi uses ``bool`` not ``cv.boolean``)."""
    builtin_map = {
        bool: "boolean",
        str: "string",
        int: "integer",
        float: "float",
    }

    def custom(value: Any) -> Any:
        mapped = builtin_map.get(value) if isinstance(value, type) else None
        if mapped is not None:
            return {"type": mapped}
        if ha_custom is not None:
            try:
                return ha_custom(value)
            except Exception:  # noqa: BLE001
                return unsupported
        return unsupported

    return custom


def _manual_serialize_schema(schema: Any) -> list[Any]:
    """Best-effort field list when neither probatio nor voluptuous_serialize is available."""
    import voluptuous as vol

    builtin_map = {
        bool: "boolean",
        str: "string",
        int: "integer",
        float: "float",
    }
    raw = getattr(schema, "schema", schema)
    if not isinstance(raw, dict):
        return []
    out: list[Any] = []
    for key, value in raw.items():
        name = getattr(key, "schema", key)
        if not isinstance(name, str):
            name = str(name)
        required = isinstance(key, vol.Required)
        optional = isinstance(key, vol.Optional)
        field: dict[str, Any] = {
            "name": name,
            "required": required and not optional,
        }
        if optional:
            field["optional"] = True
        default = getattr(key, "default", vol.UNDEFINED)
        if default is not vol.UNDEFINED and not callable(default):
            field["default"] = default
        typ = builtin_map.get(value) if isinstance(value, type) else None
        if typ is None and isinstance(value, type):
            typ = "string"
        field["type"] = typ or "string"
        out.append(field)
    return out


def _serialize_data_schema(schema: Any) -> list[Any]:
    """Serialize voluptuous/probatio schema for the console (HA websocket parity).

    HA 2026.9+ uses ``probatio.to_field_list`` and no longer ships
    ``voluptuous_serialize`` in all installs. Prefer probatio, then the legacy
    package, then a minimal builtin walk.
    """
    ha_custom = None
    try:
        from homeassistant.helpers import config_validation as cv

        ha_custom = cv.custom_serializer
    except Exception:  # noqa: BLE001
        ha_custom = None

    # 1) HA 2026.9+ — probatio (replaces voluptuous_serialize)
    try:
        from probatio import UNSUPPORTED, to_field_list

        converted = to_field_list(
            schema,
            custom_serializer=_schema_custom_serializer(ha_custom, UNSUPPORTED),
        )
        if isinstance(converted, list):
            return converted
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("probatio to_field_list failed: %s", exc)

    # 2) Legacy HA — voluptuous_serialize
    try:
        import voluptuous_serialize
        from voluptuous_serialize import UNSUPPORTED as VS_UNSUPPORTED

        converted = voluptuous_serialize.convert(
            schema,
            custom_serializer=_schema_custom_serializer(
                ha_custom, VS_UNSUPPORTED
            ),
        )
        if isinstance(converted, list):
            return converted
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("voluptuous_serialize.convert failed: %s", exc)

    # 3) Last resort — enough for Xiaomi EULA bool / simple string fields
    return _manual_serialize_schema(schema)


def _apply_placeholders(text: str, placeholders: dict[str, Any] | None) -> str:
    if not text or not placeholders:
        return text

    def _repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in placeholders and placeholders[key] is not None:
            return str(placeholders[key])
        return match.group(0)

    try:
        return _PLACEHOLDER_RE.sub(_repl, text)
    except Exception:  # noqa: BLE001
        return text


_HREF_RE = re.compile(
    r"""href\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _extract_auth_url(
    result: dict[str, Any],
    placeholders: dict[str, Any] | None = None,
) -> str | None:
    """Pull OAuth / external auth URL for the console (Xiaomi uses link_left HTML)."""
    for key in ("url", "external_url", "auth_url"):
        raw = result.get(key)
        if isinstance(raw, str) and raw.strip().startswith(("http://", "https://")):
            return raw.strip()

    ph = placeholders if isinstance(placeholders, dict) else result.get(
        "description_placeholders"
    )
    if not isinstance(ph, dict):
        return None

    # Prefer known keys (xiaomi_home: link_left = '<a href="…">')
    preferred = (
        "link_left",
        "url",
        "auth_url",
        "oauth_url",
        "authorization_url",
    )
    candidates: list[str] = []
    for key in preferred:
        val = ph.get(key)
        if isinstance(val, str) and val.strip():
            candidates.append(val)
    for key, val in ph.items():
        if key in preferred:
            continue
        if isinstance(val, str) and val.strip():
            candidates.append(val)

    for text in candidates:
        m = _HREF_RE.search(text)
        if m:
            return m.group(1).strip()
        m2 = _HTTP_URL_RE.search(text)
        if m2:
            return m2.group(0).rstrip(").,;]")
    return None


def serialize_flow_result(result: dict[str, Any]) -> dict[str, Any]:
    """Make a FlowResult JSON-safe for Agent → cloud."""
    rtype = _type_str(result.get("type"))
    out: dict[str, Any] = {
        "type": rtype,
        "flow_id": result.get("flow_id"),
        "handler": result.get("handler"),
        "step_id": result.get("step_id"),
    }
    for key in (
        "errors",
        "description_placeholders",
        "reason",
        "url",
        "external_url",
        "progress_action",
        "last_step",
        "preview",
    ):
        if key in result and result[key] is not None:
            out[key] = result[key]

    auth_url = _extract_auth_url(result)
    if auth_url:
        out["auth_url"] = auth_url
        # Also mirror onto url so older consoles can open it.
        out.setdefault("url", auth_url)

    schema = result.get("data_schema")
    if schema is not None:
        try:
            out["data_schema"] = _serialize_data_schema(schema)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("data_schema serialize failed: %s", exc)
            out["data_schema"] = []
            out["data_schema_error"] = str(exc)

    if rtype == "create_entry":
        entry = result.get("result")
        if entry is not None and hasattr(entry, "entry_id"):
            out["entry"] = {
                "entry_id": entry.entry_id,
                "domain": entry.domain,
                "title": entry.title,
                "source": getattr(entry, "source", None),
            }
        # Never ship raw entry data (may contain tokens)
        if "title" in result:
            out["title"] = result.get("title")

    if rtype == "abort":
        out["reason"] = result.get("reason")

    if rtype == "menu":
        out["menu_options"] = result.get("menu_options")

    return out


async def _load_flow_translations(
    hass: HomeAssistant,
    domain: str,
    category: str,
) -> dict[str, str]:
    """Load flattened HA translations for a config/options flow domain."""
    try:
        from homeassistant.helpers.translation import async_get_translations

        language = str(getattr(hass.config, "language", None) or "en")
        # Xiaomi etc. ship zh-Hans.json; hass language may be "zh" / "zh-CN".
        candidates: list[str] = [language]
        if language.startswith("zh"):
            for extra in ("zh-Hans", "zh-Hant", "zh"):
                if extra not in candidates:
                    candidates.append(extra)
        if "en" not in candidates:
            candidates.append("en")

        merged: dict[str, str] = {}
        for lang in candidates:
            try:
                part = await async_get_translations(
                    hass,
                    lang,
                    category,
                    integrations={domain},
                )
            except Exception:  # noqa: BLE001
                continue
            if not part:
                continue
            # Prefer earlier candidates (requested language first); only fill gaps.
            for key, value in part.items():
                if key not in merged and value:
                    merged[key] = value
        return merged
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("flow translations load failed for %s: %s", domain, exc)
        return {}


async def enrich_flow_result(
    hass: HomeAssistant,
    result: dict[str, Any],
    *,
    category: str = "config",
) -> dict[str, Any]:
    """Serialize a FlowResult and attach HA-localized strings (HA dialog parity).

    ``localized`` mirrors what HA frontend resolves from strings.json:
    title / description / data labels / errors / abort / progress / menu.
    """
    out = serialize_flow_result(result)
    _restore_auth_url(hass, out)
    domain = str(out.get("handler") or "")
    step_id = str(out.get("step_id") or "")
    rtype = str(out.get("type") or "")
    if not domain:
        _remember_auth_url(hass, out)
        return out

    translations = await _load_flow_translations(hass, domain, category)
    if not translations:
        _remember_auth_url(hass, out)
        return out

    prefix = f"component.{domain}.{category}."
    placeholders = out.get("description_placeholders")
    if not isinstance(placeholders, dict):
        placeholders = {}

    def lookup(*parts: str) -> str | None:
        key = prefix + ".".join(parts)
        raw = translations.get(key)
        if not raw:
            return None
        return _apply_placeholders(str(raw), placeholders)

    localized: dict[str, Any] = {}

    # Dialog / step title (HA priority: step title → flow_title → component title)
    title = (
        (lookup("step", step_id, "title") if step_id else None)
        or lookup("flow_title")
        or translations.get(f"component.{domain}.title")
        or translations.get(f"component.{domain}.{category}")
    )
    if title:
        localized["title"] = _apply_placeholders(str(title), placeholders)

    if step_id:
        description = lookup("step", step_id, "description")
        if description:
            localized["description"] = description

        data_labels: dict[str, str] = {}
        data_descriptions: dict[str, str] = {}
        for field in out.get("data_schema") or []:
            if not isinstance(field, dict):
                continue
            name = str(field.get("name") or "")
            if not name:
                continue
            label = lookup("step", step_id, "data", name)
            if label:
                data_labels[name] = label
            ddesc = lookup("step", step_id, "data_description", name)
            if ddesc:
                data_descriptions[name] = ddesc
        if data_labels:
            localized["data"] = data_labels
        if data_descriptions:
            localized["data_description"] = data_descriptions

        menu_labels: dict[str, str] = {}
        menu = out.get("menu_options")
        menu_keys: list[str] = []
        if isinstance(menu, list):
            menu_keys = [str(x) for x in menu]
        elif isinstance(menu, dict):
            menu_keys = [str(k) for k in menu.keys()]
        for opt in menu_keys:
            label = lookup("step", step_id, "menu_options", opt) or lookup(
                "step", step_id, "menu_option", opt
            )
            if label:
                menu_labels[opt] = label
        if menu_labels:
            localized["menu_options"] = menu_labels

    errors_in = out.get("errors")
    if isinstance(errors_in, dict) and errors_in:
        errors_out: dict[str, str] = {}
        for err_key, err_val in errors_in.items():
            code = str(err_val)
            # HA stores error *codes* in FlowResult.errors values.
            translated = lookup("error", code) or lookup("error", str(err_key))
            errors_out[str(err_key)] = translated or code
        localized["errors"] = errors_out

    if rtype == "abort":
        reason = str(out.get("reason") or "")
        if reason:
            abort_msg = lookup("abort", reason)
            if abort_msg:
                localized["abort"] = abort_msg

    # HA: FlowResultType.SHOW_PROGRESS.value == "progress" (no .PROGRESS member)
    if rtype in ("progress", "show_progress"):
        action = str(out.get("progress_action") or "")
        if action:
            progress_msg = lookup("progress", action)
            if progress_msg:
                localized["progress_action"] = progress_msg

    if rtype == "create_entry":
        create_msg = lookup("create_entry", "default")
        if create_msg:
            localized["create_entry"] = create_msg

    if localized:
        out["localized"] = localized
    _remember_auth_url(hass, out)
    if rtype in ("create_entry", "abort"):
        _forget_auth_url(hass, out.get("flow_id"))
    return out


async def list_handlers(hass: HomeAssistant) -> dict[str, Any]:
    """Full catalog of Config-Flow-capable integrations on this host.

    Mirrors HA's native "Add Integration" catalog: scan builtin
    ``homeassistant/components`` and ``custom_components`` manifests for
    ``config_flow: true``. Registered (loaded) flows are flagged so the
    console can distinguish installed-but-not-loaded brands.
    """

    # 1) Currently registered (loaded) flow handlers
    loaded: set[str] = set()
    try:
        from homeassistant.loader import async_get_config_flows

        flows = await async_get_config_flows(hass)
        loaded = {str(k) for k in (flows.keys() if isinstance(flows, dict) else flows)}
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("async_get_config_flows failed", exc_info=True)

    # 2) Full catalog via manifest scan (blocking I/O → executor)
    def _scan() -> list[dict[str, Any]]:
        import homeassistant.components as comp_pkg

        items: list[dict[str, Any]] = []
        roots: list[tuple[str, Path]] = [("builtin", Path(comp_pkg.__file__).parent)]
        try:
            custom_root = Path(hass.config.config_dir or "") / "custom_components"
            if custom_root.is_dir():
                roots.append(("custom", custom_root))
        except Exception:  # noqa: BLE001
            pass
        seen: set[str] = set()
        for source, root in roots:
            for child in sorted(root.iterdir()):
                if not child.is_dir() or child.name.startswith("_"):
                    continue
                if child.name in seen:
                    continue
                manifest_path = child / "manifest.json"
                if not manifest_path.is_file():
                    continue
                try:
                    manifest = json.loads(manifest_path.read_text())
                except Exception:  # noqa: BLE001
                    continue
                if not manifest.get("config_flow"):
                    continue
                seen.add(child.name)
                items.append(
                    {
                        "domain": child.name,
                        "name": manifest.get("name") or child.name,
                        "source": source,
                        "version": manifest.get("version"),
                        "loaded": child.name in loaded,
                    }
                )
        return items

    try:
        items = await hass.async_add_executor_job(_scan)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("manifest catalog scan failed: %s", exc)
        items = []

    # Fallback: at least registered flows if scan unavailable
    if not items and loaded:
        items = [{"domain": d, "name": d, "source": None, "version": None, "loaded": True} for d in sorted(loaded)]

    items.sort(key=lambda i: (str(i["name"]).lower(), str(i["domain"])))
    domains = [str(i["domain"]) for i in items]
    return {
        "status": "ok",
        "handlers": sorted(loaded),
        "count": len(items),
        "items": items,
        "domains_all": domains,
    }


async def list_descriptions(hass: HomeAssistant) -> dict[str, Any]:
    """Native HA "Add Integration" catalog, 1:1 with integration/descriptions.

    Delegates to ``homeassistant.loader.async_get_integration_descriptions``
    so the structure (brand groupings, supported_by, single_config_entry,
    custom overrides) exactly matches the HA frontend dialog.
    """
    try:
        from homeassistant.loader import async_get_integration_descriptions

        descriptions = await async_get_integration_descriptions(hass)
        loaded = {
            str(d) for d in hass.config.components if isinstance(d, str)
        }
        # Protocol "add device" quick rows need to know which protocol
        # components are loaded (mirrors frontend PROTOCOL_INTEGRATIONS).
        protocol = [d for d in ("bluetooth", "thread", "zha", "zwave_js", "matter") if d in loaded]
        return {
            "status": "ok",
            "descriptions": descriptions,
            "loaded_protocol_components": protocol,
            "loaded_count": len(loaded),
        }
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("integration descriptions failed: %s", exc)
        return {"status": "failed", "error": str(exc)}


async def flow_start(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    domain = payload.get("domain") or payload.get("handler")
    if not domain:
        return {"status": "failed", "error": "missing_domain"}
    source = str(payload.get("source") or SOURCE_USER)
    context: dict[str, Any] = {"source": source}
    entry_id = payload.get("entry_id")
    if entry_id and source in ("reauth", "reconfigure"):
        context["entry_id"] = str(entry_id)
    data = payload.get("data")
    try:
        result = await hass.config_entries.flow.async_init(
            str(domain),
            context=context,
            data=data if isinstance(data, dict) else None,
        )
        return {
            "status": "ok",
            "flow": await enrich_flow_result(hass, result, category="config"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}


async def flow_step(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    flow_id = payload.get("flow_id")
    if not flow_id:
        return {"status": "failed", "error": "missing_flow_id"}
    try:
        if "user_input" in payload:
            result = await hass.config_entries.flow.async_configure(
                str(flow_id), payload.get("user_input")
            )
        else:
            result = await hass.config_entries.flow.async_configure(str(flow_id))
        return {
            "status": "ok",
            "flow": await enrich_flow_result(hass, result, category="config"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}


async def flow_abort(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    """Abort a config or options flow.

    ``channel`` / ``manager``:
      - ``config`` — config_entries.flow only
      - ``options`` — config_entries.options only
      - ``auto`` (default) — try config, then options (modal close / unknown channel)
    """
    flow_id = payload.get("flow_id")
    if not flow_id:
        return {"status": "failed", "error": "missing_flow_id"}
    channel = str(
        payload.get("channel") or payload.get("manager") or "auto"
    ).strip().lower()
    managers: list[tuple[str, Any]] = []
    if channel in ("options", "options_flow"):
        managers = [("options", hass.config_entries.options)]
    elif channel in ("config", "config_flow"):
        managers = [("config", hass.config_entries.flow)]
    else:
        managers = [
            ("config", hass.config_entries.flow),
            ("options", hass.config_entries.options),
        ]
    last_error: str | None = None
    for name, mgr in managers:
        try:
            mgr.async_abort(str(flow_id))
            _forget_auth_url(hass, str(flow_id))
            return {
                "status": "ok",
                "flow_id": flow_id,
                "aborted": True,
                "channel": name,
            }
        except UnknownFlow as exc:
            last_error = str(exc) or "unknown_flow"
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            # Wrong manager often raises KeyError / UnknownFlow subclasses.
            if "unknown" in last_error.lower() or "not found" in last_error.lower():
                continue
            _forget_auth_url(hass, str(flow_id))
            return {"status": "failed", "error": last_error, "channel": name}
    _forget_auth_url(hass, str(flow_id))
    return {
        "status": "failed",
        "error": last_error or "unknown_flow",
        "flow_id": flow_id,
    }


async def options_flow_abort(
    hass: HomeAssistant, payload: dict[str, Any]
) -> dict[str, Any]:
    """Abort an options flow (explicit channel)."""
    return await flow_abort(hass, {**payload, "channel": "options"})


async def ignore_flow(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    """Ignore a discovery flow — mirrors HA ``config_entries/ignore_flow``."""
    flow_id = payload.get("flow_id")
    if not flow_id:
        return {"status": "failed", "error": "missing_flow_id"}
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        title = "Ignored"
    else:
        title = title.strip()
    try:
        flow = next(
            (
                flw
                for flw in hass.config_entries.flow.async_progress()
                if flw.get("flow_id") == str(flow_id)
            ),
            None,
        )
        if flow is None:
            return {"status": "failed", "error": "unknown_flow", "flow_id": flow_id}
        ctx = flow.get("context") or {}
        unique_id = ctx.get("unique_id")
        if unique_id is None:
            return {
                "status": "failed",
                "error": "no_unique_id",
                "hint": "Specified flow has no unique ID (cannot ignore)",
            }
        from homeassistant.config_entries import SOURCE_IGNORE

        context: dict[str, Any] = {"source": SOURCE_IGNORE}
        if "discovery_key" in ctx:
            context["discovery_key"] = ctx["discovery_key"]
        result = await hass.config_entries.flow.async_init(
            flow["handler"],
            context=context,
            data={"unique_id": unique_id, "title": title},
        )
        return {
            "status": "ok",
            "flow_id": flow_id,
            "ignored": True,
            "handler": flow.get("handler"),
            "title": title,
            "result_type": _type_str(result.get("type") if isinstance(result, dict) else None),
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}


async def flow_cleanup(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    """Abort leftover in-progress config flows (non-discovery by default).

    Used on Agent start and from the console to clear OAuth / user wizards
    abandoned after remote SaaS sessions. Discovery flows are preserved unless
    ``include_discovery`` is true.
    """
    include_discovery = bool(payload.get("include_discovery"))
    sources_filter = payload.get("sources")
    allowed: set[str] | None = None
    if isinstance(sources_filter, list) and sources_filter:
        allowed = {str(s) for s in sources_filter}
    dry_run = bool(payload.get("dry_run"))
    aborted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    try:
        progress = list(hass.config_entries.flow.async_progress())
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}
    for row in progress:
        if not isinstance(row, dict):
            continue
        fid = row.get("flow_id")
        if not fid:
            continue
        src = str((row.get("context") or {}).get("source") or "")
        if allowed is not None:
            if src not in allowed:
                skipped.append(
                    {"flow_id": fid, "source": src, "reason": "source_filter"}
                )
                continue
        else:
            # Default: only user/reauth/reconfigure leftovers from SaaS wizards.
            if src not in _STALE_USER_SOURCES:
                skipped.append({"flow_id": fid, "source": src, "reason": "keep"})
                continue
        if not include_discovery and src in _DISCOVERY_SOURCES:
            skipped.append({"flow_id": fid, "source": src, "reason": "discovery"})
            continue
        if dry_run:
            aborted.append(
                {
                    "flow_id": fid,
                    "handler": row.get("handler"),
                    "source": src,
                    "step_id": row.get("step_id"),
                    "dry_run": True,
                }
            )
            continue
        try:
            hass.config_entries.flow.async_abort(str(fid))
            _forget_auth_url(hass, str(fid))
            aborted.append(
                {
                    "flow_id": fid,
                    "handler": row.get("handler"),
                    "source": src,
                    "step_id": row.get("step_id"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            skipped.append(
                {"flow_id": fid, "source": src, "reason": str(exc)}
            )
    return {
        "status": "ok",
        "aborted": aborted,
        "aborted_count": len(aborted),
        "skipped": skipped,
        "dry_run": dry_run,
    }


async def flow_get(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    """Fetch the current state of a flow without advancing it.

    Mirrors HA native progress polling (GET /api/config/config_entries/flow/{id}).
    During SHOW_PROGRESS (e.g. Xiaomi OAuth), ``async_get`` often omits ``type``
    and ``description_placeholders``; merge the matching ``async_progress`` row
    so the console keeps auth_url and can detect completion.
    """
    flow_id = payload.get("flow_id")
    if not flow_id:
        return {"status": "failed", "error": "missing_flow_id"}
    try:
        state = hass.config_entries.flow.async_get(str(flow_id))
    except (KeyError, UnknownFlow) as exc:
        return {"status": "failed", "error": "unknown_flow", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}
    if not isinstance(state, dict):
        return {"status": "failed", "error": "unexpected_flow_state"}

    merged: dict[str, Any] = dict(state)
    rtype = _type_str(merged.get("type"))
    need_progress_merge = (
        not rtype
        or rtype in ("none", "None")
        or rtype in ("progress", "show_progress")
        or not merged.get("description_placeholders")
    )
    if need_progress_merge:
        try:
            for row in hass.config_entries.flow.async_progress():
                if not isinstance(row, dict):
                    continue
                if str(row.get("flow_id") or "") != str(flow_id):
                    continue
                # Prefer progress snapshot fields when async_get is sparse.
                for key, val in row.items():
                    if key == "flow_id":
                        continue
                    if merged.get(key) in (None, "", "None"):
                        merged[key] = val
                    elif key == "description_placeholders" and not merged.get(key):
                        merged[key] = val
                if not merged.get("type") and row.get("step_id"):
                    # Still in-flight without explicit type → treat as progress.
                    merged["type"] = "progress"
                break
        except Exception:  # noqa: BLE001
            _LOGGER.debug("flow_get progress merge failed", exc_info=True)

    if not merged.get("type") and merged.get("step_id"):
        merged["type"] = "progress"

    return {
        "status": "ok",
        "flow": await enrich_flow_result(hass, merged, category="config"),
    }


async def flow_progress(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    flow_id = payload.get("flow_id")
    try:
        progress = list(hass.config_entries.flow.async_progress())
        if flow_id:
            progress = [p for p in progress if p.get("flow_id") == flow_id]
        items = []
        for p in progress:
            # Full state so the console can render progress + OAuth links
            # (description_placeholders) like HA's native dialog.
            state = {}
            try:
                if isinstance(p, dict):
                    state = await enrich_flow_result(
                        hass, dict(p), category="config"
                    )
            except Exception:  # noqa: BLE001
                state = {}
            if isinstance(state, dict) and not state.get("type") and p.get("step_id"):
                state["type"] = "progress"
            items.append(
                {
                    "flow_id": p.get("flow_id"),
                    "handler": p.get("handler"),
                    "step_id": p.get("step_id"),
                    "context": {
                        k: v
                        for k, v in (p.get("context") or {}).items()
                        if k
                        in (
                            "source",
                            "entry_id",
                            "title_placeholders",
                            "unique_id",
                            "discovery_key",
                        )
                    },
                    "state": state,
                }
            )
        return {"status": "ok", "progress": items}
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}


async def options_flow_start(
    hass: HomeAssistant, payload: dict[str, Any]
) -> dict[str, Any]:
    entry_id = payload.get("entry_id")
    if not entry_id:
        return {"status": "failed", "error": "missing_entry_id"}
    try:
        result = await hass.config_entries.options.async_init(str(entry_id))
        return {
            "status": "ok",
            "flow": await enrich_flow_result(hass, result, category="options"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}


async def options_flow_step(
    hass: HomeAssistant, payload: dict[str, Any]
) -> dict[str, Any]:
    flow_id = payload.get("flow_id")
    if not flow_id:
        return {"status": "failed", "error": "missing_flow_id"}
    try:
        if "user_input" in payload:
            result = await hass.config_entries.options.async_configure(
                str(flow_id), payload.get("user_input")
            )
        else:
            result = await hass.config_entries.options.async_configure(str(flow_id))
        return {
            "status": "ok",
            "flow": await enrich_flow_result(hass, result, category="options"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}
