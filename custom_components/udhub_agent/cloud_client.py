"""UDHUB Agent cloud WebSocket client.

Implements the Agent side of doc/09_UDHUB云枢_Agent协议_v0.1.md:
- outbound WebSocket over /agent/v1/ws
- hello authentication
- register/heartbeat
- sync.full / sync.delta
- command.execute handling with whitelist
- command.result receipts
- policy.update handling
- automatic reconnect with backoff
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import random
import re
import time
import traceback
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import aiohttp_client
from homeassistant.const import __version__ as HA_VERSION

from .const import (
    AGENT_VERSION,
    ALLOWED_DOMAINS,
    ALLOWED_HA_SERVICES,
    CAPABILITY_TIER_EXTENDED,
    CAPABILITY_TIER_STANDARD,
    CONF_ACCESS_TOKEN,
    CONF_CLOUD_URL,
    CONF_GATEWAY_ID,
    DEFAULT_CLOUD_URL,
    DENIED_DOMAINS,
    DOMAIN,
    EXTENDED_DOMAINS,
    HEARTBEAT_INTERVAL,
    PROTOCOL_VERSION,
    RECONNECT_BASE,
    RECONNECT_MAX,
    SELF_RELOAD_COOLDOWN_SEC,
    STANDARD_DENIED_DOMAINS,
    STUCK_NO_ATTEMPT_SEC,
    SUPERVISOR_INTERVAL_SEC,
    SYNC_DOMAINS,
    URL_ROTATE_AFTER_FAILURES,
    WS_PROTO_HEARTBEAT_SEC,
)
from .config_paths import (
    WRITE_SCOPE_CONFIG,
    WRITE_SCOPE_PACKAGES,
    WRITE_SCOPE_UDHUB,
    filter_visible_config_entries,
    is_allowed_config_relative_path,
    is_allowed_file_relative_path,
    is_listable_config_relative_path,
    resolve_config_target,
)

_LOGGER = logging.getLogger(__name__)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    import secrets

    return f"{prefix}_{secrets.token_hex(8)}"


def _ws_url_from_http(http_url: str) -> str:
    """Map http://host:port to ws://host:port/agent/v1/ws."""
    url = http_url.rstrip("/")
    if url.startswith("https://"):
        return url.replace("https://", "wss://", 1) + "/agent/v1/ws"
    if url.startswith("http://"):
        return url.replace("http://", "ws://", 1) + "/agent/v1/ws"
    # bare host:port
    return f"ws://{url}/agent/v1/ws"


def _split_cloud_urls(raw: str | None) -> list[str]:
    """Parse comma/space-separated cloud URL list (fallback endpoints supported).

    Single URL stays the common case; extras rotate in when connects keep failing.
    """
    urls: list[str] = []
    for part in re.split(r"[,\s]+", str(raw or "").strip()):
        part = part.strip().rstrip("/")
        if part and part not in urls:
            urls.append(part)
    return urls


def _safe_attributes(state) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    raw = getattr(state, "attributes", {}) or {}
    for key, value in raw.items():
        if key in ("entity_id", "context", "user_id"):
            continue
        if isinstance(value, (str, int, float, bool)):
            attrs[key] = value
        elif isinstance(value, (list, dict)):
            # keep small; avoid dumping large arrays
            attrs[key] = value
        else:
            attrs[key] = str(value)
    return attrs


def _area_name(hass: HomeAssistant, area_id: str | None) -> str | None:
    if not area_id:
        return None
    try:
        from homeassistant.helpers import area_registry as ar

        registry = ar.async_get(hass)
        area = registry.async_get_area(area_id) if registry is not None else None
        return area.name if area else None
    except Exception:
        return None


def _entity_area_id(device_entry=None, entity_entry=None) -> str | None:
    area_id = None
    if entity_entry is not None:
        area_id = getattr(entity_entry, "area_id", None)
    if not area_id and device_entry is not None:
        area_id = getattr(device_entry, "area_id", None)
    return area_id or None


def _entity_room(hass: HomeAssistant, device_entry=None, entity_entry=None) -> str | None:
    area_id = None
    if entity_entry is not None:
        area_id = getattr(entity_entry, "area_id", None)
    if not area_id and device_entry is not None:
        area_id = getattr(device_entry, "area_id", None)
    return _area_name(hass, area_id)


def _lookup_registry_entry(ereg, entity_id: str):
    """HA entity registry lookup (compatible across HA versions)."""
    if ereg is None:
        return None
    try:
        return ereg.async_get(entity_id)
    except Exception:
        entities = getattr(ereg, "entities", None)
        if isinstance(entities, dict):
            return entities.get(entity_id)
    return None


def _state_to_entity(
    state,
    registry_entry=None,
    room: str | None = None,
    device_entry=None,
    area_id: str | None = None,
) -> dict[str, Any] | None:
    entity_id = state.entity_id
    domain = entity_id.split(".")[0] if "." in entity_id else "unknown"
    if domain not in SYNC_DOMAINS:
        return None
    if registry_entry is not None:
        if getattr(registry_entry, "disabled", False) or getattr(
            registry_entry, "disabled_by", None
        ):
            return None
        hidden_by = getattr(registry_entry, "hidden_by", None)
        # HA 常默认隐藏 scene/automation 实体，联动同步仍需上报
        if hidden_by and domain not in ("scene", "automation"):
            return None
    s = state.state
    available = s not in ("unavailable", "unknown", None)
    attrs = _safe_attributes(state)
    if registry_entry is not None and getattr(registry_entry, "entity_category", None):
        attrs.setdefault("entity_category", str(registry_entry.entity_category).split(".")[-1].lower())
    payload: dict[str, Any] = {
        "entity_id": entity_id,
        "domain": domain,
        "name": state.attributes.get("friendly_name") or entity_id,
        "state": s,
        "available": available,
        "attributes": attrs,
        "last_changed": state.last_changed.isoformat() if state.last_changed else None,
        "last_updated": state.last_updated.isoformat() if state.last_updated else None,
    }
    if registry_entry is not None and registry_entry.device_id:
        payload["device_id"] = registry_entry.device_id
    if room:
        payload["room"] = room
        payload["area_name"] = room
    resolved_area_id = area_id or _entity_area_id(device_entry, registry_entry)
    if resolved_area_id:
        payload["area_id"] = resolved_area_id
    return payload


def _entity_from_registry_entry(
    entry, room: str | None = None, device_entry=None, area_id: str | None = None
) -> dict[str, Any] | None:
    entity_id = getattr(entry, "entity_id", None)
    if not entity_id:
        return None
    domain = entity_id.split(".")[0] if "." in entity_id else "unknown"
    if domain not in ("scene", "automation"):
        return None
    if getattr(entry, "disabled_by", None) or getattr(entry, "disabled", False):
        return None
    name = (
        getattr(entry, "name", None)
        or getattr(entry, "original_name", None)
        or entity_id
    )
    payload: dict[str, Any] = {
        "entity_id": entity_id,
        "domain": domain,
        "name": name,
        "state": "unknown",
        "available": False,
        "attributes": {"friendly_name": name},
        "device_id": getattr(entry, "device_id", None),
    }
    if room:
        payload["room"] = room
        payload["area_name"] = room
    resolved_area_id = area_id or _entity_area_id(device_entry, entry)
    if resolved_area_id:
        payload["area_id"] = resolved_area_id
    return payload


