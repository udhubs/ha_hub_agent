"""Matter RPC proxy (doc 19 §6.2 / S10).

python-matter-server WS framing: { message_id, command, args }
— not JSON-RPC 2.0. Drain ServerInfoMessage on connect before matching results.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

DEFAULT_WS_URL = "ws://127.0.0.1:5580/ws"
DEFAULT_HTTP_URL = "http://127.0.0.1:5580/"

# Basic Information cluster attribute paths (endpoint/cluster/attr)
_ATTR_PRODUCT_NAME = "0/40/1"
_ATTR_SOFTWARE_VERSION = "0/40/9"
_ATTR_SOFTWARE_VERSION_STRING = "0/40/10"


async def dispatch(hass: HomeAssistant, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action == "probe":
        return await _probe(hass, payload)
    if action == "rpc":
        return await _rpc(hass, payload)
    if action in ("nodes", "get_nodes"):
        raw = await _rpc(
            hass,
            {**payload, "method": payload.get("method") or "get_nodes"},
        )
        return _normalize_nodes_response(raw)
    if action in ("commission", "commission_with_code"):
        code = str(payload.get("code") or payload.get("pairing_code") or "").strip()
        if not code:
            return {"status": "failed", "error": "pairing_code_required"}
        return await _rpc(
            hass,
            {
                **payload,
                "method": "commission_with_code",
                "params": {
                    "code": code,
                    "network_only": bool(payload.get("network_only", False)),
                },
            },
        )
    if action in ("open_commissioning_window", "commissioning_window"):
        node_id = payload.get("node_id")
        if node_id is None:
            return {"status": "failed", "error": "node_id_required"}
        return await _rpc(
            hass,
            {
                **payload,
                "method": "open_commissioning_window",
                "params": {"node_id": int(node_id)},
            },
        )
    if action in ("check_node_update", "node_update_check"):
        node_id = payload.get("node_id")
        if node_id is None:
            return {"status": "failed", "error": "node_id_required"}
        return await _rpc(
            hass,
            {
                **payload,
                "method": "check_node_update",
                "params": {"node_id": int(node_id)},
            },
        )
    if action in ("set_wifi_credentials", "wifi_credentials"):
        ssid = str(payload.get("ssid") or "").strip()
        credentials = str(
            payload.get("credentials") or payload.get("password") or ""
        )
        if not ssid or not credentials:
            return {"status": "failed", "error": "ssid_and_credentials_required"}
        # Wi‑Fi secret stays in Matter server memory only — never returned to SaaS
        return await _rpc(
            hass,
            {
                **payload,
                "method": "set_wifi_credentials",
                "params": {"ssid": ssid, "credentials": credentials},
            },
        )
    return {"status": "failed", "error": "unsupported_action", "action": action}


async def _probe(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    ws_url = str(payload.get("ws_url") or DEFAULT_WS_URL)
    base = str(payload.get("base_url") or DEFAULT_HTTP_URL).rstrip("/") + "/"
    session = async_get_clientsession(hass)
    ws_ok = False
    ws_error: str | None = None
    server_info: dict[str, Any] | None = None
    try:
        async with session.ws_connect(ws_url, heartbeat=20, timeout=5) as ws:
            # First message is typically ServerInfoMessage
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=3)
                if msg.type.name == "TEXT":
                    data = json.loads(msg.data)
                    if isinstance(data, dict) and "fabric_id" in data:
                        server_info = {
                            "fabric_id": data.get("fabric_id"),
                            "schema_version": data.get("schema_version"),
                            "sdk_version": data.get("sdk_version"),
                            "wifi_credentials_set": data.get("wifi_credentials_set"),
                            "thread_credentials_set": data.get(
                                "thread_credentials_set"
                            ),
                            "bluetooth_enabled": data.get("bluetooth_enabled"),
                        }
                        ws_ok = True
                    else:
                        ws_ok = True
                else:
                    ws_ok = True
            except (asyncio.TimeoutError, json.JSONDecodeError):
                ws_ok = True  # connected even if no info yet
            await ws.close()
    except Exception as exc:  # noqa: BLE001
        ws_error = str(exc)

    http_status = None
    http_preview = None
    try:
        async with session.get(base, timeout=5) as resp:
            http_status = resp.status
            http_preview = (await resp.text())[:200]
    except Exception as exc:  # noqa: BLE001
        if not ws_error:
            ws_error = str(exc)

    return {
        "status": "ok",
        "reachable": ws_ok or (http_status is not None and http_status < 500),
        "ws_url": ws_url,
        "ws_ok": ws_ok,
        "http_status": http_status,
        "base_url": base,
        "body_preview": http_preview,
        "server_info": server_info,
        "error": None if ws_ok else ws_error,
        "note": "Matter WS probe（message_id/command 协议）",
    }


async def _rpc(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    method = str(
        payload.get("method") or payload.get("command") or ""
    ).strip()
    if not method:
        return {"status": "failed", "error": "method_required"}
    params = payload.get("params")
    if params is None:
        params = payload.get("args")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        params = {}
    ws_url = str(payload.get("ws_url") or DEFAULT_WS_URL)
    session = async_get_clientsession(hass)
    message_id = str(payload.get("message_id") or payload.get("id") or uuid.uuid4().hex[:12])
    body: dict[str, Any] = {
        "message_id": message_id,
        "command": method,
    }
    if params:
        body["args"] = params

    try:
        async with session.ws_connect(ws_url, heartbeat=20, timeout=15) as ws:
            # Drain ServerInfo / events until matching message_id
            deadline = asyncio.get_event_loop().time() + 20
            await ws.send_str(json.dumps(body))
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    return {
                        "status": "failed",
                        "error": "ws_timeout",
                        "method": method,
                        "ws_url": ws_url,
                    }
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
                except asyncio.TimeoutError:
                    return {
                        "status": "failed",
                        "error": "ws_timeout",
                        "method": method,
                        "ws_url": ws_url,
                    }
                if msg.type.name != "TEXT":
                    if msg.type.name == "ERROR":
                        return {
                            "status": "failed",
                            "error": f"ws_error:{msg.data}",
                            "method": method,
                        }
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                # ServerInfo on connect — skip
                if "fabric_id" in data and "message_id" not in data:
                    continue
                # Events — skip
                if "event" in data and "message_id" not in data:
                    continue
                mid = data.get("message_id")
                if mid is not None and str(mid) != message_id:
                    continue
                if "error_code" in data:
                    return {
                        "status": "failed",
                        "error": f"matter_error:{data.get('error_code')}",
                        "details": data.get("details"),
                        "method": method,
                        "ws_url": ws_url,
                    }
                result = data.get("result", data)
                return {
                    "status": "ok",
                    "method": method,
                    "ws_url": ws_url,
                    "result": result,
                    "message_id": message_id,
                    "note": "Matter WS command/args",
                }
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("matter ws rpc failed: %s", exc)
        return {
            "status": "failed",
            "error": str(exc),
            "method": method,
            "ws_url": ws_url,
            "note": "无法连接 Matter server（需 python-matter-server :5580）",
        }


def _attr_from_node(node: dict[str, Any], path: str) -> Any:
    attrs = node.get("attributes") or node.get("attribute_values") or {}
    if isinstance(attrs, dict):
        if path in attrs:
            return attrs[path]
        # sometimes nested by endpoint
        for k, v in attrs.items():
            if str(k).endswith(path.split("/", 1)[-1]) and path in str(k):
                return v
    return None


def _normalize_node(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"raw": str(raw)[:200]}
    node_id = raw.get("node_id") if "node_id" in raw else raw.get("nodeId")
    if node_id is None:
        node_id = raw.get("id")
    name = (
        _attr_from_node(raw, _ATTR_PRODUCT_NAME)
        or raw.get("name")
        or raw.get("node_name")
    )
    sw = _attr_from_node(raw, _ATTR_SOFTWARE_VERSION_STRING) or _attr_from_node(
        raw, _ATTR_SOFTWARE_VERSION
    )
    return {
        "node_id": node_id,
        "name": str(name) if name is not None else None,
        "available": raw.get("available"),
        "is_bridge": raw.get("is_bridge") or raw.get("isBridge"),
        "software_version": str(sw) if sw is not None else None,
        "date_commissioned": raw.get("date_commissioned")
        or raw.get("dateCommissioned"),
    }


def _normalize_nodes_response(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("status") != "ok":
        return raw
    result = raw.get("result")
    nodes_raw: list[Any] = []
    if isinstance(result, list):
        nodes_raw = result
    elif isinstance(result, dict):
        if isinstance(result.get("nodes"), list):
            nodes_raw = result["nodes"]
        elif isinstance(result.get("result"), list):
            nodes_raw = result["result"]
    nodes = [_normalize_node(n) for n in nodes_raw]
    return {
        **raw,
        "nodes": nodes,
        "node_count": len(nodes),
        "note": raw.get("note") or "nodes normalized",
    }
