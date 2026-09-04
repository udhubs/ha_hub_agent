"""Query / template / event surfaces for Agent catalog."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def services_list(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    domain_filter = payload.get("domain")
    services = hass.services.async_services()
    out: dict[str, Any] = {}
    for domain, svcs in services.items():
        if domain_filter and domain != domain_filter:
            continue
        out[domain] = sorted(svcs.keys())
    return {"status": "ok", "services": out}


async def states_get(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    entity_id = payload.get("entity_id")
    domain = payload.get("domain")
    if entity_id:
        st = hass.states.get(str(entity_id))
        if not st:
            return {"status": "failed", "error": "entity_not_found"}
        return {
            "status": "ok",
            "state": {
                "entity_id": st.entity_id,
                "state": st.state,
                "attributes": dict(st.attributes),
                "last_changed": st.last_changed.isoformat() if st.last_changed else None,
                "last_updated": st.last_updated.isoformat() if st.last_updated else None,
            },
        }
    states = hass.states.async_all(str(domain) if domain else None)
    items = []
    for st in states[:500]:
        items.append(
            {
                "entity_id": st.entity_id,
                "state": st.state,
                "attributes": {
                    k: st.attributes[k]
                    for k in list(st.attributes)[:40]
                },
            }
        )
    return {"status": "ok", "states": items, "truncated": len(states) > 500}


async def history_get(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    entity_ids = payload.get("entity_ids") or payload.get("entity_id")
    if isinstance(entity_ids, str):
        entity_ids = [entity_ids]
    if not isinstance(entity_ids, list) or not entity_ids:
        return {"status": "failed", "error": "missing_entity_ids"}
    hours = float(payload.get("hours") or 6)
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=max(0.1, min(hours, 168)))
    try:
        from homeassistant.components import history as hist

        raw = await hass.async_add_executor_job(
            hist.get_significant_states,
            hass,
            start,
            end,
            [str(e) for e in entity_ids],
        )
        # Serialize states
        out: dict[str, list[dict[str, Any]]] = {}
        for eid, rows in (raw or {}).items():
            out[eid] = []
            for st in rows[-200:]:
                out[eid].append(
                    {
                        "state": st.state,
                        "last_changed": st.last_changed.isoformat()
                        if getattr(st, "last_changed", None)
                        else None,
                        "attributes": dict(getattr(st, "attributes", {}) or {}),
                    }
                )
        return {"status": "ok", "history": out, "start": start.isoformat(), "end": end.isoformat()}
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("history failed", exc_info=True)
        return {"status": "failed", "error": str(exc)}


async def logbook_get(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    hours = float(payload.get("hours") or 6)
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=max(0.1, min(hours, 168)))
    entity_id = payload.get("entity_id")
    try:
        from homeassistant.components import logbook

        entries = await hass.async_add_executor_job(
            logbook.get_significant_events,
            hass,
            start,
            end,
            [str(entity_id)] if entity_id else None,
        )
        items = []
        for e in (entries or [])[:200]:
            if isinstance(e, dict):
                items.append(e)
            else:
                items.append({"repr": str(e)[:200]})
        return {"status": "ok", "logbook": items}
    except Exception as exc:  # noqa: BLE001
        # Fallback: empty ok with detail
        return {"status": "failed", "error": str(exc)}


async def statistics_get(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    statistic_ids = payload.get("statistic_ids") or payload.get("entity_ids") or []
    if isinstance(statistic_ids, str):
        statistic_ids = [statistic_ids]
    if not statistic_ids:
        return {"status": "failed", "error": "missing_statistic_ids"}
    hours = float(payload.get("hours") or 24)
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=max(0.1, min(hours, 720)))
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period

        instance = get_instance(hass)
        result = await instance.async_add_executor_job(
            statistics_during_period,
            hass,
            start,
            end,
            set(str(x) for x in statistic_ids),
            "hour",
            None,
            {"mean", "sum", "min", "max"},
        )
        return {
            "status": "ok",
            "statistics": result or {},
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}


async def energy_get(hass: HomeAssistant, _payload: dict[str, Any]) -> dict[str, Any]:
    try:
        data = hass.data.get("energy")
        if data is None:
            return {"status": "ok", "supported": False, "detail": "energy_not_configured"}
        prefs = getattr(data, "manager", None) or data
        return {"status": "ok", "supported": True, "energy": str(type(prefs).__name__)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}


async def template_render(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    template = payload.get("template") or payload.get("value")
    if not template:
        return {"status": "failed", "error": "missing_template"}
    try:
        from homeassistant.helpers import template as tpl

        t = tpl.Template(str(template), hass)
        rendered = t.async_render(payload.get("variables") or {})
        return {"status": "ok", "result": rendered}
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}


async def event_fire(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    event_type = str(payload.get("event_type") or payload.get("type") or "").strip()
    if not event_type:
        return {"status": "failed", "error": "missing_event_type"}
    # Block sensitive event families
    blocked = ("homeassistant_", "call_service", "component_loaded", "auth_")
    low = event_type.lower()
    if any(low.startswith(b) for b in blocked) and not low.startswith("udhub_"):
        if low not in ("udhub_test",):
            # allow custom / user events; still block core auth
            if "auth" in low or low.startswith("homeassistant."):
                return {"status": "denied", "error": "event_type_not_allowed"}
    data = payload.get("event_data") or payload.get("data") or {}
    if not isinstance(data, dict):
        return {"status": "failed", "error": "event_data_must_be_object"}
    if len(str(data)) > 20_000:
        return {"status": "denied", "error": "event_data_too_large"}
    try:
        hass.bus.async_fire(event_type, data)
        return {"status": "ok", "event_type": event_type}
    except Exception as exc:  # noqa: BLE001
        return {"status": "failed", "error": str(exc)}


async def dispatch_query(hass: HomeAssistant, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action == "services_list":
        return await services_list(hass, payload)
    if action == "states_get":
        return await states_get(hass, payload)
    if action == "history":
        return await history_get(hass, payload)
    if action == "logbook":
        return await logbook_get(hass, payload)
    if action == "statistics":
        return await statistics_get(hass, payload)
    if action == "energy_get":
        return await energy_get(hass, payload)
    return {"status": "failed", "error": "unsupported_action", "action": action}