class UdhubCloudClient:
    """Manages the outbound WebSocket connection to UDHUB cloud."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        # Primary + fallback endpoints (cloud_url may be comma-separated).
        self.cloud_urls = _split_cloud_urls(
            str(entry.data.get(CONF_CLOUD_URL) or "")
        ) or [DEFAULT_CLOUD_URL]
        self._url_idx = 0
        self.gateway_id = str(entry.data.get(CONF_GATEWAY_ID) or "")
        self.access_token = str(entry.data.get(CONF_ACCESS_TOKEN) or "")

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._supervisor_task: asyncio.Task | None = None
        self._state_listener = None
        self._closing = False
        self._connected = False
        self._seen_command_ids: set[str] = set()
        self._max_ws_message_bytes = 256 * 1024  # 256 KB
        self._command_timeout_sec = 90
        # --- self-healing bookkeeping (monotonic clocks) ---
        self._consecutive_failures = 0
        self._last_attempt_ts: float = 0.0  # last _run() loop iteration
        self._last_auth_ok_ts: float = 0.0  # last successful hello.ok
        self._last_self_reload_ts: float = 0.0
        self._policy: dict[str, Any] = {
            "delivery_status": None,
            "capability_tier": CAPABILITY_TIER_STANDARD,
            "allowed_domains": list(ALLOWED_DOMAINS),
            "extended_domains": list(EXTENDED_DOMAINS),
            "supervisor_ops": False,
            "config_write_scope": WRITE_SCOPE_UDHUB,
            "allowed_command_kinds": None,
        }

    def _start_background_task(self, coro, name: str) -> asyncio.Task:
        """Create a background task; fallback for older HA versions."""
        if hasattr(self.hass, "async_create_background_task"):
            return self.hass.async_create_background_task(coro, name=name)
        task = asyncio.create_task(coro)
        try:
            task.set_name(name)
        except Exception:
            pass
        return task

    @property
    def cloud_url(self) -> str:
        """Current (possibly rotated) cloud endpoint."""
        return self.cloud_urls[self._url_idx % len(self.cloud_urls)]

    @property
    def ws_url(self) -> str:
        """Current WebSocket endpoint."""
        return _ws_url_from_http(self.cloud_url)

    async def start(self) -> None:
        """Start the background connection task."""
        if not self.gateway_id or not self.access_token:
            _LOGGER.info(
                "UDHUB cloud client idle (awaiting SaaS claim) entry=%s",
                self.entry.entry_id,
            )
            return
        if self._task and not self._task.done():
            return
        self._closing = False
        _LOGGER.info(
            "UDHUB cloud client starting for gateway=%s cloud=%s ws=%s",
            self.gateway_id,
            self.cloud_url,
            self.ws_url,
        )
        self._task = self._start_background_task(
            self._run(), name=f"udhub_agent_{self.gateway_id}"
        )
        # Detached supervisor (empty contextvars context survives entry-scope
        # cancels during reload): restarts dead connection loops and escalates
        # to entry reload when the loop is stuck (OTA-reload-hang self-heal).
        if self._supervisor_task is None or self._supervisor_task.done():
            self._supervisor_task = asyncio.Task(
                self._supervisor_loop(),
                name=f"udhub_sup_{self.gateway_id}",
                context=contextvars.Context(),
            )

    async def stop(self) -> None:
        """Stop the client and clean up."""
        self._closing = True
        if self._state_listener:
            self._state_listener()
            self._state_listener = None
        if self._supervisor_task and not self._supervisor_task.done():
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except asyncio.CancelledError:
                pass
        self._supervisor_task = None
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._connected = False

    async def _run(self) -> None:
        """Main reconnect loop."""
        backoff = RECONNECT_BASE
        while not self._closing:
            self._last_attempt_ts = time.monotonic()
            self._consecutive_failures += 1
            try:
                await self._connect_and_serve()
                backoff = RECONNECT_BASE
            except aiohttp.WSServerHandshakeError as e:
                _LOGGER.warning("UDHUB WS handshake failed: %s", e)
            except aiohttp.ClientError as e:
                _LOGGER.warning("UDHUB WS connection error: %s", e)
            except Exception:
                _LOGGER.exception("UDHUB WS unexpected error")

            if self._closing:
                break
            # Rotate fallback endpoint after repeated failures on this URL.
            if (
                self._consecutive_failures % URL_ROTATE_AFTER_FAILURES == 0
                and len(self.cloud_urls) > 1
            ):
                self._url_idx = (self._url_idx + 1) % len(self.cloud_urls)
                _LOGGER.warning(
                    "UDHUB rotating cloud endpoint after %s failures → %s",
                    self._consecutive_failures,
                    self.cloud_url,
                )
            jitter = random.uniform(0, 1)
            delay = min(backoff, RECONNECT_MAX) + jitter
            _LOGGER.info("UDHUB reconnecting in %.1fs", delay)
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, RECONNECT_MAX)

    async def _supervisor_loop(self) -> None:
        """Independent watchdog over the connection task (self-healing).

        L1: connection task exited unexpectedly → restart it in-process.
        L2: connection loop STUCK — no new connect attempt while unhealthy
            (OTA-reload-hang signature) → detached entry reload with cooldown.
        """
        await asyncio.sleep(SUPERVISOR_INTERVAL_SEC)
        while not self._closing:
            try:
                if self._connected and self._ws and not self._ws.closed:
                    await asyncio.sleep(SUPERVISOR_INTERVAL_SEC)
                    continue

                # Unhealthy — is the connection task even running?
                if self._task is None or self._task.done():
                    _LOGGER.warning(
                        "UDHUB supervisor: connection task exited unexpectedly, restarting"
                    )
                    self._connected = False
                    self._task = self._start_background_task(
                        self._run(), name=f"udhub_agent_{self.gateway_id}"
                    )
                    await asyncio.sleep(SUPERVISOR_INTERVAL_SEC)
                    continue

                # Task alive but making no new attempts while unhealthy → stuck.
                now = time.monotonic()
                idle = now - self._last_attempt_ts
                if idle > STUCK_NO_ATTEMPT_SEC:
                    if now - self._last_self_reload_ts < SELF_RELOAD_COOLDOWN_SEC:
                        _LOGGER.info(
                            "UDHUB supervisor: loop idle %.0fs while disconnected "
                            "(self-reload on cooldown)",
                            idle,
                        )
                    else:
                        self._last_self_reload_ts = now
                        _LOGGER.warning(
                            "UDHUB supervisor: no connect attempt for %.0fs while "
                            "disconnected — scheduling self reload",
                            idle,
                        )
                        self._schedule_self_reload()
                await asyncio.sleep(SUPERVISOR_INTERVAL_SEC)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("UDHUB supervisor error")
                await asyncio.sleep(SUPERVISOR_INTERVAL_SEC)

    def _schedule_self_reload(self) -> None:
        """Detached entry reload — survives unload-scope cancellation."""
        from . import self_update as self_update_mod

        asyncio.Task(
            self_update_mod.schedule_reload_after_update(
                self.hass, self.entry, target_version="supervisor-heal"
            ),
            name=f"udhub_heal_{self.gateway_id}",
            context=contextvars.Context(),
        )

    async def _connect_and_serve(self) -> None:
        """Open WebSocket, authenticate, then read messages."""
        self._session = aiohttp_client.async_get_clientsession(self.hass)
        _LOGGER.info("UDHUB connecting to %s", self.ws_url)

        async with self._session.ws_connect(
            self.ws_url,
            # Protocol-level liveness: aiohttp pings every N s and closes the
            # socket when a pong is missed → read loop exits → reconnect.
            # Catches half-open TCP that app-level heartbeat cannot detect.
            heartbeat=WS_PROTO_HEARTBEAT_SEC,
            autoping=True,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as ws:
            self._ws = ws
            # 1) send hello
            hello = self._envelope("agent.hello", {
                "access_token": self.access_token,
                "gateway_id": self.gateway_id,
                "protocol_version": PROTOCOL_VERSION,
                "agent_version": AGENT_VERSION,
                "ha_version": HA_VERSION,
                "capability_manifest": self._capability_manifest(),
                "ha_features": self._ha_features(),
            })
            await ws.send_str(json.dumps(hello))

            # 2) wait for hello.ok / hello.error
            first = await ws.receive(timeout=15)
            if first.type == aiohttp.WSMsgType.TEXT:
                msg = json.loads(first.data)
                if msg.get("type") == "hello.error":
                    _LOGGER.error("UDHUB auth rejected: %s", msg.get("payload", {}))
                    return
                if msg.get("type") != "hello.ok":
                    _LOGGER.warning("UDHUB unexpected first frame: %s", msg.get("type"))
                    return
                _LOGGER.info("UDHUB authenticated gateway=%s", self.gateway_id)
                self._connected = True
                self._last_auth_ok_ts = time.monotonic()
                self._consecutive_failures = 0
            else:
                _LOGGER.warning("UDHUB WS closed before hello: %s", first.type)
                return

            # 3) register environment and full sync
            await self._send_register(ws)
            await self._send_full_sync(ws)
            self._subscribe_state_changes(ws)
            self._heartbeat_task = self._start_background_task(
                self._heartbeat_loop(ws), name=f"udhub_hb_{self.gateway_id}"
            )

            # 4) message loop
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(ws, msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.error("UDHUB WS error: %s", ws.exception())
                    break
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                    break

    def _envelope(self, msg_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "type": msg_type,
            "message_id": _new_id("msg"),
            "gateway_id": self.gateway_id,
            "correlation_id": None,
            "sent_at": _now_iso(),
            "payload": payload,
        }

    async def _send_register(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        # 安装/连云后：能开则自动开启 SSH，再随 register 上报状态
        ssh_snap: dict[str, Any]
        try:
            from . import ssh_bypass

            ssh_snap = await ssh_bypass.ensure_ssh_bypass(self.hass)
        except Exception:
            _LOGGER.debug("UDHUB ssh ensure on register failed", exc_info=True)
            ssh_snap = await self._ssh_bypass_snapshot()
        payload = {
            "agent_version": AGENT_VERSION,
            "ha_version": HA_VERSION,
            "install_type": await self._detect_install_type(),
            "timezone": str(self.hass.config.time_zone),
            "location_name": self.hass.config.location_name,
            "ssh_bypass": ssh_snap,
            "host_metrics": await self._host_metrics_snapshot(),
            "capability_manifest": self._capability_manifest(),
            "ha_features": self._ha_features(),
        }
        await ws.send_str(json.dumps(self._envelope("agent.register", payload)))

    def _capability_manifest(self) -> dict[str, Any]:
        from .capability_catalog import build_manifest

        return build_manifest(AGENT_VERSION)

    async def _detect_install_type(self) -> str:
        """Best-effort HA installation type detection."""
        try:
            from homeassistant.helpers import system_info as si
            info = await si.async_get_system_info(self.hass)
            return info.get("installation_type", "Unknown")
        except Exception:
            return "Unknown"

    async def _ssh_bypass_snapshot(self) -> dict[str, Any]:
        try:
            from . import ssh_bypass

            return await ssh_bypass.probe_ssh_bypass(self.hass)
        except Exception:
            _LOGGER.debug("UDHUB ssh bypass snapshot failed", exc_info=True)
            return {"status": "unknown", "detail": "snapshot_error"}

    async def _host_metrics_snapshot(self) -> dict[str, Any]:
        try:
            from . import host_metrics

            return await host_metrics.collect_host_metrics(self.hass)
        except Exception:
            _LOGGER.debug("UDHUB host metrics snapshot failed", exc_info=True)
            return {
                "supported": False,
                "source": "unsupported",
                "detail": "collect_error",
            }

    async def _heartbeat_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Send periodic heartbeat."""
        try:
            while not self._closing and not ws.closed:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if self._closing or ws.closed:
                    break
                payload = {
                    "agent_version": AGENT_VERSION,
                    "ha_version": HA_VERSION,
                    "install_type": await self._detect_install_type(),
                    "uptime_sec": 0,  # placeholder
                    "queue_depth": 0,
                    "policy_version": 1,
                    "health": {"agent": "ok", "last_sync_at": None},
                    "ssh_bypass": await self._ssh_bypass_snapshot(),
                    "host_metrics": await self._host_metrics_snapshot(),
                    "capability_manifest": self._capability_manifest(),
                    "ha_features": self._ha_features(),
                }
                await ws.send_str(json.dumps(self._envelope("agent.heartbeat", payload)))
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.exception("UDHUB heartbeat error — closing socket to force reconnect")
            # A dead send used to leave the read loop hanging on a zombie
            # socket; close it so the reconnect loop takes over immediately.
            try:
                await ws.close()
            except Exception:
                pass

    async def _exec_ssh_bypass_probe(self, result: dict[str, Any]) -> None:
        snap = await self._ssh_bypass_snapshot()
        result["status"] = "ok"
        result["ssh_bypass"] = snap

    async def _exec_ssh_bypass_ensure(self, result: dict[str, Any]) -> None:
        from . import ssh_bypass

        snap = await ssh_bypass.ensure_ssh_bypass(self.hass)
        result["ssh_bypass"] = snap
        if snap.get("enabled"):
            result["status"] = "ok"
        elif snap.get("ensure") == "skipped_unsupported":
            result["status"] = "ok"
            result["error"] = None
        else:
            result["status"] = "failed"
            result["error"] = str(
                snap.get("error") or snap.get("ensure") or "ssh_bypass_ensure_failed"
            )

    async def _exec_packages_probe(self, result: dict[str, Any]) -> None:
        from . import packages_setup

        snap = await packages_setup.probe_packages(self.hass)
        result["packages"] = snap
        result["status"] = "ok" if snap.get("status") == "ready" else "ok"

    async def _exec_packages_ensure(self, result: dict[str, Any]) -> None:
        from . import packages_setup

        snap = await packages_setup.ensure_packages(self.hass)
        result["packages"] = snap
        if snap.get("status") == "ready":
            result["status"] = "ok"
        elif snap.get("status") == "needs_restart":
            result["status"] = "ok"
            result["error"] = "packages_needs_restart"
        else:
            result["status"] = "failed"
            result["error"] = str(
                snap.get("error") or snap.get("status") or "packages_ensure_failed"
            )

    async def _send_full_sync(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        *,
        exclude_entity_ids: set[str] | None = None,
        removed_entity_ids: list[str] | None = None,
    ) -> None:
        """Upload current hass.states snapshot."""
        skip = {str(x) for x in (exclude_entity_ids or set()) if x}
        devices = self._build_device_list()
        ereg = None
        dreg = None
        try:
            from homeassistant.helpers import entity_registry as er
            from homeassistant.helpers import device_registry as dr

            ereg = er.async_get(self.hass)
            dreg = dr.async_get(self.hass)
        except Exception:
            _LOGGER.debug("UDHUB entity registry unavailable", exc_info=True)
        entities: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for state in self.hass.states.async_all():
            if state.entity_id in skip:
                continue
            entry = _lookup_registry_entry(ereg, state.entity_id)
            device_entry = None
            if entry is not None and getattr(entry, "device_id", None) and dreg is not None:
                try:
                    device_entry = dreg.async_get(entry.device_id)
                except Exception:
                    device_entry = None
            room = _entity_room(self.hass, device_entry, entry)
            entity = _state_to_entity(state, entry, room, device_entry)
            if entity:
                entities.append(entity)
                seen_ids.add(entity["entity_id"])
        # 补全 registry 中有但 states 未覆盖的 scene/automation（含默认 hidden）
        if ereg is not None:
            try:
                reg_entities = getattr(ereg, "entities", None)
                entries = reg_entities.values() if reg_entities is not None else []
                for entry in entries:
                    eid = getattr(entry, "entity_id", "")
                    if not eid or eid in seen_ids or eid in skip:
                        continue
                    domain = eid.split(".")[0] if "." in eid else ""
                    if domain not in ("scene", "automation"):
                        continue
                    state = self.hass.states.get(eid)
                    device_entry = None
                    if getattr(entry, "device_id", None) and dreg is not None:
                        try:
                            device_entry = dreg.async_get(entry.device_id)
                        except Exception:
                            device_entry = None
                    room = _entity_room(self.hass, device_entry, entry)
                    if state is not None:
                        entity = _state_to_entity(state, entry, room, device_entry)
                    else:
                        entity = _entity_from_registry_entry(
                            entry, room, device_entry
                        )
                    if entity:
                        entities.append(entity)
                        seen_ids.add(entity["entity_id"])
            except Exception:
                _LOGGER.debug("UDHUB linkage registry supplement failed", exc_info=True)
        payload: dict[str, Any] = {
            "devices": devices,
            "entities": entities,
            "areas": self._build_area_list(),
            "floors": self._build_floor_list(),
            "labels": self._build_label_list(),
            "zones": self._build_zone_list(),
            "persons": self._build_person_list(),
            "config_entries": self._build_config_entries_summary(),
            "full": True,
        }
        removed = [str(x) for x in (removed_entity_ids or []) if x]
        if removed:
            payload["removed_entity_ids"] = removed
        await ws.send_str(json.dumps(self._envelope("sync.full", payload)))
        _LOGGER.debug("UDHUB sent sync.full devices=%d entities=%d", len(devices), len(entities))

    def _build_floor_list(self) -> list[dict[str, Any]]:
        try:
            from homeassistant.helpers import floor_registry as fr

            reg = fr.async_get(self.hass)
            floors = getattr(reg, "floors", {}) or {}
            return [
                {
                    "floor_id": f.floor_id,
                    "name": f.name,
                    "level": getattr(f, "level", None),
                }
                for f in floors.values()
            ]
        except Exception:
            return []

    def _build_label_list(self) -> list[dict[str, Any]]:
        try:
            from homeassistant.helpers import label_registry as lr

            reg = lr.async_get(self.hass)
            return [
                {
                    "label_id": e.label_id,
                    "name": e.name,
                    "color": getattr(e, "color", None),
                    "icon": getattr(e, "icon", None),
                }
                for e in reg.async_list_labels()
            ]
        except Exception:
            return []

    def _build_zone_list(self) -> list[dict[str, Any]]:
        try:
            return [
                {
                    "entity_id": st.entity_id,
                    "name": st.name,
                    "latitude": st.attributes.get("latitude"),
                    "longitude": st.attributes.get("longitude"),
                    "radius": st.attributes.get("radius"),
                }
                for st in self.hass.states.async_all("zone")
            ]
        except Exception:
            return []

    def _build_person_list(self) -> list[dict[str, Any]]:
        try:
            return [
                {
                    "entity_id": st.entity_id,
                    "name": st.name,
                    "user_id": st.attributes.get("user_id"),
                }
                for st in self.hass.states.async_all("person")
            ]
        except Exception:
            return []

    def _build_config_entries_summary(self) -> list[dict[str, Any]]:
        try:
            return [
                {
                    "entry_id": e.entry_id,
                    "domain": e.domain,
                    "title": e.title,
                    "state": str(e.state) if e.state else None,
                    "disabled_by": str(e.disabled_by) if e.disabled_by else None,
                }
                for e in self.hass.config_entries.async_entries()
            ]
        except Exception:
            return []

    def _build_device_list(self) -> list[dict[str, Any]]:
        """Build device list from HA device registry (field devices only)."""
        try:
            from homeassistant.helpers import device_registry as dr

            from .device_registry_fields import serialize_device_row

            registry = dr.async_get(self.hass)
            if registry is None:
                return []
            service_type = getattr(dr, "DeviceEntryType", None)
            service_enum = getattr(service_type, "SERVICE", None) if service_type else None
            by_id = {d.id: d for d in registry.devices.values()}
            devices = []
            for d in registry.devices.values():
                if getattr(d, "disabled_by", None) or getattr(d, "disabled", False):
                    continue
                row = serialize_device_row(
                    self.hass, d, include_room=True, devices_by_id=by_id
                )
                # Keep SERVICE devices (Season 等) with entry_type for SaaS「设备与服务」拆分
                if (
                    not getattr(d, "parent_device_id", None)
                    and service_enum is not None
                    and getattr(d, "entry_type", None) == service_enum
                ):
                    row["entry_type"] = "service"
                row["online_status"] = "UNKNOWN"
                devices.append(row)
            return devices
        except Exception:
            _LOGGER.exception("UDHUB failed to read device registry")
            return []

    def _ha_features(self) -> dict[str, Any]:
        from .device_registry_fields import probe_ha_features

        return probe_ha_features(self.hass, HA_VERSION)

    def _build_area_list(self) -> list[dict[str, Any]]:
        """Build HA area registry snapshot for sync.full."""
        try:
            from homeassistant.helpers import area_registry as ar

            registry = ar.async_get(self.hass)
            if registry is None:
                return []
            return [
                {
                    "id": area.id,
                    "name": area.name,
                    "floor_id": getattr(area, "floor_id", None),
                }
                for area in registry.areas.values()
            ]
        except Exception:
            _LOGGER.debug("UDHUB failed to read area registry", exc_info=True)
            return []

    def _subscribe_state_changes(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Subscribe to hass state_changed events and send sync.delta."""

        @callback
        def _on_state_changed(event):
            if self._closing or not self._connected:
                return
            new_state = event.data.get("new_state")
            if not new_state:
                return
            entry = None
            device_entry = None
            try:
                from homeassistant.helpers import entity_registry as er
                from homeassistant.helpers import device_registry as dr

                ereg = er.async_get(self.hass)
                dreg = dr.async_get(self.hass)
                entry = _lookup_registry_entry(ereg, new_state.entity_id)
                if entry is not None and getattr(entry, "device_id", None) and dreg is not None:
                    try:
                        device_entry = dreg.async_get(entry.device_id)
                    except Exception:
                        device_entry = None
            except Exception:
                entry = None
            room = _entity_room(self.hass, device_entry, entry)
            entity = _state_to_entity(new_state, entry, room, device_entry)
            if not entity:
                return
            payload = {"entities": [entity]}
            frame = self._envelope("sync.delta", payload)
            asyncio.run_coroutine_threadsafe(
                self._safe_send(ws, frame), self.hass.loop
            )

        self._state_listener = self.hass.bus.async_listen(
            EVENT_STATE_CHANGED, _on_state_changed
        )

    async def _safe_send(self, ws: aiohttp.ClientWebSocketResponse, frame: dict[str, Any]) -> None:
        try:
            if not ws.closed:
                await ws.send_str(json.dumps(frame))
        except Exception:
            _LOGGER.debug("UDHUB send failed: %s", traceback.format_exc(limit=1))

    async def _handle_message(
        self, ws: aiohttp.ClientWebSocketResponse, raw: str
    ) -> None:
        # Guard: reject oversized messages before parsing
        if len(raw) > self._max_ws_message_bytes:
            _LOGGER.warning(
                "UDHUB dropping oversized WS message (%d bytes, limit %d)",
                len(raw),
                self._max_ws_message_bytes,
            )
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            _LOGGER.warning("UDHUB invalid JSON: %s", raw[:200])
            return

        msg_type = msg.get("type")
        payload = msg.get("payload", {})
        correlation_id = msg.get("correlation_id") or msg.get("message_id")

        if msg_type == "policy.update":
            self._policy["delivery_status"] = payload.get("delivery_status")
            self._policy["capability_tier"] = (
                payload.get("capability_tier") or CAPABILITY_TIER_STANDARD
            )
            self._policy["allowed_domains"] = payload.get("allowed_domains") or list(
                ALLOWED_DOMAINS
            )
            self._policy["extended_domains"] = payload.get("extended_domains") or list(
                EXTENDED_DOMAINS
            )
            self._policy["supervisor_ops"] = bool(payload.get("supervisor_ops"))
            self._policy["config_write_scope"] = (
                payload.get("config_write_scope") or WRITE_SCOPE_UDHUB
            )
            raw_kinds = payload.get("allowed_command_kinds")
            if isinstance(raw_kinds, list) and raw_kinds:
                self._policy["allowed_command_kinds"] = [
                    str(k).strip() for k in raw_kinds if str(k).strip()
                ]
            else:
                self._policy["allowed_command_kinds"] = None
            _LOGGER.debug("UDHUB policy updated: %s", self._policy)
            return

        if msg_type == "command.execute":
            await self._handle_command(ws, payload, correlation_id)
            return

        if msg_type == "agent.revoke":
            _LOGGER.warning("UDHUB received revoke; starting reauth flow")
            await self.stop()
            self.hass.async_create_task(self.entry.async_start_reauth(self.hass))
            return

    async def _handle_command(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        payload: dict[str, Any],
        correlation_id: str | None,
    ) -> None:
        command_id = payload.get("command_id") or correlation_id or _new_id("cmd")
        kind = payload.get("kind", "call_service")
        result_payload: dict[str, Any] = {
            "command_id": command_id,
            "status": "failed",
            "error": None,
        }
        if payload.get("revision_id"):
            result_payload["revision_id"] = payload.get("revision_id")

        # Deduplication: skip already-seen command IDs
        if command_id in self._seen_command_ids:
            _LOGGER.warning("UDHUB duplicate command_id=%s; skipping", command_id)
            return
        self._seen_command_ids.add(command_id)
        # Cap the dedup set to prevent unbounded growth
        if len(self._seen_command_ids) > 10_000:
            self._seen_command_ids.clear()

        if not self._is_command_kind_allowed(kind):
            result_payload["status"] = "denied"
            result_payload["error"] = "command_kind_not_allowed"
            receipt = self._envelope("command.result", result_payload)
            receipt["correlation_id"] = correlation_id
            await self._safe_send(ws, receipt)
            return

        try:
            from .catalog_dispatch import dispatch_catalog

            catalog_handled = await dispatch_catalog(
                self.hass,
                kind=kind,
                action=payload.get("action"),
                payload=payload,
                require_supervisor_ops=self._require_supervisor_ops,
                require_extended_tier=self._require_extended_tier,
                result=result_payload,
            )
            if catalog_handled:
                pass
            elif kind == "call_service":
                await asyncio.wait_for(
                    self._exec_call_service(ws, payload, result_payload),
                    timeout=self._command_timeout_sec,
                )
            elif kind == "config_write":
                await asyncio.wait_for(
                    self._exec_config_write(ws, payload, result_payload),
                    timeout=self._command_timeout_sec,
                )
            elif kind == "scene_create":
                await asyncio.wait_for(
                    self._exec_scene_create(ws, payload, result_payload),
                    timeout=self._command_timeout_sec,
                )
            elif kind == "registry_update":
                await asyncio.wait_for(
                    self._exec_registry_update(payload, result_payload),
                    timeout=self._command_timeout_sec,
                )
            elif kind == "sync_full":
                await asyncio.wait_for(
                    self._exec_sync_full(ws, result_payload),
                    timeout=120,
                )
            elif kind == "agent_update":
                await asyncio.wait_for(
                    self._exec_agent_update(payload, result_payload),
                    timeout=180,
                )
            elif kind == "agent_ping":
                # Lightweight command-channel probe (≠ heartbeat).
                result_payload["status"] = "ok"
                result_payload["pong"] = True
                result_payload["agent_version"] = AGENT_VERSION
                result_payload["probed_at"] = _now_iso()
            elif kind == "ssh_bypass_probe":
                await asyncio.wait_for(
                    self._exec_ssh_bypass_probe(result_payload),
                    timeout=30,
                )
            elif kind == "ssh_bypass_ensure":
                await asyncio.wait_for(
                    self._exec_ssh_bypass_ensure(result_payload),
                    timeout=180,
                )
            elif kind == "packages_probe":
                await asyncio.wait_for(
                    self._exec_packages_probe(result_payload),
                    timeout=30,
                )
            elif kind == "packages_ensure":
                await asyncio.wait_for(
                    self._exec_packages_ensure(result_payload),
                    timeout=120,
                )
            elif kind == "supervisor_probe":
                await asyncio.wait_for(
                    self._exec_supervisor_probe(result_payload),
                    timeout=30,
                )
            elif kind == "supervisor_core_restart":
                await asyncio.wait_for(
                    self._exec_supervisor_core_restart(result_payload),
                    timeout=120,
                )
            elif kind == "supervisor_backup_list":
                await asyncio.wait_for(
                    self._exec_supervisor_backup_list(result_payload),
                    timeout=60,
                )
            elif kind == "supervisor_backup_create":
                await asyncio.wait_for(
                    self._exec_supervisor_backup_create(payload, result_payload),
                    timeout=300,
                )
            elif kind == "supervisor_addon_list":
                await asyncio.wait_for(
                    self._exec_supervisor_addon_list(result_payload),
                    timeout=30,
                )
            elif kind == "supervisor_addon_install":
                await asyncio.wait_for(
                    self._exec_supervisor_addon_install(payload, result_payload),
                    timeout=120,
                )
            elif kind == "supervisor_addon_uninstall":
                await asyncio.wait_for(
                    self._exec_supervisor_addon_uninstall(payload, result_payload),
                    timeout=120,
                )
            elif kind == "supervisor_addon_start":
                await asyncio.wait_for(
                    self._exec_supervisor_addon_start(payload, result_payload),
                    timeout=60,
                )
            elif kind == "supervisor_addon_stop":
                await asyncio.wait_for(
                    self._exec_supervisor_addon_stop(payload, result_payload),
                    timeout=60,
                )
            elif kind == "supervisor_addon_restart":
                await asyncio.wait_for(
                    self._exec_supervisor_addon_restart(payload, result_payload),
                    timeout=60,
                )
            elif kind == "supervisor_host_info":
                await asyncio.wait_for(
                    self._exec_supervisor_host_info(result_payload),
                    timeout=30,
                )
            elif kind == "supervisor_host_reboot":
                await asyncio.wait_for(
                    self._exec_supervisor_host_reboot(result_payload),
                    timeout=30,
                )
            elif kind == "supervisor_os_info":
                await asyncio.wait_for(
                    self._exec_supervisor_os_info(result_payload),
                    timeout=30,
                )
            elif kind == "supervisor_os_update":
                await asyncio.wait_for(
                    self._exec_supervisor_os_update(payload, result_payload),
                    timeout=600,
                )
            elif kind == "ha_restart":
                await asyncio.wait_for(
                    self._exec_ha_restart(payload, result_payload),
                    timeout=120,
                )
            elif kind == "ha_check_config":
                await asyncio.wait_for(
                    self._exec_ha_check_config(payload, result_payload),
                    timeout=60,
                )
            elif kind == "ha_reload_core":
                await asyncio.wait_for(
                    self._exec_ha_reload_core(payload, result_payload),
                    timeout=60,
                )
            elif kind == "file_read":
                await asyncio.wait_for(
                    self._exec_file_read(payload, result_payload),
                    timeout=30,
                )
            elif kind == "file_write":
                await asyncio.wait_for(
                    self._exec_file_write(payload, result_payload),
                    timeout=30,
                )
            elif kind == "file_delete":
                await asyncio.wait_for(
                    self._exec_file_delete(payload, result_payload),
                    timeout=30,
                )
            elif kind == "file_list":
                await asyncio.wait_for(
                    self._exec_file_list(payload, result_payload),
                    timeout=30,
                )
            elif kind == "integration_list":
                await asyncio.wait_for(
                    self._exec_integration_list(payload, result_payload),
                    timeout=30,
                )
            elif kind == "integration_reload":
                await asyncio.wait_for(
                    self._exec_integration_reload_cmd(payload, result_payload),
                    timeout=60,
                )
            elif kind == "integration_remove":
                await asyncio.wait_for(
                    self._exec_integration_remove(payload, result_payload),
                    timeout=60,
                )
            elif kind == "user_list":
                await asyncio.wait_for(
                    self._exec_user_list(payload, result_payload),
                    timeout=30,
                )
            elif kind == "user_create":
                await asyncio.wait_for(
                    self._exec_user_create(payload, result_payload),
                    timeout=30,
                )
            elif kind == "user_delete":
                await asyncio.wait_for(
                    self._exec_user_delete(payload, result_payload),
                    timeout=30,
                )
            elif kind == "helper_list":
                await asyncio.wait_for(
                    self._exec_helper_list(payload, result_payload),
                    timeout=30,
                )
            elif kind == "helper_create":
                await asyncio.wait_for(
                    self._exec_helper_create(payload, result_payload),
                    timeout=60,
                )
            elif kind == "helper_update":
                await asyncio.wait_for(
                    self._exec_helper_update(payload, result_payload),
                    timeout=60,
                )
            elif kind == "helper_delete":
                await asyncio.wait_for(
                    self._exec_helper_delete(payload, result_payload),
                    timeout=60,
                )
            elif kind == "group_list":
                await asyncio.wait_for(
                    self._exec_group_list(payload, result_payload),
                    timeout=30,
                )
            elif kind == "group_upsert":
                await asyncio.wait_for(
                    self._exec_group_upsert(payload, result_payload),
                    timeout=60,
                )
            elif kind == "group_delete":
                await asyncio.wait_for(
                    self._exec_group_delete(payload, result_payload),
                    timeout=60,
                )
            elif kind == "label_list":
                await asyncio.wait_for(
                    self._exec_label_list(payload, result_payload),
                    timeout=30,
                )
            elif kind == "label_create":
                await asyncio.wait_for(
                    self._exec_label_create(payload, result_payload),
                    timeout=30,
                )
            elif kind == "label_delete":
                await asyncio.wait_for(
                    self._exec_label_delete(payload, result_payload),
                    timeout=30,
                )
            elif kind == "label_assign":
                await asyncio.wait_for(
                    self._exec_label_assign(payload, result_payload),
                    timeout=30,
                )
            elif kind == "shell_command":
                await asyncio.wait_for(
                    self._exec_shell_command(payload, result_payload),
                    timeout=120,
                )
            else:
                result_payload["error"] = "unsupported_kind"
        except asyncio.TimeoutError:
            _LOGGER.error("UDHUB command %s timed out after %ds", command_id, self._command_timeout_sec)
            result_payload["status"] = "failed"
            result_payload["error"] = "command_timeout"
        except Exception as exc:
            _LOGGER.exception("UDHUB command failed")
            result_payload["status"] = "failed"
            result_payload["error"] = str(exc)

        receipt = self._envelope("command.result", result_payload)
        receipt["correlation_id"] = correlation_id
        await self._safe_send(ws, receipt)

    def _effective_capability_tier(self, payload: dict[str, Any]) -> str:
        tier = str(payload.get("tier") or self._policy.get("capability_tier") or CAPABILITY_TIER_STANDARD)
        if tier == CAPABILITY_TIER_EXTENDED:
            return CAPABILITY_TIER_EXTENDED
        return CAPABILITY_TIER_STANDARD

    def _allowed_service_domains(self, payload: dict[str, Any]) -> set[str]:
        tier = self._effective_capability_tier(payload)
        if tier == CAPABILITY_TIER_EXTENDED:
            raw = self._policy.get("extended_domains") or list(EXTENDED_DOMAINS)
        else:
            raw = self._policy.get("allowed_domains") or list(ALLOWED_DOMAINS)
        return {str(d) for d in raw}

    def _config_write_scope(self, payload: dict[str, Any]) -> str:
        if self._effective_capability_tier(payload) != CAPABILITY_TIER_EXTENDED:
            return WRITE_SCOPE_UDHUB
        requested = str(payload.get("write_scope") or "").strip().lower()
        policy_scope = str(
            self._policy.get("config_write_scope") or WRITE_SCOPE_UDHUB
        )
        scopes = (WRITE_SCOPE_UDHUB, WRITE_SCOPE_PACKAGES, WRITE_SCOPE_CONFIG)
        effective = (
            policy_scope if policy_scope in scopes else WRITE_SCOPE_UDHUB
        )
        if requested in scopes:
            effective = scopes[
                max(scopes.index(effective), scopes.index(requested))
            ]
        return effective

    def _require_supervisor_ops(self, result: dict[str, Any]) -> bool:
        if not self._policy.get("supervisor_ops"):
            result["status"] = "denied"
            result["error"] = "supervisor_ops_disabled"
            return False
        return True

    def _require_extended_tier(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> bool:
        if self._effective_capability_tier(payload) != CAPABILITY_TIER_EXTENDED:
            result["status"] = "denied"
            result["error"] = "requires_extended_tier"
            return False
        return True

    def _is_command_kind_allowed(self, kind: str) -> bool:
        """Enforce policy.update allowed_command_kinds when cloud provides a list."""
        normalized = str(kind or "call_service").strip()
        # Always allow diagnostic kinds (OTA + command-channel probe).
        if normalized in ("agent_update", "agent_ping"):
            return True
        allowed = self._policy.get("allowed_command_kinds")
        if not isinstance(allowed, list) or not allowed:
            return True
        if normalized in allowed:
            return True
        # Cloud may list config_delete; Agent implements delete via config_write.
        if normalized == "config_delete" and "config_write" in allowed:
            return True
        return False

    def _validate_file_path(
        self,
        payload: dict[str, Any],
        relative: str,
        result: dict[str, Any],
        *,
        write: bool,
        list_dir: bool = False,
    ) -> bool:
        if list_dir:
            allowed = is_listable_config_relative_path(relative)
        elif write:
            scope = self._config_write_scope(payload)
            allowed = is_allowed_file_relative_path(relative, scope, write=True)
            result["write_scope"] = scope
        else:
            allowed = is_allowed_file_relative_path(relative, WRITE_SCOPE_CONFIG, write=False)
        if not allowed:
            result["status"] = "denied"
            result["error"] = "path_not_allowed"
            return False
        return True

    async def _exec_call_service(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        payload: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        domain = payload.get("domain")
        service = payload.get("service")
        target = payload.get("target") or {}
        data = payload.get("data") or {}

        if domain in DENIED_DOMAINS:
            result["status"] = "denied"
            result["error"] = "domain_denied"
            return

        tier = self._effective_capability_tier(payload)
        if tier != CAPABILITY_TIER_EXTENDED and domain in STANDARD_DENIED_DOMAINS:
            result["status"] = "denied"
            result["error"] = "domain_denied"
            return

        allowed = self._allowed_service_domains(payload)
        if domain not in allowed:
            result["status"] = "denied"
            result["error"] = "domain_not_allowed"
            return

        if domain == "homeassistant" and service not in ALLOWED_HA_SERVICES:
            result["status"] = "denied"
            result["error"] = "service_not_allowed"
            return

        service_call = f"{domain}.{service}"
        _LOGGER.info("UDHUB executing service %s target=%s data=%s", service_call, target, data)
        await self.hass.services.async_call(domain, service, service_data=data, target=target, blocking=True)
        result["status"] = "ok"
        result["service"] = service_call
        if domain == "scene" and service == "delete":
            eid = target.get("entity_id") or data.get("entity_id")
            if eid:
                eid_s = str(eid)
                result["entity_id"] = eid_s
                # 删除后从 registry 清掉，避免下一轮 sync 从 registry 补回
                try:
                    from homeassistant.helpers import entity_registry as er

                    ereg = er.async_get(self.hass)
                    if ereg is not None and ereg.async_get(eid_s) is not None:
                        ereg.async_remove(eid_s)
                        result["registry_remove"] = "ok"
                    else:
                        result["registry_remove"] = "missing"
                except Exception as exc:
                    result["registry_remove"] = str(exc)
                try:
                    await self._send_full_sync(
                        ws,
                        exclude_entity_ids={eid_s},
                        removed_entity_ids=[eid_s],
                    )
                except Exception:
                    _LOGGER.debug(
                        "UDHUB sync after scene.delete failed", exc_info=True
                    )

    async def _exec_scene_create(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        payload: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Runtime scene.create for quick验证 (doc 17 §5.4 channel A)."""
        entities = payload.get("entities") or {}
        scene_id = payload.get("scene_id") or payload.get("entity_id") or "udhub_tmp"
        if isinstance(scene_id, str) and scene_id.startswith("scene."):
            scene_id = scene_id.split(".", 1)[1]
        if not isinstance(entities, dict) or not entities:
            result["status"] = "failed"
            result["error"] = "missing_entities"
            return
        data = {
            "scene_id": scene_id,
            "entities": entities,
        }
        # HA scene.create has no snapshot_name; friendly name is not part of the schema
        await self.hass.services.async_call(
            "scene", "create", service_data=data, blocking=True
        )
        entity_id = f"scene.{scene_id}"
        result["status"] = "ok"
        result["entity_id"] = entity_id
        # 立即上报，避免控制台列表仍读旧镜像
        try:
            await self._send_full_sync(ws)
        except Exception:
            _LOGGER.debug(
                "UDHUB sync after scene_create failed", exc_info=True
            )

    async def _exec_sync_full(
        self, ws: aiohttp.ClientWebSocketResponse, result: dict[str, Any]
    ) -> None:
        """Reload scene/automation registries, then push full entity sync."""
        for domain in ("scene", "automation"):
            try:
                await self.hass.services.async_call(domain, "reload", blocking=True)
            except Exception:
                _LOGGER.debug(
                    "UDHUB %s.reload skipped or failed during sync_full",
                    domain,
                    exc_info=True,
                )
        await self._send_full_sync(ws)
        result["status"] = "ok"

    async def _exec_agent_update(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        import importlib

        from . import self_update as self_update_mod
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(self.hass)
        payload = {
            **payload,
            "previous_version": AGENT_VERSION,
        }
        update_result = await self_update_mod.apply_agent_update(
            self.hass,
            self.entry,
            payload,
            session=session,
        )
        result.update(update_result)
        if update_result.get("status") == "ok":
            # Extract may have replaced self_update.py; reload helper from disk.
            try:
                importlib.reload(self_update_mod)
            except Exception:
                _LOGGER.debug(
                    "UDHUB reload self_update module failed", exc_info=True
                )
            self_update_mod.purge_agent_modules()
            # Detached task on purpose: HA tracks config-entry cancel scopes
            # via contextvars, so a task created inside this command handler
            # inherits the entry's scope and is cancelled by its own
            # async_reload (unload kills every scoped task mid-flight — the
            # entry then sticks in loaded/unload_in_progress with the old
            # version still running). A fresh empty Context severs that
            # inheritance so the reload survives the unload it triggers.
            _LOGGER.warning(
                "UDHUB agent_update: creating detached reload task target=v%s",
                update_result.get("version") or "?",
            )

            def _log_reload_done(task: "asyncio.Task[None]") -> None:
                if task.cancelled():
                    _LOGGER.warning(
                        "UDHUB agent_update reload task was CANCELLED"
                    )
                elif task.exception() is not None:
                    _LOGGER.warning(
                        "UDHUB agent_update reload task RAISED: %s",
                        task.exception(),
                    )
                else:
                    _LOGGER.warning(
                        "UDHUB agent_update reload task COMPLETED"
                    )

            health_timeout = float(
                update_result.get("health_timeout_sec")
                or payload.get("health_timeout_sec")
                or 90
            )
            task = asyncio.Task(
                self_update_mod.schedule_reload_after_update(
                    self.hass,
                    self.entry,
                    target_version=str(update_result.get("version") or ""),
                    rollback_on_failure=True,
                    health_timeout_sec=health_timeout,
                ),
                context=contextvars.Context(),
            )
            task.add_done_callback(_log_reload_done)
            self._reload_task = task
            result["reloading"] = True
            result["restarting"] = False
            result["rollback_armed"] = True
            result["message"] = (
                "package applied; reloading integration "
                f"(auto-rollback if cloud health fails within {int(health_timeout)}s)"
            )

    async def _exec_registry_update(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Update entity/device name and/or area (doc 17 §5.3).

        Also supports area/floor registry CRUD for SaaS → HA room sync:
        action=area_create|area_delete|floor_create|floor_delete|area_set_floor
        """
        from homeassistant.helpers import entity_registry as er
        from homeassistant.helpers import area_registry as ar
        from homeassistant.helpers import device_registry as dr

        target = payload.get("target") or {}
        entity_id = target.get("entity_id") or payload.get("entity_id")
        device_id = target.get("device_id") or payload.get("device_id")
        new_name = payload.get("name")
        area_id = payload.get("area_id")
        area_name = payload.get("area_name") or payload.get("room")
        old_area_name = payload.get("old_area_name")
        action = str(payload.get("action") or "").strip().lower()
        floor_id = payload.get("floor_id")
        floor_name = payload.get("floor_name") or payload.get("floor")

        ent_reg = er.async_get(self.hass)
        area_reg = ar.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)

        floor_reg = None
        try:
            from homeassistant.helpers import floor_registry as fr

            floor_reg = fr.async_get(self.hass)
        except Exception:
            floor_reg = None

        def _find_area_id(name: str | None) -> str | None:
            if not name or area_reg is None:
                return None
            for area in area_reg.areas.values():
                if area.name == name:
                    return area.id
            return None

        def _find_floor_id(name: str | None) -> str | None:
            if not name or floor_reg is None:
                return None
            floors = getattr(floor_reg, "floors", {}) or {}
            for fl in floors.values():
                if getattr(fl, "name", None) == name:
                    return fl.floor_id
            return None

        # ── Area create ──────────────────────────────────────────
        if action in ("area_create", "create_area") or (
            not action
            and area_name
            and not entity_id
            and not device_id
            and not old_area_name
            and not area_id
        ):
            if area_reg is None:
                result["status"] = "failed"
                result["error"] = "area_registry_unavailable"
                return
            if not area_name:
                result["status"] = "failed"
                result["error"] = "area_name_required"
                return
            existing = _find_area_id(str(area_name))
            if existing:
                result["status"] = "ok"
                result["area_id"] = existing
                result["area_name"] = area_name
                result["created"] = False
            else:
                created = area_reg.async_create(str(area_name))
                result["status"] = "ok"
                result["area_id"] = created.id
                result["area_name"] = area_name
                result["created"] = True
            # optional floor assignment on create
            if floor_name and floor_reg is not None and result.get("area_id"):
                fid = _find_floor_id(str(floor_name))
                if not fid:
                    fl = floor_reg.async_create(str(floor_name))
                    fid = fl.floor_id
                try:
                    area_reg.async_update(result["area_id"], floor_id=fid)
                    result["floor_id"] = fid
                except TypeError:
                    pass
            return

        # ── Area delete ──────────────────────────────────────────
        if action in ("area_delete", "delete_area"):
            if area_reg is None:
                result["status"] = "failed"
                result["error"] = "area_registry_unavailable"
                return
            delete_id = area_id or _find_area_id(
                str(area_name) if area_name else None
            )
            if not delete_id:
                result["status"] = "failed"
                result["error"] = "area_not_found"
                return
            # Clear device/entity area refs first
            if dev_reg is not None:
                for dev in list(dev_reg.devices.values()):
                    if getattr(dev, "area_id", None) == delete_id:
                        try:
                            dev_reg.async_update_device(dev.id, area_id=None)
                        except Exception:
                            _LOGGER.debug(
                                "UDHUB clear device area failed", exc_info=True
                            )
            if ent_reg is not None:
                for ent in list(ent_reg.entities.values()):
                    if getattr(ent, "area_id", None) == delete_id:
                        try:
                            ent_reg.async_update_entity(ent.entity_id, area_id=None)
                        except Exception:
                            _LOGGER.debug(
                                "UDHUB clear entity area failed", exc_info=True
                            )
            area_reg.async_delete(delete_id)
            result["status"] = "ok"
            result["area_id"] = delete_id
            result["deleted"] = True
            return

        # ── Floor create ─────────────────────────────────────────
        if action in ("floor_create", "create_floor"):
            if floor_reg is None:
                result["status"] = "failed"
                result["error"] = "floor_registry_unavailable"
                return
            if not floor_name:
                result["status"] = "failed"
                result["error"] = "floor_name_required"
                return
            existing = _find_floor_id(str(floor_name))
            if existing:
                result["status"] = "ok"
                result["floor_id"] = existing
                result["floor_name"] = floor_name
                result["created"] = False
            else:
                created = floor_reg.async_create(str(floor_name))
                result["status"] = "ok"
                result["floor_id"] = created.floor_id
                result["floor_name"] = floor_name
                result["created"] = True
            return

        # ── Floor delete ─────────────────────────────────────────
        if action in ("floor_delete", "delete_floor"):
            if floor_reg is None:
                result["status"] = "failed"
                result["error"] = "floor_registry_unavailable"
                return
            delete_fid = floor_id or _find_floor_id(
                str(floor_name) if floor_name else None
            )
            if not delete_fid:
                result["status"] = "failed"
                result["error"] = "floor_not_found"
                return
            # Detach areas from floor
            if area_reg is not None:
                for area in list(area_reg.areas.values()):
                    if getattr(area, "floor_id", None) == delete_fid:
                        try:
                            area_reg.async_update(area.id, floor_id=None)
                        except TypeError:
                            pass
            floor_reg.async_delete(delete_fid)
            result["status"] = "ok"
            result["floor_id"] = delete_fid
            result["deleted"] = True
            return

        # ── Area set floor ───────────────────────────────────────
        if action in ("area_set_floor", "set_floor"):
            if area_reg is None:
                result["status"] = "failed"
                result["error"] = "area_registry_unavailable"
                return
            aid = area_id or _find_area_id(str(area_name) if area_name else None)
            if not aid:
                result["status"] = "failed"
                result["error"] = "area_not_found"
                return
            fid = floor_id
            if not fid and floor_name:
                if floor_reg is None:
                    result["status"] = "failed"
                    result["error"] = "floor_registry_unavailable"
                    return
                fid = _find_floor_id(str(floor_name))
                if not fid:
                    created = floor_reg.async_create(str(floor_name))
                    fid = created.floor_id
            try:
                area_reg.async_update(aid, floor_id=fid or None)
            except TypeError:
                result["status"] = "failed"
                result["error"] = "floor_assign_unsupported"
                return
            result["status"] = "ok"
            result["area_id"] = aid
            result["floor_id"] = fid
            return

        # Area-only rename (rename HA area registry entry)
        if area_name and not entity_id and not device_id and area_reg is not None:
            rename_id = area_id
            if not rename_id and old_area_name:
                for area in area_reg.areas.values():
                    if area.name == old_area_name:
                        rename_id = area.id
                        break
            if rename_id:
                area_entry = area_reg.async_get_area(rename_id)
                if area_entry is None:
                    result["status"] = "failed"
                    result["error"] = "area_not_found"
                    return
                area_reg.async_update(rename_id, name=area_name)
                result["status"] = "ok"
                result["area_id"] = rename_id
                result["area_name"] = area_name
                return
            if old_area_name:
                result["status"] = "failed"
                result["error"] = "area_not_found"
                return

        resolved_area_id = area_id
        if not resolved_area_id and area_name and area_reg is not None:
            # match existing area by name; create if missing
            found = None
            for area in area_reg.areas.values():
                if area.name == area_name:
                    found = area.id
                    break
            if found:
                resolved_area_id = found
            else:
                created = area_reg.async_create(area_name)
                resolved_area_id = created.id

        if entity_id and ent_reg is not None:
            entry = ent_reg.async_get(entity_id)
            if entry is None:
                result["status"] = "failed"
                result["error"] = "entity_not_found"
                return
            kwargs: dict[str, Any] = {}
            if new_name is not None:
                kwargs["name"] = new_name or None
            if resolved_area_id is not None:
                kwargs["area_id"] = resolved_area_id
            if kwargs:
                ent_reg.async_update_entity(entity_id, **kwargs)
            # also push area onto device when possible
            if entry.device_id and resolved_area_id and dev_reg is not None:
                dev_reg.async_update_device(entry.device_id, area_id=resolved_area_id)
            if entry.device_id and new_name and dev_reg is not None:
                # name_by_user is the user-facing device name
                try:
                    dev_reg.async_update_device(entry.device_id, name_by_user=new_name)
                except TypeError:
                    pass
            result["status"] = "ok"
            result["entity_id"] = entity_id
            result["area_id"] = resolved_area_id
            return

        if device_id and dev_reg is not None:
            kwargs = {}
            if new_name is not None:
                kwargs["name_by_user"] = new_name or None
            if resolved_area_id is not None:
                kwargs["area_id"] = resolved_area_id
            if not kwargs:
                result["status"] = "failed"
                result["error"] = "nothing_to_update"
                return
            dev_reg.async_update_device(device_id, **kwargs)
            result["status"] = "ok"
            result["device_id"] = device_id
            result["area_id"] = resolved_area_id
            return

        result["status"] = "failed"
        result["error"] = "missing_target"

    async def _exec_supervisor_probe(self, result: dict[str, Any]) -> None:
        if not self._require_supervisor_ops(result):
            return
        from . import supervisor_ops

        snap = await supervisor_ops.probe_supervisor(self.hass)
        result["supervisor"] = snap
        result["status"] = "ok" if snap.get("supported") else "failed"
        if not snap.get("supported"):
            result["error"] = str(snap.get("detail") or "unsupported")

    async def _exec_supervisor_core_restart(self, result: dict[str, Any]) -> None:
        if not self._require_supervisor_ops(result):
            return
        from . import supervisor_ops

        snap = await supervisor_ops.restart_core(self.hass)
        result["supervisor"] = snap
        result["status"] = "ok" if snap.get("status") == "ok" else "failed"
        if snap.get("error"):
            result["error"] = snap["error"]

    async def _exec_supervisor_backup_list(self, result: dict[str, Any]) -> None:
        if not self._require_supervisor_ops(result):
            return
        from . import supervisor_ops

        snap = await supervisor_ops.list_backups(self.hass)
        result["supervisor"] = snap
        result["status"] = "ok" if snap.get("supported") else "failed"
        if snap.get("detail") and not snap.get("supported"):
            result["error"] = str(snap.get("detail"))

    async def _exec_supervisor_backup_create(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not self._require_supervisor_ops(result):
            return
        from . import supervisor_ops

        name = payload.get("name")
        snap = await supervisor_ops.create_backup(
            self.hass, str(name) if name else None
        )
        result["supervisor"] = snap
        result["status"] = "ok" if snap.get("status") == "ok" else "failed"
        if snap.get("error"):
            result["error"] = snap["error"]

    async def _exec_config_write(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        payload: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Write cloud-generated YAML to config/packages/udhub*.yaml.

        Requires an active elevated session on the cloud side; the Agent only
        performs the filesystem operation and triggers reload services.
        """
        path = payload.get("path")
        content = payload.get("content")
        reload_services = payload.get("reload_services") or []
        delete_file = bool(
            payload.get("delete")
            or payload.get("op") == "delete"
            or payload.get("action") == "delete"
        )
        if not path:
            result["status"] = "failed"
            result["error"] = "missing_path_or_content"
            return
        if content is None and not delete_file:
            result["status"] = "failed"
            result["error"] = "missing_path_or_content"
            return

        # Security: only allow YAML under config/packages per write scope (PRD §5.4E/G)
        import os

        config_dir = os.path.realpath(self.hass.config.config_dir)
        candidate, relative = resolve_config_target(config_dir, str(path))
        if candidate is None or relative is None:
            result["status"] = "denied"
            result["error"] = "path_not_allowed"
            return

        write_scope = self._config_write_scope(payload)
        if not is_allowed_config_relative_path(relative, write_scope):
            result["status"] = "denied"
            result["error"] = "path_not_allowed"
            result["write_scope"] = write_scope
            return

        full_path = candidate

        if delete_file:
            removed = False
            if os.path.isfile(full_path):
                try:
                    os.remove(full_path)
                    removed = True
                except OSError as exc:
                    result["status"] = "failed"
                    result["error"] = f"delete_failed:{exc}"
                    result["path"] = relative
                    return
            services = list(reload_services) if isinstance(reload_services, list) else []
            if "homeassistant.reload_core_config" not in services:
                services.insert(0, "homeassistant.reload_core_config")
            reload_errors: list[str] = []
            for svc in services:
                if isinstance(svc, str) and "." in svc:
                    try:
                        domain, service = svc.split(".", 1)
                        await self.hass.services.async_call(
                            domain, service, {}, blocking=True
                        )
                    except Exception as exc:
                        _LOGGER.warning(
                            "UDHUB reload after delete failed: %s (%s)", svc, exc
                        )
                        reload_errors.append(f"{svc}:{exc}")
            result["status"] = "applied"
            result["path"] = relative
            result["deleted"] = True
            result["removed"] = removed
            eid = payload.get("entity_id")
            drop_ids: list[str] = []
            if eid:
                eid_s = str(eid)
                result["entity_id"] = eid_s
                drop_ids.append(eid_s)
                # packages 删文件后偶发仍留 state；再调 scene.delete 兜底
                try:
                    await self.hass.services.async_call(
                        "scene",
                        "delete",
                        {"entity_id": eid_s},
                        blocking=True,
                    )
                    result["scene_delete"] = "ok"
                except Exception as exc:
                    result["scene_delete"] = str(exc)
                # 从 entity registry 移除，避免 sync 从 registry 补回
                try:
                    from homeassistant.helpers import entity_registry as er

                    ereg = er.async_get(self.hass)
                    entry = ereg.async_get(eid_s) if ereg is not None else None
                    if entry is not None:
                        ereg.async_remove(eid_s)
                        result["registry_remove"] = "ok"
                    else:
                        result["registry_remove"] = "missing"
                except Exception as exc:
                    result["registry_remove"] = str(exc)
            if reload_errors:
                result["reload_errors"] = reload_errors
            try:
                await self._send_full_sync(
                    ws,
                    exclude_entity_ids=set(drop_ids),
                    removed_entity_ids=drop_ids,
                )
            except Exception:
                _LOGGER.debug(
                    "UDHUB sync after config_delete failed", exc_info=True
                )
            return

        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        # PRD 5.4E：写入前确保 packages include 已启用（幂等）
        try:
            from . import packages_setup

            pkg_snap = await packages_setup.ensure_packages(self.hass)
            result["packages"] = pkg_snap
            if pkg_snap.get("status") not in ("ready", "needs_restart"):
                result["status"] = "failed"
                result["error"] = "packages_not_ready"
                result["hint"] = pkg_snap.get("hint") or (
                    "configuration.yaml must enable "
                    "homeassistant.packages: !include_dir_named packages"
                )
                return
        except Exception as exc:
            _LOGGER.warning("UDHUB packages_ensure before write failed: %s", exc)
            result["packages_ensure_error"] = str(exc)

        # Doc 20 §4.2: keep previous content for rollback
        previous_content: str | None = None
        previous_existed = False
        if os.path.isfile(full_path):
            previous_existed = True
            try:
                with open(full_path, encoding="utf-8") as rf:
                    previous_content = rf.read()
            except OSError as exc:
                result["status"] = "failed"
                result["error"] = f"backup_read_failed:{exc}"
                return

        new_text = content if isinstance(content, str) else str(content)

        # B-pipeline: optional Supervisor full backup before overwrite (best-effort)
        pre_backup = payload.get("pre_backup")
        if pre_backup is None:
            pre_backup = True
        if pre_backup and not delete_file:
            try:
                from datetime import datetime, timezone

                from . import supervisor_ops

                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                bak = await supervisor_ops.create_backup(
                    self.hass, f"udhub-prewrite-{stamp}"
                )
                result["pre_backup"] = bak
                if bak.get("supported") is False:
                    result["pre_backup_skipped"] = bak.get("detail") or "no_supervisor"
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("UDHUB pre_backup failed (continuing write): %s", exc)
                result["pre_backup_error"] = str(exc)

        # write atomically
        tmp = f"{full_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new_text)
        os.replace(tmp, full_path)

        async def _restore_previous() -> None:
            if previous_existed and previous_content is not None:
                tmp_r = f"{full_path}.tmp"
                with open(tmp_r, "w", encoding="utf-8") as wf:
                    wf.write(previous_content)
                os.replace(tmp_r, full_path)
            elif not previous_existed and os.path.isfile(full_path):
                try:
                    os.remove(full_path)
                except OSError:
                    pass

        # check_config before reload (doc 20 §4.2)
        check_errors: list[str] = []
        try:
            from homeassistant.helpers.check_config import async_check_ha_config_file

            check_res = await async_check_ha_config_file(self.hass)
            if check_res is not None:
                err_attr = getattr(check_res, "errors", None)
                if err_attr:
                    check_errors = [str(e) for e in err_attr]
        except Exception as exc:
            _LOGGER.debug("check_config after write failed: %s", exc, exc_info=True)
            result["check_config_warning"] = str(exc)

        if check_errors:
            await _restore_previous()
            result["status"] = "failed"
            result["error"] = "config_check_failed"
            result["check_errors"] = check_errors[:20]
            result["rolled_back"] = True
            result["path"] = relative
            return

        # 新 package 文件必须 reload_core_config 才会进入 HA；仅 scene.reload 不够
        services = list(reload_services) if isinstance(reload_services, list) else []
        if "homeassistant.reload_core_config" not in services:
            services.insert(0, "homeassistant.reload_core_config")

        reload_errors: list[str] = []
        for svc in services:
            if isinstance(svc, str) and "." in svc:
                try:
                    domain, service = svc.split(".", 1)
                    await self.hass.services.async_call(
                        domain, service, {}, blocking=True
                    )
                except Exception as exc:
                    _LOGGER.warning("UDHUB reload service failed: %s (%s)", svc, exc)
                    reload_errors.append(f"{svc}:{exc}")

        # Domain health after reload (non-fatal)
        health: dict[str, Any] = {}
        for svc in services:
            if not (isinstance(svc, str) and "." in svc):
                continue
            domain = svc.split(".", 1)[0]
            if domain == "homeassistant":
                continue
            try:
                states = self.hass.states.async_entity_ids(domain)
                health[domain] = {"entity_count": len(states), "ok": True}
            except Exception as exc:  # noqa: BLE001
                health[domain] = {"ok": False, "error": str(exc)}
        if health:
            result["domain_health"] = health
            if any(not v.get("ok") for v in health.values()):
                result["domain_health_warning"] = True

        # 校验 scene package 是否真正注册（避免「落盘成功但 entity 不存在」）
        text = new_text
        scene_id = None
        friendly_name = None
        # list format: - id: udhub_xxx / name: "..."
        m_list = re.search(r"(?m)^\s{2}-\s+id:\s*(udhub_[a-z0-9_]+)\s*$", text)
        if m_list:
            scene_id = m_list.group(1)
        m_name = re.search(r"(?m)^\s{4}name:\s*[\"']?(.+?)[\"']?\s*$", text)
        if m_name:
            friendly_name = m_name.group(1).strip().strip("\"'")
        # dict format (legacy): scene:\n  udhub_xxx:
        if not scene_id:
            m_scene = re.search(r"(?m)^scene:\s*$", text)
            if m_scene:
                after = text[m_scene.end() :]
                m_dict = re.search(r"(?m)^\s{2}(udhub_[a-z0-9_]+)\s*:", after)
                if m_dict:
                    scene_id = m_dict.group(1)

        def _find_scene_entity() -> str | None:
            if scene_id:
                eid = f"scene.{scene_id}"
                if self.hass.states.get(eid) is not None:
                    return eid
            if friendly_name:
                for state in self.hass.states.async_all("scene"):
                    if (
                        state.name == friendly_name
                        or state.attributes.get("friendly_name") == friendly_name
                    ):
                        return state.entity_id
            return None

        if scene_id or friendly_name:
            registered_id = None
            for _ in range(16):
                registered_id = _find_scene_entity()
                if registered_id:
                    break
                await asyncio.sleep(0.35)
            if not registered_id:
                # 附带诊断，便于云端/控制台提示
                try:
                    from . import packages_setup

                    result["packages"] = await packages_setup.probe_packages(
                        self.hass
                    )
                except Exception:
                    pass
                try:
                    result["scene_entities_sample"] = sorted(
                        s.entity_id
                        for s in self.hass.states.async_all("scene")
                    )[:30]
                except Exception:
                    pass
                await _restore_previous()
                try:
                    await self.hass.services.async_call(
                        "homeassistant", "reload_core_config", {}, blocking=True
                    )
                    await self.hass.services.async_call(
                        "scene", "reload", {}, blocking=True
                    )
                except Exception:
                    pass
                result["status"] = "failed"
                result["error"] = "scene_not_registered"
                result["rolled_back"] = True
                result["path"] = relative
                if scene_id:
                    result["expected_entity"] = f"scene.{scene_id}"
                if friendly_name:
                    result["expected_name"] = friendly_name
                result["hint"] = (
                    "File written but scene entity missing. Ensure "
                    "configuration.yaml has: homeassistant: packages: "
                    "!include_dir_named packages  (flat packages/udhub_*.yaml). "
                    "Use list-format scene YAML (- id: udhub_…). "
                    "If packages include was just added, one HA restart may be required."
                )
                if reload_errors:
                    result["reload_errors"] = reload_errors
                return
            result["entity_id"] = registered_id

        result["status"] = "applied"
        result["path"] = relative
        result["check_config"] = "ok"
        if reload_errors:
            result["reload_errors"] = reload_errors
        # 推送最新 scene/automation 镜像到云端，控制台列表可立即看到
        try:
            await self._send_full_sync(ws)
        except Exception:
            _LOGGER.debug(
                "UDHUB sync after config_write failed", exc_info=True
            )

    # ── Supervisor Add-on / Host / OS ──────────────────────────────────

    async def _exec_supervisor_addon_list(self, result: dict[str, Any]) -> None:
        if not self._require_supervisor_ops(result):
            return
        from . import supervisor_ops
        snap = await supervisor_ops.list_addons(self.hass)
        result.update(snap)
        result["status"] = "ok" if snap.get("supported") else "failed"
        if snap.get("detail") and not snap.get("supported"):
            result["error"] = str(snap.get("detail"))

    async def _exec_supervisor_addon_install(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not self._require_supervisor_ops(result):
            return
        from . import supervisor_ops
        slug = str(payload.get("slug") or "")
        if not slug:
            result["status"] = "failed"
            result["error"] = "missing_slug"
            return
        snap = await supervisor_ops.install_addon(self.hass, slug)
        result.update(snap)
        if "status" not in snap:
            result["status"] = "ok" if snap.get("supported") else "failed"

    async def _exec_supervisor_addon_uninstall(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not self._require_supervisor_ops(result):
            return
        from . import supervisor_ops
        slug = str(payload.get("slug") or "")
        if not slug:
            result["status"] = "failed"
            result["error"] = "missing_slug"
            return
        snap = await supervisor_ops.uninstall_addon(self.hass, slug)
        result.update(snap)
        if "status" not in snap:
            result["status"] = "ok" if snap.get("supported") else "failed"

    async def _exec_supervisor_addon_start(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not self._require_supervisor_ops(result):
            return
        from . import supervisor_ops
        slug = str(payload.get("slug") or "")
        if not slug:
            result["status"] = "failed"
            result["error"] = "missing_slug"
            return
        snap = await supervisor_ops.start_addon(self.hass, slug)
        result.update(snap)
        if "status" not in snap:
            result["status"] = "ok" if snap.get("supported") else "failed"

    async def _exec_supervisor_addon_stop(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not self._require_supervisor_ops(result):
            return
        from . import supervisor_ops
        slug = str(payload.get("slug") or "")
        if not slug:
            result["status"] = "failed"
            result["error"] = "missing_slug"
            return
        snap = await supervisor_ops.stop_addon(self.hass, slug)
        result.update(snap)
        if "status" not in snap:
            result["status"] = "ok" if snap.get("supported") else "failed"

    async def _exec_supervisor_addon_restart(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not self._require_supervisor_ops(result):
            return
        from . import supervisor_ops
        slug = str(payload.get("slug") or "")
        if not slug:
            result["status"] = "failed"
            result["error"] = "missing_slug"
            return
        snap = await supervisor_ops.restart_addon(self.hass, slug)
        result.update(snap)
        if "status" not in snap:
            result["status"] = "ok" if snap.get("supported") else "failed"

    async def _exec_supervisor_host_info(self, result: dict[str, Any]) -> None:
        if not self._require_supervisor_ops(result):
            return
        from . import supervisor_ops
        snap = await supervisor_ops.host_info(self.hass)
        result.update(snap)
        if "status" not in snap:
            result["status"] = "ok" if snap.get("supported") else "failed"
        if snap.get("error"):
            result["error"] = snap["error"]

    async def _exec_supervisor_host_reboot(self, result: dict[str, Any]) -> None:
        if not self._require_supervisor_ops(result):
            return
        from . import supervisor_ops
        snap = await supervisor_ops.host_reboot(self.hass)
        result.update(snap)
        if "status" not in snap:
            result["status"] = "ok" if snap.get("supported") else "failed"

    async def _exec_supervisor_os_info(self, result: dict[str, Any]) -> None:
        if not self._require_supervisor_ops(result):
            return
        from . import supervisor_ops
        snap = await supervisor_ops.os_info(self.hass)
        result.update(snap)
        if "status" not in snap:
            result["status"] = "ok" if snap.get("supported") else "failed"
        if snap.get("error"):
            result["error"] = snap["error"]

    async def _exec_supervisor_os_update(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not self._require_supervisor_ops(result):
            return
        from . import supervisor_ops
        version = payload.get("version")
        snap = await supervisor_ops.os_update(self.hass, str(version) if version else None)
        result.update(snap)
        if "status" not in snap:
            result["status"] = "ok" if snap.get("supported") else "failed"

    # ── HA Core operations ─────────────────────────────────────────────

    async def _exec_ha_restart(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Restart HA Core via homeassistant.restart service."""
        if not self._require_extended_tier(payload, result):
            return
        try:
            await self.hass.services.async_call("homeassistant", "restart", {}, blocking=True)
            result["status"] = "ok"
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)

    async def _exec_ha_check_config(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Check HA configuration via homeassistant.check_config service."""
        if not self._require_extended_tier(payload, result):
            return
        try:
            await self.hass.services.async_call("homeassistant", "check_config", {}, blocking=True)
            result["status"] = "ok"
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)

    async def _exec_ha_reload_core(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Reload HA Core configuration."""
        if not self._require_extended_tier(payload, result):
            return
        try:
            await self.hass.services.async_call(
                "homeassistant", "reload_core_config", {}, blocking=True
            )
            result["status"] = "ok"
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)

    # ── File operations ────────────────────────────────────────────────

    def _resolve_config_path(self, path: str) -> tuple[str | None, str | None]:
        """Resolve a path within the HA config directory safely."""
        import os
        config_dir = os.path.realpath(self.hass.config.config_dir)
        candidate, relative = resolve_config_target(config_dir, path)
        if candidate is None or relative is None:
            return None, None
        # Security: prevent escaping config dir
        if not candidate.startswith(config_dir + os.sep) and candidate != config_dir:
            return None, None
        return candidate, relative

    async def _exec_file_read(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Read a file from the HA config directory."""
        import os
        if not self._require_extended_tier(payload, result):
            return
        path = payload.get("path") or ""
        if not path:
            result["status"] = "failed"
            result["error"] = "missing_path"
            return
        full, relative = self._resolve_config_path(path)
        if full is None or not self._validate_file_path(payload, relative or "", result, write=False):
            if result.get("status") != "denied":
                result["status"] = "denied"
                result["error"] = "path_not_allowed"
            return
        if not os.path.isfile(full):
            result["status"] = "failed"
            result["error"] = "file_not_found"
            return
        # Size limit: 1MB
        size = os.path.getsize(full)
        if size > 1_048_576:
            result["status"] = "failed"
            result["error"] = "file_too_large"
            result["size"] = size
            return
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            result["status"] = "ok"
            result["path"] = relative
            result["content"] = content
            result["size"] = size
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)

    async def _exec_file_write(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Write a file to the HA config directory."""
        import os
        if not self._require_extended_tier(payload, result):
            return
        path = payload.get("path") or ""
        content = payload.get("content")
        if not path or content is None:
            result["status"] = "failed"
            result["error"] = "missing_path_or_content"
            return
        full, relative = self._resolve_config_path(path)
        if full is None or not self._validate_file_path(payload, relative or "", result, write=True):
            if result.get("status") != "denied":
                result["status"] = "denied"
                result["error"] = "path_not_allowed"
            return
        try:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            tmp = f"{full}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content if isinstance(content, str) else str(content))
            os.replace(tmp, full)
            result["status"] = "ok"
            result["path"] = relative
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)

    async def _exec_file_delete(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Delete a file from the HA config directory."""
        import os
        if not self._require_extended_tier(payload, result):
            return
        path = payload.get("path") or ""
        if not path:
            result["status"] = "failed"
            result["error"] = "missing_path"
            return
        full, relative = self._resolve_config_path(path)
        if full is None or not self._validate_file_path(payload, relative or "", result, write=True):
            if result.get("status") != "denied":
                result["status"] = "denied"
                result["error"] = "path_not_allowed"
            return
        if not os.path.isfile(full):
            result["status"] = "failed"
            result["error"] = "file_not_found"
            return
        try:
            os.remove(full)
            result["status"] = "ok"
            result["path"] = relative
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)

    async def _exec_file_list(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """List files in a directory within the HA config directory."""
        import os
        if not self._require_extended_tier(payload, result):
            return
        path = payload.get("path") or "."
        full, relative = self._resolve_config_path(path)
        if full is None or not self._validate_file_path(
            payload, relative or ".", result, write=False, list_dir=True
        ):
            if result.get("status") != "denied":
                result["status"] = "denied"
                result["error"] = "path_not_allowed"
            return
        if not os.path.isdir(full):
            result["status"] = "failed"
            result["error"] = "not_a_directory"
            return
        try:
            entries = []
            dir_rel = relative or "."
            names = filter_visible_config_entries(
                dir_rel, sorted(os.listdir(full))[:500]
            )
            for name in names:
                entry_path = os.path.join(full, name)
                is_dir = os.path.isdir(entry_path)
                entry = {
                    "name": name,
                    "type": "directory" if is_dir else "file",
                }
                if not is_dir:
                    try:
                        entry["size"] = os.path.getsize(entry_path)
                    except Exception:
                        pass
                entries.append(entry)
            result["status"] = "ok"
            result["path"] = relative
            result["entries"] = entries
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)

    # ── Integration management ─────────────────────────────────────────

    async def _exec_integration_list(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """List all config entries (integrations)."""
        if not self._require_extended_tier(payload, result):
            return
        try:
            entries = self.hass.config_entries.async_entries()
            items = []
            for entry in entries:
                items.append({
                    "entry_id": entry.entry_id,
                    "domain": entry.domain,
                    "title": entry.title,
                    "state": str(entry.state) if entry.state else "unknown",
                    "source": entry.source,
                    "disabled_by": str(entry.disabled_by) if entry.disabled_by else None,
                })
            result["status"] = "ok"
            result["integrations"] = items
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)

    async def _exec_integration_reload_cmd(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Reload a specific integration by entry_id or domain."""
        if not self._require_extended_tier(payload, result):
            return
        entry_id = payload.get("entry_id")
        domain = payload.get("domain")
        try:
            if entry_id:
                entry = self.hass.config_entries.async_get_entry(entry_id)
                if not entry:
                    result["status"] = "failed"
                    result["error"] = "entry_not_found"
                    return
                await self.hass.config_entries.async_reload(entry_id)
                result["status"] = "ok"
                result["entry_id"] = entry_id
            elif domain:
                # Reload all entries for a domain
                entries = [
                    e for e in self.hass.config_entries.async_entries()
                    if e.domain == domain
                ]
                for entry in entries:
                    await self.hass.config_entries.async_reload(entry.entry_id)
                result["status"] = "ok"
                result["domain"] = domain
                result["reloaded_count"] = len(entries)
            else:
                result["status"] = "failed"
                result["error"] = "missing_entry_id_or_domain"
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)

    async def _exec_integration_remove(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Remove a config entry (uninstall integration)."""
        if not self._require_extended_tier(payload, result):
            return
        entry_id = payload.get("entry_id")
        if not entry_id:
            result["status"] = "failed"
            result["error"] = "missing_entry_id"
            return
        try:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if not entry:
                result["status"] = "failed"
                result["error"] = "entry_not_found"
                return
            await self.hass.config_entries.async_remove(entry_id)
            result["status"] = "ok"
            result["entry_id"] = entry_id
            result["domain"] = entry.domain
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)

    # ── User management ────────────────────────────────────────────────

    async def _exec_user_list(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """List all HA users (requires admin)."""
        if not self._require_extended_tier(payload, result):
            return
        try:
            users = await self.hass.auth.async_get_users()
            items = []
            for user in users:
                items.append({
                    "id": user.id,
                    "name": user.name,
                    "is_active": user.is_active,
                    "is_owner": user.is_owner,
                    "group_ids": [g.id for g in user.groups],
                    "local_only": getattr(user, "local_only", False),
                })
            result["status"] = "ok"
            result["users"] = items
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)

    async def _exec_user_create(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Create a new HA user."""
        if not self._require_extended_tier(payload, result):
            return
        name = payload.get("name")
        password = payload.get("password")
        if not name:
            result["status"] = "failed"
            result["error"] = "missing_name"
            return
        try:
            user = await self.hass.auth.async_create_user(name, local_only=False)
            if password:
                await self.hass.auth.auth_providers[0].async_validate_login(name, password) \
                    if False else None  # just create the user
                # Add local credential
                from homeassistant.auth.providers import homeassistant as hass_auth
                provider = None
                for p in self.hass.auth.auth_providers:
                    if isinstance(p, hass_auth.HassAuthProvider):
                        provider = p
                        break
                if provider:
                    data = provider.data
                    data.add_auth(user.id, name, password)
                    data.async_save()
            result["status"] = "ok"
            result["user_id"] = user.id
            result["name"] = name
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)

    async def _exec_user_delete(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Delete a HA user by ID."""
        if not self._require_extended_tier(payload, result):
            return
        user_id = payload.get("user_id")
        if not user_id:
            result["status"] = "failed"
            result["error"] = "missing_user_id"
            return
        try:
            users = await self.hass.auth.async_get_users()
            target = None
            for u in users:
                if u.id == user_id:
                    target = u
                    break
            if not target:
                result["status"] = "failed"
                result["error"] = "user_not_found"
                return
            # Prevent deleting the owner
            if target.is_owner:
                result["status"] = "denied"
                result["error"] = "cannot_delete_owner"
                return
            await self.hass.auth.async_remove_user(target)
            result["status"] = "ok"
            result["user_id"] = user_id
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)

    # ── Helpers / Groups / Labels (Agent≈HA object surface) ─────────────

    async def _exec_helper_list(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not self._require_extended_tier(payload, result):
            return
        from . import ha_objects

        snap = await ha_objects.list_helpers(self.hass)
        result.update(snap)

    async def _exec_helper_create(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not self._require_extended_tier(payload, result):
            return
        from . import ha_objects

        snap = await ha_objects.create_helper(self.hass, payload)
        result.update(snap)

    async def _exec_helper_update(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not self._require_extended_tier(payload, result):
            return
        from . import ha_objects

        snap = await ha_objects.update_helper(self.hass, payload)
        result.update(snap)

    async def _exec_helper_delete(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not self._require_extended_tier(payload, result):
            return
        from . import ha_objects

        snap = await ha_objects.delete_helper(self.hass, payload)
        result.update(snap)

    async def _exec_group_list(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not self._require_extended_tier(payload, result):
            return
        from . import ha_objects

        snap = await ha_objects.list_groups(self.hass)
        result.update(snap)

    async def _exec_group_upsert(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not self._require_extended_tier(payload, result):
            return
        from . import ha_objects

        snap = await ha_objects.upsert_group(self.hass, payload)
        result.update(snap)

    async def _exec_group_delete(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not self._require_extended_tier(payload, result):
            return
        from . import ha_objects

        snap = await ha_objects.delete_group(self.hass, payload)
        result.update(snap)

    async def _exec_label_list(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not self._require_extended_tier(payload, result):
            return
        from . import ha_objects

        snap = await ha_objects.list_labels(self.hass)
        result.update(snap)

    async def _exec_label_create(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not self._require_extended_tier(payload, result):
            return
        from . import ha_objects

        snap = await ha_objects.create_label(self.hass, payload)
        result.update(snap)

    async def _exec_label_delete(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not self._require_extended_tier(payload, result):
            return
        from . import ha_objects

        snap = await ha_objects.delete_label(self.hass, payload)
        result.update(snap)

    async def _exec_label_assign(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        if not self._require_extended_tier(payload, result):
            return
        from . import ha_objects

        snap = await ha_objects.assign_labels(self.hass, payload)
        result.update(snap)

    # ── Shell command ──────────────────────────────────────────────────

    async def _exec_shell_command(
        self, payload: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Execute a shell command (extended tier only)."""
        if self._effective_capability_tier(payload) != CAPABILITY_TIER_EXTENDED:
            result["status"] = "denied"
            result["error"] = "requires_extended_tier"
            return
        command = payload.get("command") or ""
        if not command:
            result["status"] = "failed"
            result["error"] = "missing_command"
            return
        # Security: block dangerous commands
        dangerous = ["rm -rf /", "mkfs", "dd if=", ":(){", "fork bomb"]
        for d in dangerous:
            if d in command.lower():
                result["status"] = "denied"
                result["error"] = "dangerous_command_blocked"
                return
        try:
            import subprocess
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.hass.config.config_dir,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=110)
            result["status"] = "ok"
            result["exit_code"] = proc.returncode
            result["stdout"] = stdout.decode("utf-8", errors="replace")[:10000]
            result["stderr"] = stderr.decode("utf-8", errors="replace")[:5000]
        except asyncio.TimeoutError:
            result["status"] = "failed"
            result["error"] = "command_timeout"
        except Exception as exc:
            result["status"] = "failed"
            result["error"] = str(exc)


async def async_create_cloud_client(hass: HomeAssistant, entry: ConfigEntry) -> UdhubCloudClient:
    """Factory helper."""
    client = UdhubCloudClient(hass, entry)
    await client.start()
    return client
