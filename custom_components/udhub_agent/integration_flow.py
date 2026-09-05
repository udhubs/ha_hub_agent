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


def _type_str(value: Any) -> str:
    if isinstance(value, FlowResultType):
        return str(value.value)
    return str(value)


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
    domain = str(out.get("handler") or "")
    step_id = str(out.get("step_id") or "")
    rtype = str(out.get("type") or "")
    if not domain:
        return out

    translations = await _load_flow_translations(hass, domain, category)
    if not translations:
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
    flow_id = payload.get("flow_id")
    if not flow_id:
        return {"status": "failed", "error": "missing_flow_id"}
    try:
        hass.config_entries.flow.async_abort(str(flow_id))
        return {"status": "ok", "flow_id": flow_id, "aborted": True}
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}


async def flow_get(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    """Fetch the current state of a flow without advancing it.

    Mirrors HA native progress polling (GET /api/config/config_entries/flow/{id}).
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
    return {
        "status": "ok",
        "flow": await enrich_flow_result(hass, dict(state), category="config"),
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
            items.append(
                {
                    "flow_id": p.get("flow_id"),
                    "handler": p.get("handler"),
                    "step_id": p.get("step_id"),
                    "context": {
                        k: v
                        for k, v in (p.get("context") or {}).items()
                        if k in ("source", "entry_id", "title_placeholders")
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
