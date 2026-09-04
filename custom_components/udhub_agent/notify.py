"""HA persistent notification helper (sync/async API compatible)."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant


async def create_notification(hass: HomeAssistant, **kwargs: Any) -> None:
    """Create a notification; works across HA versions."""
    result = persistent_notification.async_create(hass, **kwargs)
    if asyncio.iscoroutine(result):
        await result
