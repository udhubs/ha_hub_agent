"""HomeKit-style activation label UI helpers (config / options flow)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.helpers import selector
from homeassistant.helpers.selector import QrCodeSelectorConfig, QrErrorCorrectionLevel

from .activation import ACTIVATION_EXPIRES_SEC, build_qr_payload

# QR 渲染尺寸（HA QrCodeSelector scale，默认 8 偏大）
QR_DISPLAY_SCALE = 3
from .const import (
    CONF_ACTIVATION_KEY,
    CONF_BIND_SOURCE,
    CONF_CLAIM_STATUS,
    CONF_CLOUD_URL,
    CONF_GATEWAY_ID,
    CONF_QR_PAYLOAD,
    CONF_SETUP_ID,
    CONF_USER_CODE,
    BIND_SOURCE_CLOUD,
    BIND_SOURCE_LOCAL,
    CLAIM_STATUS_CLAIMED,
    DEFAULT_CLOUD_URL,
)


def activation_key_from_entry(entry_data: dict[str, Any]) -> str:
    """Return stored activation key from a config entry."""
    return str(
        entry_data.get(CONF_ACTIVATION_KEY)
        or entry_data.get(CONF_USER_CODE)
        or ""
    ).strip()


def qr_payload_from_entry(entry_data: dict[str, Any]) -> str:
    """Rebuild QR payload from entry data (supports legacy entries)."""
    stored = str(entry_data.get(CONF_QR_PAYLOAD) or "").strip()
    if stored:
        return stored
    key = activation_key_from_entry(entry_data)
    if not key:
        return ""
    cloud_url = str(entry_data.get(CONF_CLOUD_URL) or DEFAULT_CLOUD_URL).rstrip("/")
    setup_id = str(entry_data.get(CONF_SETUP_ID) or "GENL")
    return build_qr_payload(
        activation_key=key,
        setup_id=setup_id,
        cloud_url=cloud_url,
        agent_version="0.2.0",
    )


def ensure_qr_payload(
    activation_key: str,
    qr_payload: str | None,
    cloud_url: str,
    setup_id: str | None,
) -> str:
    """Always return a scannable payload when we have an activation key."""
    if qr_payload:
        return qr_payload
    if not activation_key:
        return ""
    sid = setup_id or "GENL"
    return build_qr_payload(
        activation_key=activation_key,
        setup_id=sid,
        cloud_url=cloud_url.rstrip("/"),
        agent_version="0.2.0",
    )


def label_description_placeholders(
    activation_key: str,
    qr_payload: str,
    *,
    gateway_id: str | None = None,
    expires_minutes: int | None = None,
    claim_status: str | None = None,
) -> dict[str, str]:
    """Placeholders for strings.json activation / label steps."""
    default_status = "在云枢控制台「认领项目」输入上方设备码完成绑定。"
    expires_label = (
        "长期有效（认领前不过期）"
        if ACTIVATION_EXPIRES_SEC <= 0
        else f"约 {expires_minutes or ACTIVATION_EXPIRES_SEC // 60} 分钟"
    )
    return {
        "user_code": activation_key,
        "qr_payload": qr_payload,
        "gateway_id": gateway_id or "—",
        "expires_minutes": expires_label,
        "claim_status": claim_status or default_status,
    }


def activation_label_schema(
    qr_payload: str,
    activation_key: str,
    *,
    include_code_field: bool = True,
    editable_code: bool = False,
) -> vol.Schema:
    """HomeKit 标签页：二维码 + 8 位设备码（均可见）。

    设备码用 suggested_value 预填（可复制）。仅用于 last_step=False 的步骤；
    完成页请用 finish_step_schema()，避免 HA 在 last_step 上卡死提交。
    """
    fields: dict[Any, Any] = {}
    payload = qr_payload or activation_key
    if payload:
        fields[vol.Optional("activation_qr")] = selector.QrCodeSelector(
            QrCodeSelectorConfig(
                data=payload,
                scale=QR_DISPLAY_SCALE,
                error_correction_level=QrErrorCorrectionLevel.QUARTILE,
            )
        )
    if include_code_field and activation_key:
        # Always prefill so the 8-digit code is visible/copyable in the form.
        # (Description placeholder alone is easy to miss.)
        code_field = vol.Optional(
            "activation_code",
            description={"suggested_value": activation_key},
        )
        fields[code_field] = str
    if not fields:
        fields[vol.Optional("activation_code")] = str
    return vol.Schema(fields)


def options_bind_state(
    entry_data: dict[str, Any],
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Whether cloud-code rebind is allowed and status copy for options UI."""
    options = options or {}
    claimed = entry_data.get(CONF_CLAIM_STATUS) == CLAIM_STATUS_CLAIMED
    gateway_id = str(entry_data.get(CONF_GATEWAY_ID) or "").strip()
    cloud_bound = bool(
        options.get("cloud_bound")
        or entry_data.get(CONF_BIND_SOURCE) == BIND_SOURCE_CLOUD
        or str(entry_data.get(CONF_SETUP_ID) or "").startswith("bind-")
    )

    if claimed and gateway_id:
        return {
            "can_rebind": False,
            "action_value": "connected",
            "action_label": "已连接云枢",
            "claim_status": (
                f"✅ **已连接云枢** · 主机 `{gateway_id}`\n\n"
                "云端激活码已绑定，无需重复操作。"
            ),
        }
    if cloud_bound:
        return {
            "can_rebind": False,
            "action_value": "bound",
            "action_label": "已绑定云端激活码",
            "claim_status": (
                "✅ **已绑定云端激活码**\n\n"
                "请在云枢控制台完成认领；认领成功后 Agent 将自动连接。"
            ),
        }
    return {
        "can_rebind": True,
        "action_value": "rebind",
        "action_label": "输入云端激活码（服务商出示码）",
        "claim_status": (
            "设备码长期有效。在云枢「认领项目」输入该码即可；"
            "或使用下方绑定服务商出示的云端码。"
        ),
    }


def options_init_schema(
    qr_payload: str,
    activation_key: str,
    *,
    can_rebind: bool = True,
) -> vol.Schema:
    """Options 首页：直接展示二维码 + 设备码，未绑定时才显示绑定操作。"""
    fields: dict[Any, Any] = {}
    payload = qr_payload or activation_key
    if payload:
        fields[vol.Optional("activation_qr")] = selector.QrCodeSelector(
            QrCodeSelectorConfig(
                data=payload,
                scale=QR_DISPLAY_SCALE,
                error_correction_level=QrErrorCorrectionLevel.QUARTILE,
            )
        )
    if activation_key:
        fields[
            vol.Optional(
                "activation_code",
                description={"suggested_value": activation_key},
            )
        ] = str
    if can_rebind:
        fields[vol.Required("action")] = selector.SelectSelector(
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
    return vol.Schema(fields)


def finish_step_schema() -> vol.Schema:
    """完成步：空 schema，确保 HA 显示可点的「完成」按钮。"""
    return vol.Schema({})
