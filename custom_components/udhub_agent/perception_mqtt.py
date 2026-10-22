"""I-3：Agent → HA MQTT event 实体（与侧车 ha_bridge 同 topic 约定）。

有 MQTT 集成时发布；无则静默跳过。断网时仍可本地联动。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

EVENT_TYPES = [
    "person.presence",
    "person.presence.still",
    "person.entry",
    "person.exit",
    "environment.scene",
    "appliance.cooking",
]

STATUS_TOPIC = "udhub/perception/status"


def sanitize_area(area: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]+", "_", (area or "unknown").strip().lower())
    return safe.strip("_") or "unknown"


def object_id_for_area(area: str) -> str:
    return f"udhub_{sanitize_area(area)}_perception"


def state_topic_for_area(area: str) -> str:
    return f"udhub/perception/{sanitize_area(area)}/event"


def discovery_topic_for_area(area: str) -> str:
    return f"homeassistant/event/{object_id_for_area(area)}/config"


def discovery_payload(area: str) -> dict[str, Any]:
    oid = object_id_for_area(area)
    return {
        "name": f"云枢感知 · {area}",
        "unique_id": oid,
        "object_id": oid,
        "state_topic": state_topic_for_area(area),
        "event_types": list(EVENT_TYPES),
        "availability_topic": STATUS_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": {
            "identifiers": ["udhub_perception"],
            "name": "UDHUB Perception",
            "manufacturer": "UDHUB",
            "model": "ha-bridge",
        },
    }


def event_state_payload(ev: dict[str, Any]) -> dict[str, Any]:
    loc = ev.get("location") if isinstance(ev.get("location"), dict) else {}
    return {
        "event_type": str(ev.get("type") or "environment.scene"),
        "confidence": ev.get("confidence"),
        "source_id": ev.get("source_id"),
        "source_kind": ev.get("source_kind"),
        "event_id": ev.get("event_id"),
        "summary": ev.get("evidence_summary") or "",
        "area": (loc or {}).get("area"),
        "flags": ev.get("flags") or [],
    }


class PerceptionMqttBridge:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._discovered: set[str] = set()
        self._warned_missing = False
        self.published = 0

    def _mqtt_ready(self) -> bool:
        return "mqtt" in self.hass.config.components

    async def publish_event(self, ev: dict[str, Any]) -> bool:
        if not self._mqtt_ready():
            if not self._warned_missing:
                _LOGGER.debug(
                    "UDHUB perception MQTT skip: mqtt integration not loaded"
                )
                self._warned_missing = True
            return False
        try:
            from homeassistant.components import mqtt
        except ImportError:
            return False

        loc = ev.get("location") if isinstance(ev.get("location"), dict) else {}
        area = str((loc or {}).get("area") or "unknown")
        key = sanitize_area(area)
        try:
            if key not in self._discovered:
                await mqtt.async_publish(
                    self.hass,
                    discovery_topic_for_area(area),
                    json.dumps(discovery_payload(area), ensure_ascii=False),
                    qos=0,
                    retain=True,
                )
                await mqtt.async_publish(
                    self.hass,
                    STATUS_TOPIC,
                    "online",
                    qos=0,
                    retain=True,
                )
                self._discovered.add(key)
            await mqtt.async_publish(
                self.hass,
                state_topic_for_area(area),
                json.dumps(event_state_payload(ev), ensure_ascii=False),
                qos=0,
                retain=False,
            )
            self.published += 1
            return True
        except Exception:  # noqa: BLE001
            _LOGGER.debug("UDHUB perception MQTT publish failed", exc_info=True)
            return False
