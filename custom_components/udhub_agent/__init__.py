"""UDHUB · 云枢 HA Agent.

Install: copy into HA `custom_components/`, restart, add integration.
Setup finishes locally after showing the activation code; SaaS claim
completes in the background (doc/19 Mode A, frictionless host add).
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .claim_poll import poll_activation_once
from .cloud_client import UdhubCloudClient
from .notify import create_notification
from .const import (
    CLAIM_POLL_INTERVAL_SEC,
    CLAIM_STATUS_CLAIMED,
    CLAIM_STATUS_PENDING,
    CLAIM_WAIT_TIMEOUT_SEC,
    CONF_ACCESS_TOKEN,
    CONF_ACTIVATION_ID,
    CONF_ACTIVATION_KEY,
    CONF_CLAIM_STATUS,
    CONF_CLOUD_URL,
    CONF_GATEWAY_ID,
    CONF_REFRESH_TOKEN,
    CONF_SETUP_ID,
    CONF_USER_CODE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = []


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    client = UdhubCloudClient(hass, entry)
    hass.data[DOMAIN][entry.entry_id] = {
        "gateway_id": entry.data.get(CONF_GATEWAY_ID),
        "cloud_url": entry.data.get(CONF_CLOUD_URL),
        "client": client,
        "claim_task": None,
    }

    if entry.data.get(CONF_ACCESS_TOKEN) and entry.data.get(CONF_GATEWAY_ID):
        await client.start()
    else:
        coro = _wait_for_saas_claim(hass, entry)
        name = f"udhub_claim_{entry.entry_id}"
        if hasattr(hass, "async_create_background_task"):
            task = hass.async_create_background_task(coro, name)
        else:
            task = hass.async_create_task(coro)
        hass.data[DOMAIN][entry.entry_id]["claim_task"] = task

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    entry_data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if entry_data:
        claim_task = entry_data.get("claim_task")
        if claim_task and not claim_task.done():
            claim_task.cancel()
        client = entry_data.get("client")
        if isinstance(client, UdhubCloudClient):
            await client.stop()
    return unload_ok


async def _wait_for_saas_claim(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Poll cloud until claimed, then update entry and start WS."""
    cloud_url = str(entry.data.get(CONF_CLOUD_URL) or "").rstrip("/")
    activation_id = entry.data.get(CONF_ACTIVATION_ID)
    activation_key = str(
        entry.data.get(CONF_ACTIVATION_KEY)
        or entry.data.get(CONF_USER_CODE)
        or ""
    ).strip()
    if not cloud_url or not (activation_id or activation_key):
        _LOGGER.error("UDHUB pending entry missing cloud_url/activation")
        return

    session = async_get_clientsession(hass)
    elapsed = 0
    _LOGGER.info(
        "UDHUB waiting for SaaS claim code=%s id=%s",
        activation_key,
        activation_id,
    )

    while CLAIM_WAIT_TIMEOUT_SEC <= 0 or elapsed <= CLAIM_WAIT_TIMEOUT_SEC:
        data = await poll_activation_once(
            session,
            cloud_url,
            activation_id=str(activation_id) if activation_id else None,
            activation_key=activation_key or None,
        )
        if data and data.get("activation_id") and not activation_id:
            activation_id = data["activation_id"]

        if data and data.get("status") == "claimed":
            gateway_id = data["gateway_id"]
            new_data = {
                **entry.data,
                CONF_GATEWAY_ID: gateway_id,
                CONF_ACCESS_TOKEN: data["access_token"],
                CONF_REFRESH_TOKEN: data.get("refresh_token"),
                CONF_ACTIVATION_ID: data.get("activation_id") or activation_id,
                CONF_CLAIM_STATUS: CLAIM_STATUS_CLAIMED,
            }
            hass.config_entries.async_update_entry(
                entry,
                data=new_data,
                title=f"UDHUB {gateway_id}",
            )
            setup_id = entry.data.get(CONF_SETUP_ID) or gateway_id
            await create_notification(
                hass,
                title="UDHUB Agent 已连接云枢",
                message=(
                    f"主机 **{gateway_id}** 已由控制台认领并自动接入。\n\n"
                    f"设备码：**{activation_key}**"
                ),
                notification_id=f"{DOMAIN}_setup_{setup_id}",
            )
            _LOGGER.info("UDHUB SaaS claim complete gateway=%s", gateway_id)
            await hass.config_entries.async_reload(entry.entry_id)
            return

        await asyncio.sleep(CLAIM_POLL_INTERVAL_SEC)
        if CLAIM_WAIT_TIMEOUT_SEC > 0:
            elapsed += CLAIM_POLL_INTERVAL_SEC

    if CLAIM_WAIT_TIMEOUT_SEC <= 0:
        return

    _LOGGER.warning(
        "UDHUB claim wait timed out after %ss code=%s",
        CLAIM_WAIT_TIMEOUT_SEC,
        activation_key,
    )
    await create_notification(
        hass,
        title="UDHUB Agent 认领超时",
        message=(
            f"设备码 **{activation_key}** 在约 "
            f"{CLAIM_WAIT_TIMEOUT_SEC // 60} 分钟内未被认领。\n\n"
            "请在云枢重新认领，或于「UDHUB Agent → 配置」重新生成激活码。"
        ),
        notification_id=f"{DOMAIN}_claim_timeout_{entry.entry_id}",
    )
