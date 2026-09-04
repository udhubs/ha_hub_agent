"""Config Flow: Agent 出示码 → 直接完成；云端认领后后台自动连上。

产品口径（无感加主机）：
1. Agent 本地生成 HomeKit 风格 8 位码 + QR，上报云端
2. HA 展示标签后即可「完成」写入待认领配置（无需再确认）
3. 集成商在控制台「认领项目」完成云端校验
4. Agent 后台 poll 到 claimed 后写入 Token 并连接 WS
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.const import __version__ as HA_VERSION

from .activation import create_activation_session
from .claim_poll import poll_activation_once, unwrap_payload
from .notify import create_notification
from .const import (
    BIND_SOURCE_CLOUD,
    BIND_SOURCE_LOCAL,
    CLAIM_STATUS_CLAIMED,
    CLAIM_STATUS_PENDING,
    CONF_ACCESS_TOKEN,
    CONF_ACTIVATION_ID,
    CONF_ACTIVATION_KEY,
    CONF_BIND_SOURCE,
    CONF_CLAIM_STATUS,
    CONF_CLOUD_URL,
    CONF_GATEWAY_ID,
    CONF_QR_PAYLOAD,
    CONF_REFRESH_TOKEN,
    CONF_SETUP_ID,
    CONF_USER_CODE,
    DEFAULT_CLOUD_URL,
    DOMAIN,
    AGENT_VERSION,
)
from .label_ui import (
    activation_label_schema,
    activation_key_from_entry,
    ensure_qr_payload,
    finish_step_schema,
    label_description_placeholders,
    options_bind_state,
    options_init_schema,
    qr_payload_from_entry,
)

_LOGGER = logging.getLogger(__name__)


def _pending_entry_data(
    *,
    cloud_url: str,
    activation_key: str,
    setup_id: str,
    qr_payload: str | None,
    activation_id: str | None,
    bind_source: str = BIND_SOURCE_LOCAL,
) -> dict[str, Any]:
    return {
        CONF_CLOUD_URL: cloud_url,
        CONF_USER_CODE: activation_key,
        CONF_ACTIVATION_KEY: activation_key,
        CONF_SETUP_ID: setup_id,
        CONF_QR_PAYLOAD: qr_payload,
        CONF_ACTIVATION_ID: activation_id,
        CONF_CLAIM_STATUS: CLAIM_STATUS_PENDING,
        CONF_BIND_SOURCE: bind_source,
    }


def _claimed_entry_data(
    *,
    cloud_url: str,
    claimed: dict[str, Any],
    activation_key: str,
    setup_id: str | None,
    qr_payload: str | None,
    activation_id: str | None,
    bind_source: str | None = None,
) -> dict[str, Any]:
    data = {
        CONF_CLOUD_URL: cloud_url,
        CONF_GATEWAY_ID: claimed["gateway_id"],
        CONF_ACCESS_TOKEN: claimed["access_token"],
        CONF_REFRESH_TOKEN: claimed.get("refresh_token"),
        CONF_USER_CODE: activation_key,
        CONF_ACTIVATION_KEY: activation_key,
        CONF_SETUP_ID: setup_id,
        CONF_QR_PAYLOAD: qr_payload,
        CONF_ACTIVATION_ID: activation_id or claimed.get("activation_id"),
        CONF_CLAIM_STATUS: CLAIM_STATUS_CLAIMED,
    }
    if bind_source:
        data[CONF_BIND_SOURCE] = bind_source
    return data


async def _report_activation_to_cloud(
    session: aiohttp.ClientSession,
    cloud_url: str,
    session_data: dict[str, Any],
    phone: str | None,
) -> str | None:
    """Report locally generated activation to cloud; return activation_id."""
    async with session.post(
        f"{cloud_url}/api/v1/agent/activations/report",
        json={
            "activation_key": session_data["activation_key"],
            "setup_id": session_data["setup_id"],
            "qr_payload": session_data["qr_payload"],
            "phone": phone,
            "agent_version": session_data["agent_version"],
            "ha_version": session_data["ha_version"],
            "install_type": session_data["install_type"],
            "key_version": session_data["key_version"],
            "expires_in": session_data["expires_in"],
        },
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        data = unwrap_payload(await resp.json())
        if resp.status >= 400:
            _LOGGER.warning("Agent activation report failed: %s", data)
            return None
        return data.get("activation_id")


class UdhubAgentConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._cloud_url = DEFAULT_CLOUD_URL
        self._activation_id: str | None = None
        self._activation_key: str | None = None
        self._qr_payload: str | None = None
        self._setup_id: str | None = None
        self._phone: str | None = None
        self._bind_mode = "show"

    def _existing_entry(self) -> config_entries.ConfigEntry | None:
        """Entry being updated during reauth / reconfigure."""
        if self.source not in (
            config_entries.SOURCE_REAUTH,
            config_entries.SOURCE_RECONFIGURE,
        ):
            return None
        entry_id = self.context.get("entry_id")
        if not entry_id:
            return None
        return self.hass.config_entries.async_get_entry(entry_id)

    def _prefill_from_entry(self, entry: config_entries.ConfigEntry) -> None:
        self._cloud_url = str(
            entry.data.get(CONF_CLOUD_URL) or DEFAULT_CLOUD_URL
        ).rstrip("/")

    @staticmethod
    @config_entries.callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return UdhubAgentOptionsFlowHandler()

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            self._cloud_url = user_input[CONF_CLOUD_URL].rstrip("/")
            self._phone = (user_input.get("phone") or "").strip() or None
            self._bind_mode = user_input.get("bind_mode") or "show"
            cloud_code = (user_input.get("activation_code") or "").strip().upper()

            session = async_get_clientsession(self.hass)
            try:
                if self._bind_mode == "enter":
                    if not cloud_code:
                        errors["activation_code"] = "required"
                    else:
                        async with session.post(
                            f"{self._cloud_url}/api/v1/agent/activation/bind",
                            json={
                                "activation_code": cloud_code,
                                "phone": self._phone,
                                "agent_version": AGENT_VERSION,
                                "ha_version": HA_VERSION,
                                "install_type": "Home Assistant OS",
                                "key_version": "v2",
                            },
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as resp:
                            data = unwrap_payload(await resp.json())
                            if resp.status >= 400:
                                errors["base"] = "cannot_connect"
                            else:
                                self._activation_id = data["activation_id"]
                                self._activation_key = (
                                    data.get("activation_key") or cloud_code
                                )
                                self._qr_payload = ensure_qr_payload(
                                    self._activation_key,
                                    data.get("qr_payload"),
                                    self._cloud_url,
                                    data.get("setup_id"),
                                )
                                self._setup_id = data.get("setup_id")
                                if (
                                    data.get("auto_claimed")
                                    or data.get("status") == "claimed"
                                ):
                                    claimed = await poll_activation_once(
                                        session,
                                        self._cloud_url,
                                        activation_id=self._activation_id,
                                        activation_key=self._activation_key,
                                    )
                                    if claimed and claimed.get("status") == "claimed":
                                        return await self._create_claimed_entry(
                                            claimed
                                        )
                                return await self.async_step_activation()
                else:
                    session_data = create_activation_session(
                        cloud_url=self._cloud_url,
                        agent_version=AGENT_VERSION,
                        ha_version=HA_VERSION,
                        install_type="Home Assistant OS",
                    )
                    self._activation_key = session_data["activation_key"]
                    self._qr_payload = session_data["qr_payload"]
                    self._setup_id = session_data["setup_id"]
                    self._activation_id = await _report_activation_to_cloud(
                        session, self._cloud_url, session_data, self._phone
                    )
                    return await self.async_step_activation()
            except Exception:
                _LOGGER.exception("UDHUB config user step failed")
                errors["base"] = "cannot_connect"

        schema = vol.Schema(
            {
                vol.Required(CONF_CLOUD_URL, default=self._cloud_url): str,
                vol.Required(
                    "bind_mode", default=self._bind_mode
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": "show", "label": "出示激活码（推荐）"},
                            {"value": "enter", "label": "输入云端激活码"},
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional("phone", default=""): str,
                vol.Optional("activation_code", default=""): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_activation(self, user_input: dict[str, Any] | None = None):
        """Show label; submit finishes HA setup (SaaS claim happens in background)."""
        assert self._activation_key
        qr_payload = ensure_qr_payload(
            self._activation_key,
            self._qr_payload,
            self._cloud_url,
            self._setup_id,
        )
        self._qr_payload = qr_payload

        if user_input is not None:
            return await self._create_pending_entry()

        placeholders = label_description_placeholders(
            self._activation_key,
            qr_payload,
            claim_status=(
                "记下设备码后点击 **提交** 完成添加。然后在云枢「认领项目」输入该码；"
                "设备码长期有效，认领成功后 Agent 自动连接。"
            ),
        )

        return self.async_show_form(
            step_id="activation",
            data_schema=activation_label_schema(qr_payload, self._activation_key),
            description_placeholders=placeholders,
            last_step=False,
        )

    async def _create_pending_entry(self):
        """Create or update config entry without tokens; SaaS claim runs in background."""
        assert self._activation_key and self._setup_id
        bind_source = (
            BIND_SOURCE_CLOUD if self._bind_mode == "enter" else BIND_SOURCE_LOCAL
        )
        pending_data = _pending_entry_data(
            cloud_url=self._cloud_url,
            activation_key=self._activation_key,
            setup_id=self._setup_id,
            qr_payload=self._qr_payload,
            activation_id=self._activation_id,
            bind_source=bind_source,
        )
        title = f"UDHUB 待认领 {self._activation_key}"
        unique = f"pending:{self._setup_id}"

        await create_notification(
            self.hass,
            title="UDHUB Agent 待认领",
            message=(
                f"设备码：**{self._activation_key}**\n\n"
                "请在云枢控制台「认领项目」输入该码。"
                "认领成功后本机会自动连接，无需再操作。"
            ),
            notification_id=f"{DOMAIN}_pending_{self._setup_id}",
        )

        existing = self._existing_entry()
        if existing:
            new_data = {**existing.data, **pending_data}
            for key in (CONF_GATEWAY_ID, CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN):
                new_data.pop(key, None)
            self.hass.config_entries.async_update_entry(
                existing,
                data=new_data,
                title=title,
                unique_id=unique,
            )
            return self.async_update_reload_and_abort(existing)

        await self.async_set_unique_id(unique)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=title, data=pending_data)

    async def _create_claimed_entry(self, claimed: dict[str, Any]):
        gateway_id = claimed["gateway_id"]
        bind_source = (
            BIND_SOURCE_CLOUD if self._bind_mode == "enter" else BIND_SOURCE_LOCAL
        )
        entry_data = _claimed_entry_data(
            cloud_url=self._cloud_url,
            claimed=claimed,
            activation_key=self._activation_key or "",
            setup_id=self._setup_id,
            qr_payload=self._qr_payload,
            activation_id=self._activation_id,
            bind_source=bind_source,
        )
        title = f"UDHUB {gateway_id}"

        await create_notification(
            self.hass,
            title="UDHUB Agent 已连接云枢",
            message=(
                f"主机 **{gateway_id}** 已成功接入。\n\n"
                f"设备码：**{self._activation_key}**"
            ),
            notification_id=f"{DOMAIN}_setup_{gateway_id}",
        )

        existing = self._existing_entry()
        if existing:
            self.hass.config_entries.async_update_entry(
                existing,
                data=entry_data,
                title=title,
                unique_id=gateway_id,
            )
            return self.async_update_reload_and_abort(existing)

        await self.async_set_unique_id(gateway_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=title, data=entry_data)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        """重新配置：更新现有条目，可重新出示激活码。"""
        entry = self._existing_entry()
        if entry and user_input is None:
            self._prefill_from_entry(entry)
            self._bind_mode = "show"
        return await self.async_step_user(user_input)

    async def async_step_reauth(self, user_input: dict[str, Any] | None = None):
        """解绑后再绑：更新现有条目（默认输入云端码，也可改选出示码）。"""
        entry = self._existing_entry()
        if entry and user_input is None:
            self._prefill_from_entry(entry)
            self._bind_mode = "enter"
        return await self.async_step_user(user_input)


class UdhubAgentOptionsFlowHandler(config_entries.OptionsFlow):
    """HomeKit 标签页：安装完成后仍可查看激活码与二维码。"""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        entry_data = self.config_entry.data
        activation_key = activation_key_from_entry(entry_data)
        qr_payload = ensure_qr_payload(
            activation_key,
            qr_payload_from_entry(entry_data),
            str(entry_data.get(CONF_CLOUD_URL) or DEFAULT_CLOUD_URL),
            entry_data.get(CONF_SETUP_ID),
        )
        gateway_id = str(entry_data.get(CONF_GATEWAY_ID) or "待认领")
        bind_state = options_bind_state(entry_data, dict(self.config_entry.options))

        if user_input is not None:
            if user_input.get("action") == "rebind" and bind_state["can_rebind"]:
                return await self.async_step_rebind()
            return self.async_create_entry(data=dict(self.config_entry.options))

        if not activation_key:
            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema(
                    {
                        vol.Required("action"): selector.SelectSelector(
                            selector.SelectSelectorConfig(
                                options=[
                                    {
                                        "value": "rebind",
                                        "label": "输入云端激活码（服务商出示码）",
                                    },
                                ],
                                mode=selector.SelectSelectorMode.LIST,
                            )
                        )
                    }
                ),
                errors={"base": "no_label"},
                description_placeholders={
                    "gateway_id": gateway_id,
                    "user_code": "—",
                },
                last_step=False,
            )

        return self.async_show_form(
            step_id="init",
            data_schema=options_init_schema(
                qr_payload,
                activation_key,
                can_rebind=bind_state["can_rebind"],
            ),
            description_placeholders=label_description_placeholders(
                activation_key,
                qr_payload,
                gateway_id=gateway_id,
                claim_status=bind_state["claim_status"],
            ),
            last_step=not bind_state["can_rebind"],
        )

    async def async_step_view_label(self, user_input: dict[str, Any] | None = None):
        entry_data = self.config_entry.data
        activation_key = activation_key_from_entry(entry_data)
        qr_payload = ensure_qr_payload(
            activation_key,
            qr_payload_from_entry(entry_data),
            str(entry_data.get(CONF_CLOUD_URL) or DEFAULT_CLOUD_URL),
            entry_data.get(CONF_SETUP_ID),
        )
        gateway_id = str(entry_data.get(CONF_GATEWAY_ID) or "待认领")

        if user_input is not None:
            return await self.async_step_init()

        if not activation_key:
            return self.async_show_form(
                step_id="view_label",
                data_schema=vol.Schema({}),
                errors={"base": "no_label"},
            )

        return self.async_show_form(
            step_id="view_label",
            data_schema=activation_label_schema(
                qr_payload,
                activation_key,
                include_code_field=True,
            ),
            description_placeholders=label_description_placeholders(
                activation_key,
                qr_payload,
                gateway_id=gateway_id,
            ),
            last_step=False,
        )

    async def async_step_rebind(self, user_input: dict[str, Any] | None = None):
        """Bind provider cloud activation code without leaving options flow."""
        errors: dict[str, str] = {}
        entry = self.config_entry
        cloud_url = str(entry.data.get(CONF_CLOUD_URL) or DEFAULT_CLOUD_URL).rstrip("/")

        if user_input is not None:
            cloud_code = (user_input.get("activation_code") or "").strip().upper()
            cloud_url = str(user_input.get(CONF_CLOUD_URL) or cloud_url).rstrip("/")
            if not cloud_code:
                errors["activation_code"] = "required"
            else:
                session = async_get_clientsession(self.hass)
                try:
                    async with session.post(
                        f"{cloud_url}/api/v1/agent/activation/bind",
                        json={
                            "activation_code": cloud_code,
                            "agent_version": AGENT_VERSION,
                            "ha_version": HA_VERSION,
                            "install_type": "Home Assistant OS",
                            "key_version": "v2",
                        },
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        data = unwrap_payload(await resp.json())
                        if resp.status >= 400:
                            err_text = str(
                                data.get("message") or data.get("code") or ""
                            ).lower()
                            if "not_found" in err_text or resp.status == 404:
                                errors["activation_code"] = "invalid_code"
                            else:
                                errors["base"] = "cannot_connect"
                        else:
                            activation_id = data["activation_id"]
                            activation_key = (
                                data.get("activation_key") or cloud_code
                            )
                            setup_id = (
                                data.get("setup_id")
                                or entry.data.get(CONF_SETUP_ID)
                                or f"bind-{activation_id}"
                            )
                            qr_payload = ensure_qr_payload(
                                activation_key,
                                data.get("qr_payload"),
                                cloud_url,
                                setup_id,
                            )

                            if (
                                data.get("auto_claimed")
                                or data.get("status") == "claimed"
                            ):
                                claimed = await poll_activation_once(
                                    session,
                                    cloud_url,
                                    activation_id=activation_id,
                                    activation_key=activation_key,
                                )
                                if claimed and claimed.get("status") == "claimed":
                                    new_data = _claimed_entry_data(
                                        cloud_url=cloud_url,
                                        claimed=claimed,
                                        activation_key=activation_key,
                                        setup_id=setup_id,
                                        qr_payload=qr_payload,
                                        activation_id=activation_id,
                                        bind_source=BIND_SOURCE_CLOUD,
                                    )
                                    self.hass.config_entries.async_update_entry(
                                        entry,
                                        data=new_data,
                                        title=f"UDHUB {claimed['gateway_id']}",
                                        unique_id=claimed["gateway_id"],
                                    )
                                    await self.hass.config_entries.async_reload(
                                        entry.entry_id
                                    )
                                    return self.async_create_entry(
                                        data={
                                            **dict(entry.options),
                                            "cloud_bound": True,
                                        }
                                    )

                            pending_data = _pending_entry_data(
                                cloud_url=cloud_url,
                                activation_key=activation_key,
                                setup_id=setup_id,
                                qr_payload=qr_payload,
                                activation_id=activation_id,
                                bind_source=BIND_SOURCE_CLOUD,
                            )
                            new_data = {**entry.data, **pending_data}
                            for key in (
                                CONF_GATEWAY_ID,
                                CONF_ACCESS_TOKEN,
                                CONF_REFRESH_TOKEN,
                            ):
                                new_data.pop(key, None)
                            self.hass.config_entries.async_update_entry(
                                entry,
                                data=new_data,
                                title=f"UDHUB 待认领 {activation_key}",
                                unique_id=f"pending:{setup_id}",
                            )
                            await self.hass.config_entries.async_reload(
                                entry.entry_id
                            )
                            return self.async_create_entry(
                                data={
                                    **dict(entry.options),
                                    "cloud_bound": True,
                                }
                            )
                except Exception:
                    _LOGGER.exception("UDHUB options rebind failed")
                    errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="rebind",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CLOUD_URL, default=cloud_url): str,
                    vol.Required("activation_code"): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "hint": "输入服务商在云枢控制台生成的激活码，格式 XXX-XX-XXX",
            },
            last_step=False,
        )
