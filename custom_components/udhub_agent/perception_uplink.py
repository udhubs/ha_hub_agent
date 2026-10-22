"""Edge → cloud perception.event.v1 uplink (doc 14 §3.5).

State-branch (ha_entity) only: motion / door / occupancy / person flips.
Uses HTTP POST /api/v1/perception/events with gateway access_token — not WSS.
Wave 0: area from HA area registry; rising + falling edges (entry/exit).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import aiohttp
from homeassistant.const import EVENT_STATE_CHANGED, STATE_HOME, STATE_ON, STATE_OPEN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import AGENT_VERSION
from .perception_mqtt import PerceptionMqttBridge

_LOGGER = logging.getLogger(__name__)

# Domains / device_class we promote to cognitive events
_BINARY_CLASSES = {
    "motion",
    "occupancy",
    "presence",
    "door",
    "garage_door",
    "window",
    "opening",
}
_ACTIVE_STATES = {STATE_ON, STATE_OPEN, STATE_HOME, "detected", "open", "occupied"}
_INACTIVE_STATES = {"off", "closed", "away", "not_home", "clear", "false"}

FLUSH_INTERVAL_SEC = 12
BATCH_MAX = 40
COOLDOWN_SEC = 45  # per entity_id + event_type


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_RADAR_RE = re.compile(
    r"mmwave|radar|ld2410|presence.*radar|aqara.*fp2|everything.?presence|毫米波",
    re.I,
)


def _is_radar(entity_id: str, attrs: dict[str, Any]) -> bool:
    blob = f"{entity_id} {attrs.get('friendly_name', '')} {attrs}"
    return bool(_RADAR_RE.search(blob))


def _event_type(
    domain: str,
    device_class: str | None,
    *,
    rising: bool,
    radar: bool = False,
) -> str:
    dc = (device_class or "").lower()
    if radar and rising:
        return "person.presence.still"
    if domain == "person" or dc in ("occupancy", "presence", "motion"):
        return "person.presence" if rising else "person.exit"
    if dc in ("door", "garage_door", "window", "opening"):
        return "person.entry" if rising else "person.exit"
    return f"environment.{domain or 'state'}"


def _confidence(domain: str, device_class: str | None, *, radar: bool = False) -> float:
    if radar:
        return 0.9
    dc = (device_class or "").lower()
    if domain == "binary_sensor" and dc in ("motion", "occupancy", "door"):
        return 0.82
    if domain == "person":
        return 0.78
    return 0.7


class PerceptionUplink:
    """Buffer state-change inferences and POST to cloud."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        cloud_url: str,
        gateway_id: str,
        access_token: str,
    ) -> None:
        self.hass = hass
        self.cloud_url = cloud_url.rstrip("/")
        self.gateway_id = gateway_id
        self.access_token = access_token
        self._queue: list[dict[str, Any]] = []
        self._last_emit: dict[str, float] = {}
        self._unsub = None
        self._flush_task: asyncio.Task | None = None
        self._closing = False
        self._mqtt = PerceptionMqttBridge(hass)

    def start(self) -> None:
        if self._unsub is not None:
            return
        self._closing = False

        @callback
        def _on_state(event: Event) -> None:
            if self._closing:
                return
            self._maybe_enqueue(event)

        self._unsub = self.hass.bus.async_listen(EVENT_STATE_CHANGED, _on_state)
        self._flush_task = asyncio.create_task(self._flush_loop())
        _LOGGER.info("UDHUB perception uplink started gateway=%s", self.gateway_id)

    async def stop(self) -> None:
        self._closing = True
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush(force=True)

    def _resolve_area(self, entity_id: str, attrs: dict[str, Any]) -> str:
        """Prefer HA area name over friendly_name."""
        try:
            ent_reg = er.async_get(self.hass)
            area_reg = ar.async_get(self.hass)
            entry = ent_reg.async_get(entity_id)
            area_id = entry.area_id if entry else None
            if not area_id and entry and entry.device_id:
                dev = dr.async_get(self.hass).async_get(entry.device_id)
                if dev:
                    area_id = dev.area_id
            if area_id:
                area = area_reg.async_get_area(area_id)
                if area and area.name:
                    return str(area.name)[:64]
                return str(area_id)[:64]
        except Exception:  # noqa: BLE001
            _LOGGER.debug("area resolve failed for %s", entity_id, exc_info=True)
        return str(attrs.get("friendly_name") or entity_id)[:64]

    def _maybe_enqueue(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if not new_state:
            return
        entity_id = str(new_state.entity_id or "")
        if not entity_id or "." not in entity_id:
            return
        domain = entity_id.split(".", 1)[0]
        if domain not in ("binary_sensor", "person", "lock", "input_boolean"):
            return

        attrs = dict(new_state.attributes or {})
        device_class = attrs.get("device_class")
        if domain == "binary_sensor":
            dc = str(device_class or "").lower()
            if dc and dc not in _BINARY_CLASSES:
                return
            if not dc:
                hint = entity_id.lower()
                if not any(
                    k in hint
                    for k in ("motion", "occupancy", "presence", "door", "window", "pir")
                ):
                    return

        new_s = str(new_state.state or "").lower()
        old_s = str(old_state.state or "").lower() if old_state else ""
        active = {s.lower() for s in _ACTIVE_STATES}
        inactive = _INACTIVE_STATES

        rising = new_s in active and (not old_s or old_s not in active)
        falling = new_s in inactive and old_s in active
        if not rising and not falling:
            return
        if old_s and old_s == new_s:
            return

        radar = _is_radar(entity_id, attrs)
        evt_type = _event_type(
            domain,
            str(device_class) if device_class else None,
            rising=rising,
            radar=radar,
        )
        cooldown_key = f"{entity_id}::{evt_type}"
        now = time.monotonic()
        last = self._last_emit.get(cooldown_key, 0.0)
        if now - last < COOLDOWN_SEC:
            return
        self._last_emit[cooldown_key] = now

        area = self._resolve_area(entity_id, attrs)
        source_kind = "radar" if radar else "ha_entity"
        evt = {
            "schema": "perception.event.v1",
            "event_id": f"pev_{uuid.uuid4().hex[:12]}",
            "gateway_id": self.gateway_id,
            "source_id": entity_id,
            "source_kind": source_kind,
            "source_class": str(device_class or domain),
            "ts_start": _now_iso(),
            "type": evt_type,
            "confidence": _confidence(
                domain,
                str(device_class) if device_class else None,
                radar=radar,
            ),
            "location": {"area": area},
            "attributes": {
                "entity_id": entity_id,
                "state": new_s,
                "from_state": old_s or None,
                "edge": "rising" if rising else "falling",
                **({"motion": "still"} if radar and rising else {}),
            },
            "evidence": None,
            "producer": {
                "agent_kind": "udhub_agent",
                "version": AGENT_VERSION,
                "model": "state-infer-v0",
            },
            "model_version": "state-infer-v0",
            "rulepack_version": None,
            "source_registry_version": None,
            "evidence_summary": f"HA {entity_id} {old_s or '—'}→{new_s} ({area})",
            "flags": ["signal_branch"] if radar else ["state_branch"],
        }
        self._queue.append(evt)
        # 双出口：本地 MQTT 优先（断网联动），再批量上云
        asyncio.create_task(self._mqtt.publish_event(evt))
        if len(self._queue) >= BATCH_MAX:
            asyncio.create_task(self._flush(force=True))

    async def _flush_loop(self) -> None:
        try:
            while not self._closing:
                await asyncio.sleep(FLUSH_INTERVAL_SEC)
                await self._flush(force=False)
        except asyncio.CancelledError:
            return

    async def _flush(self, *, force: bool) -> None:
        if not self._queue:
            return
        if not force and len(self._queue) < 1:
            return
        batch = self._queue[:BATCH_MAX]
        del self._queue[: len(batch)]
        url = f"{self.cloud_url}/api/v1/perception/events"
        payload = {"gateway_id": self.gateway_id, "events": batch}
        try:
            session = async_get_clientsession(self.hass)
            async with session.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    _LOGGER.warning(
                        "UDHUB perception uplink failed status=%s body=%s",
                        resp.status,
                        text[:200],
                    )
                    if resp.status >= 500:
                        self._queue = batch + self._queue
                else:
                    _LOGGER.debug(
                        "UDHUB perception uplink ok n=%s gateway=%s",
                        len(batch),
                        self.gateway_id,
                    )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("UDHUB perception uplink error", exc_info=True)
            self._queue = batch + self._queue
