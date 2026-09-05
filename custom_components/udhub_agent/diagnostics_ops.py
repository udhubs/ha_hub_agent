"""Agent diagnostic collectors (doc 19 §4.5 / S9).

Brand tree: Xiaomi/Aqara cloud reachability + config entry state.
Protocol tree: ZHA / Matter / MQTT coordinator-ish health.
Link probe: layered heartbeat → gateway availability → offline sample.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

_XIAOMI_DOMAINS = (
    "xiaomi_miot",
    "xiaomi_home",
    "xiaomi_aqara",
    "xiaomi_gateway",
    "aqara",
    "mijia",
)


async def dispatch(hass: HomeAssistant, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action == "brand_probe":
        return await brand_probe(hass, payload)
    if action == "protocol_probe":
        return await protocol_probe(hass, payload)
    if action in ("link_probe", "full"):
        return await link_probe(hass, payload)
    if action in ("post_upgrade", "post_upgrade_health"):
        return await post_upgrade_health(hass, payload)
    return {"status": "failed", "error": "unsupported_action", "action": action}


async def brand_probe(hass: HomeAssistant, _payload: dict[str, Any]) -> dict[str, Any]:
    entries = list(hass.config_entries.async_entries())
    xiaomi_entries = [
        e
        for e in entries
        if str(e.domain).lower() in _XIAOMI_DOMAINS
        or "xiaomi" in str(e.domain).lower()
        or "aqara" in str(e.domain).lower()
        or "mijia" in str(e.domain).lower()
    ]
    nodes: list[dict[str, Any]] = []
    for e in xiaomi_entries:
        state = getattr(e, "state", None)
        state_s = str(getattr(state, "value", state) or "unknown")
        title = getattr(e, "title", None) or e.domain
        ok = state_s in ("loaded", "setup_in_progress")
        data = getattr(e, "data", None) or {}
        # Heuristic: cloud token / auth fields without printing secrets
        all_keys = sorted(str(k) for k in data.keys())
        token_keys = [
            k
            for k in all_keys
            if any(
                x in k.lower()
                for x in ("token", "auth", "cookie", "session", "password", "key")
            )
        ]
        has_cred = len(token_keys) > 0
        reason = getattr(e, "reason", None)
        reason_s = str(reason) if reason else None
        nodes.append(
            {
                "id": e.entry_id,
                "domain": e.domain,
                "title": title,
                "entry_state": state_s,
                "reason": reason_s,
                "has_credential_fields": has_cred,
                "credential_field_count": len(token_keys),
                "data_keys": all_keys[:40],
                "status": "ok" if ok and (has_cred or state_s == "loaded") else "degraded",
                "hint": (
                    "配置条目正常"
                    if ok and not reason_s
                    else (
                        f"条目异常：{reason_s}"
                        if reason_s
                        else "检查米家账号 / OAuth token / 云可达性（需在集成设置中处理）"
                    )
                ),
            }
        )

    # Entity availability sample for brand domains
    offline_sample: list[str] = []
    brand_entity_total = 0
    brand_unavailable = 0
    for state in hass.states.async_all():
        eid = state.entity_id
        domain = eid.split(".", 1)[0]
        attrs = state.attributes or {}
        blob = f"{eid} {attrs.get('attribution', '')} {attrs.get('device_class', '')}".lower()
        if not any(k in blob for k in ("xiaomi", "aqara", "mijia")) and domain not in (
            "vacuum",
            "remote",
        ):
            continue
        brand_entity_total += 1
        if state.state in ("unavailable", "unknown"):
            brand_unavailable += 1
            if len(offline_sample) < 8:
                offline_sample.append(eid)

    # OAuth redirect base (public URL only) + cloud HEAD (no auth)
    oauth_redirect_base = "http://homeassistant.local:8123"
    try:
        internal = getattr(hass.config, "internal_url", None)
        if internal:
            oauth_redirect_base = str(internal).rstrip("/")
    except Exception:  # noqa: BLE001
        pass

    cloud_http: dict[str, Any] | None = None
    try:
        session = async_get_clientsession(hass)
        # Public Mi account portal — HEAD only, no credentials
        probe_url = "https://account.xiaomi.com/"
        async with session.head(probe_url, timeout=5, allow_redirects=True) as resp:
            cloud_http = {
                "url": probe_url,
                "ok": resp.status < 500,
                "status": resp.status,
            }
    except Exception as exc:  # noqa: BLE001
        cloud_http = {
            "url": "https://account.xiaomi.com/",
            "ok": False,
            "error": str(exc)[:120],
        }

    notes = []
    if xiaomi_entries:
        notes.append(f"检测到米家/Aqara 类配置条目 {len(xiaomi_entries)} 个")
    else:
        notes.append("未检测到米家/Aqara 配置条目")
    if cloud_http:
        notes.append(
            "米家云可达"
            if cloud_http.get("ok")
            else f"米家云探测失败：{cloud_http.get('error') or cloud_http.get('status')}"
        )
    notes.append(f"OAuth 回调基址提示：{oauth_redirect_base}")

    return {
        "status": "ok",
        "tree": "brand",
        "present": bool(xiaomi_entries),
        "entries": nodes,
        "oauth_redirect_base": oauth_redirect_base,
        "cloud_http": cloud_http,
        "entities": {
            "sampled_hint": brand_entity_total,
            "unavailable": brand_unavailable,
            "sample": offline_sample,
        },
        "notes": notes,
    }


async def protocol_probe(hass: HomeAssistant, _payload: dict[str, Any]) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []

    # ZHA
    zha_entries = list(hass.config_entries.async_entries("zha"))
    zha_ok = False
    zha_detail: dict[str, Any] = {"entries": len(zha_entries)}
    if zha_entries:
        try:
            zha_data = hass.data.get("zha")
            zha_detail["hass_data_present"] = zha_data is not None
            device_count = 0
            offline_count = 0
            lqi_samples: list[dict[str, Any]] = []
            # Prefer device registry + zha identifiers
            from homeassistant.helpers import device_registry as dr

            reg = dr.async_get(hass)
            for device in reg.devices.values():
                idents = getattr(device, "identifiers", None) or set()
                if not any(i and i[0] == "zha" for i in idents):
                    continue
                device_count += 1
                # availability via linked entities
                from homeassistant.helpers import entity_registry as er

                ent_reg = er.async_get(hass)
                ents = [
                    e
                    for e in ent_reg.entities.values()
                    if getattr(e, "device_id", None) == device.id
                ]
                unavailable = 0
                for e in ents[:8]:
                    st = hass.states.get(e.entity_id)
                    if st and st.state in ("unavailable", "unknown"):
                        unavailable += 1
                if ents and unavailable == len(ents[:8]):
                    offline_count += 1
                if len(lqi_samples) < 5:
                    lqi_samples.append(
                        {
                            "name": getattr(device, "name_by_user", None)
                            or getattr(device, "name", None)
                            or device.id,
                            "entities": len(ents),
                            "unavailable_sample": unavailable,
                        }
                    )
            zha_detail["device_count"] = device_count
            zha_detail["offline_estimate"] = offline_count
            zha_detail["devices_sample"] = lqi_samples

            # Soft LQI / RSSI / neighbor sample via zigpy (best-effort)
            lqi_rows: list[dict[str, Any]] = []
            neighbor_hint = 0
            try:
                zha_gateway = None
                if isinstance(zha_data, dict):
                    zha_gateway = (
                        zha_data.get("gateway")
                        or zha_data.get("zha_gateway")
                        or next(
                            (
                                v
                                for v in zha_data.values()
                                if getattr(v, "devices", None) is not None
                            ),
                            None,
                        )
                    )
                else:
                    zha_gateway = getattr(zha_data, "gateway", None) or zha_data
                devices_map = getattr(zha_gateway, "devices", None) or {}
                if hasattr(devices_map, "values"):
                    zdevs = list(devices_map.values())[:40]
                elif isinstance(devices_map, (list, tuple)):
                    zdevs = list(devices_map)[:40]
                else:
                    zdevs = []
                for zdev in zdevs:
                    zig = getattr(zdev, "device", None) or zdev
                    ieee = str(getattr(zig, "ieee", None) or getattr(zdev, "ieee", "") or "")
                    lqi = getattr(zig, "lqi", None)
                    rssi = getattr(zig, "rssi", None)
                    nwk = getattr(zig, "nwk", None)
                    neighbors = getattr(zig, "neighbors", None)
                    if neighbors:
                        try:
                            neighbor_hint += len(list(neighbors))
                        except Exception:  # noqa: BLE001
                            neighbor_hint += 1
                    if lqi is None and rssi is None and not ieee:
                        continue
                    if len(lqi_rows) < 12:
                        lqi_rows.append(
                            {
                                "ieee": ieee[:24] if ieee else None,
                                "nwk": int(nwk) if nwk is not None else None,
                                "lqi": int(lqi) if lqi is not None else None,
                                "rssi": int(rssi) if rssi is not None else None,
                            }
                        )
                zha_detail["lqi_sample"] = lqi_rows
                zha_detail["neighbor_edge_estimate"] = neighbor_hint
                if lqi_rows:
                    weak = sum(
                        1
                        for r in lqi_rows
                        if r.get("lqi") is not None and int(r["lqi"]) < 80
                    )
                    zha_detail["weak_lqi_count"] = weak
            except Exception as exc:  # noqa: BLE001
                zha_detail["lqi_probe_error"] = str(exc)[:120]

            zha_ok = device_count > 0 or any(
                str(getattr(getattr(e, "state", None), "value", e.state) or "")
                == "loaded"
                for e in zha_entries
            )
            if offline_count and device_count and offline_count / max(device_count, 1) > 0.3:
                zha_ok = False
                zha_detail.setdefault(
                    "hint", "离线设备占比偏高，检查协调器/USB/信道"
                )
            weak_n = zha_detail.get("weak_lqi_count")
            lqi_n = len(zha_detail.get("lqi_sample") or [])
            if (
                isinstance(weak_n, int)
                and lqi_n
                and weak_n >= max(3, lqi_n // 3)
            ):
                zha_ok = False
                zha_detail["hint"] = "多设备 LQI 偏低，检查信道干扰/距离"
        except Exception as exc:  # noqa: BLE001
            zha_detail["error"] = str(exc)
            zha_ok = any(
                str(getattr(getattr(e, "state", None), "value", e.state) or "")
                == "loaded"
                for e in zha_entries
            )
    nodes.append(
        {
            "id": "proto_zha",
            "label": "Zigbee (ZHA)",
            "status": "present" if zha_entries else "absent",
            "healthy": zha_ok if zha_entries else None,
            "detail": zha_detail,
            "hint": "USB 协调器 / 网络形成 / 设备配对" if zha_entries else "未安装 ZHA",
        }
    )

    # Matter
    matter_entries = list(hass.config_entries.async_entries("matter"))
    matter_reachable = False
    matter_detail: dict[str, Any] = {"entries": len(matter_entries)}
    if matter_entries:
        try:
            from . import matter_rpc

            probe = await matter_rpc.dispatch(hass, "probe", {})
            matter_reachable = bool(probe.get("reachable"))
            matter_detail["probe"] = {
                "reachable": matter_reachable,
                "error": probe.get("error"),
            }
        except Exception as exc:  # noqa: BLE001
            matter_detail["error"] = str(exc)
    nodes.append(
        {
            "id": "proto_matter",
            "label": "Matter",
            "status": "present" if matter_entries else "absent",
            "healthy": matter_reachable if matter_entries else None,
            "detail": matter_detail,
            "hint": "python-matter-server (:5580) 与 Thread/Wi‑Fi 边界",
        }
    )

    # MQTT
    mqtt_entries = list(hass.config_entries.async_entries("mqtt"))
    mqtt_ok = False
    if mqtt_entries:
        mqtt_ok = any(
            str(getattr(getattr(e, "state", None), "value", e.state) or "") == "loaded"
            for e in mqtt_entries
        )
    nodes.append(
        {
            "id": "proto_mqtt",
            "label": "MQTT",
            "status": "present" if mqtt_entries else "absent",
            "healthy": mqtt_ok if mqtt_entries else None,
            "detail": {"entries": len(mqtt_entries)},
            "hint": "broker 连通与 discovery",
        }
    )

    return {
        "status": "ok",
        "tree": "protocol",
        "nodes": nodes,
        "notes": [
            n["label"] + (" 已检出" if n["status"] == "present" else " 未检出")
            for n in nodes
        ],
    }


async def link_probe(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    brand = await brand_probe(hass, payload)
    protocol = await protocol_probe(hass, payload)

    unavailable = 0
    total = 0
    sample: list[str] = []
    for state in hass.states.async_all():
        if state.entity_id.startswith(("sensor.", "binary_sensor.", "light.", "switch.", "climate.")):
            total += 1
            if state.state in ("unavailable", "unknown"):
                unavailable += 1
                if len(sample) < 10:
                    sample.append(state.entity_id)

    return {
        "status": "ok",
        "brand": brand,
        "protocol": protocol,
        "entities": {
            "sampled": total,
            "unavailable": unavailable,
            "sample": sample,
        },
        "notes": [
            *(brand.get("notes") or []),
            *(protocol.get("notes") or []),
            f"核心域实体采样 {total} · unavailable {unavailable}",
        ],
    }


async def post_upgrade_health(hass: HomeAssistant, payload: dict[str, Any]) -> dict[str, Any]:
    """Core 升级后巡检（doc 19 §5.7 / S16）。"""
    from .device_registry_fields import probe_ha_features
    from homeassistant.const import __version__ as core_version

    wanted = payload.get("checks") or ["pip", "hassio", "brand", "protocol"]
    if isinstance(wanted, str):
        wanted = [wanted]
    wanted_set = {str(x) for x in wanted}

    features = probe_ha_features(hass, str(core_version))
    notes: list[str] = []
    results: dict[str, Any] = {
        "ha_version": features.get("ha_version"),
        "hassio_available": features.get("hassio_available"),
        "pip_deps_ok": features.get("pip_deps_ok"),
        "pip_deps": features.get("pip_deps"),
    }
    ok = True

    if "hassio" in wanted_set:
        if features.get("hassio_available"):
            notes.append("hassio：可用")
        else:
            notes.append("hassio：不可用（Container/Core 或未挂载 Supervisor）")
            # not necessarily fail — form-dependent

    if "pip" in wanted_set:
        if features.get("pip_deps_ok") is False:
            ok = False
            notes.append("pip 依赖：异常（见 pip_deps）")
        else:
            notes.append("pip 依赖：正常或无需检查的集成未安装")

    brand = None
    protocol = None
    if "brand" in wanted_set:
        brand = await brand_probe(hass, payload)
        results["brand"] = brand
        notes.extend(brand.get("notes") or [])
        for e in brand.get("entries") or []:
            if e.get("status") == "degraded":
                ok = False
                notes.append(f"品牌条目降级：{e.get('domain')}")

    if "protocol" in wanted_set:
        protocol = await protocol_probe(hass, payload)
        results["protocol"] = protocol
        notes.extend(protocol.get("notes") or [])
        for n in protocol.get("nodes") or []:
            if n.get("status") == "present" and n.get("healthy") is False:
                ok = False
                notes.append(f"协议不健康：{n.get('label')}")

    # Failed config entries after upgrade
    failed_entries = []
    for e in hass.config_entries.async_entries():
        state = getattr(e, "state", None)
        state_s = str(getattr(state, "value", state) or "")
        if state_s in ("setup_error", "setup_retry", "migration_error", "failed_unload"):
            failed_entries.append(
                {"domain": e.domain, "title": getattr(e, "title", None), "state": state_s}
            )
    results["failed_entries"] = failed_entries[:20]
    if failed_entries:
        ok = False
        notes.append(f"配置条目异常 {len(failed_entries)} 个")

    return {
        "status": "ok",
        "ok": ok,
        "checks": sorted(wanted_set),
        **results,
        "notes": notes,
    }
