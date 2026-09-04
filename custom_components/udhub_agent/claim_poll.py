"""Poll cloud until activation is claimed (used after HA finishes setup)."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)


def unwrap_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Nest cloud-api wraps integrator APIs; agent routes return bare JSON."""
    if isinstance(data.get("data"), dict) and "activation_id" not in data:
        return data["data"]
    return data


async def poll_activation_once(
    session: aiohttp.ClientSession,
    cloud_url: str,
    *,
    activation_id: str | None,
    activation_key: str | None,
) -> dict[str, Any] | None:
    """One poll by activation_id or code. Returns payload or None."""
    base = cloud_url.rstrip("/")
    try:
        if activation_id:
            async with session.post(
                f"{base}/api/v1/agent/activation/{activation_id}/poll",
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status >= 400:
                    return None
                return unwrap_payload(await resp.json())

        if not activation_key:
            return None

        async with session.post(
            f"{base}/api/v1/agent/activation/poll-by-code",
            json={"activation_code": activation_key},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status >= 400:
                return None
            return unwrap_payload(await resp.json())
    except Exception as exc:
        _LOGGER.debug("claim poll failed: %s", exc)
        return None
