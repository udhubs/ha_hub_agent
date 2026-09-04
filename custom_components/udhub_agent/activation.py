"""UDHUB Agent 激活码生成 — HomeKit 风格 8 位数字 XXX-XX-XXX。

对齐 doc/19 §2：Agent 侧本地生成激活码（模拟物理标签），
上报云端后由控制台读取展示。
"""

from __future__ import annotations

import random
import secrets
import string
from urllib.parse import quote


# ── 常量 ──────────────────────────────────────────────────────────────────────

# HomeKit 风格：纯数字 3-2-3 分组
_HOMEKIT_DIGITS = string.digits  # "0123456789"

# Setup ID 字符集（Crockford-32 变体，去除 0/1/I/O 易混淆字符）
_SETUP_ID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"

# 激活码有效期：HA 本地出示码长期有效（0 = 永久，认领前不过期）
ACTIVATION_EXPIRES_SEC = 0

# QR payload URI scheme
_QR_URI_SCHEME = "udhub://activate"


# ── 生成函数 ──────────────────────────────────────────────────────────────────


def generate_homekit_code() -> str:
    """生成 HomeKit 风格 8 位数字激活码：XXX-XX-XXX。

    使用 secrets 模块保证密码学安全随机性。
    10^8 = 1 亿种组合。
    """
    digits = [secrets.choice(_HOMEKIT_DIGITS) for _ in range(8)]
    code = "".join(digits)
    return f"{code[:3]}-{code[3:5]}-{code[5:]}"


def generate_setup_id() -> str:
    """生成 4 字符 Setup ID（嵌入 QR payload）。"""
    return "".join(secrets.choice(_SETUP_ID_ALPHABET) for _ in range(4))


def generate_install_nonce() -> str:
    """生成安装随机数（用于 QR payload 防重放）。"""
    return secrets.token_hex(16)


def build_qr_payload(
    activation_key: str,
    setup_id: str,
    cloud_url: str,
    install_nonce: str | None = None,
    agent_version: str | None = None,
) -> str:
    """构建 QR 码 payload（URI scheme）。

    格式：udhub://activate?v=1&sid=XXXX&key=XXX-XX-XXX&cloud=...&n=...&av=...
    """
    cloud = quote(cloud_url.rstrip("/"), safe="")
    parts = [
        f"{_QR_URI_SCHEME}?v=1",
        f"&sid={setup_id}",
        f"&key={activation_key}",
        f"&cloud={cloud}",
    ]
    if install_nonce:
        parts.append(f"&n={install_nonce[:16]}")
    if agent_version:
        parts.append(f"&av={agent_version}")
    return "".join(parts)


# ── 校验函数 ──────────────────────────────────────────────────────────────────


def is_homekit_code(key: str) -> bool:
    """检测是否为 HomeKit 风格 8 位数字码（XXX-XX-XXX）。"""
    normalized = key.strip().replace(" ", "")
    parts = normalized.split("-")
    if len(parts) != 3:
        return False
    return (
        len(parts[0]) == 3
        and len(parts[1]) == 2
        and len(parts[2]) == 3
        and all(p.isdigit() for p in parts)
    )


def normalize_activation_code(raw: str) -> str:
    """标准化激活码（去空格、转大写）。"""
    return raw.strip().upper().replace(" ", "")


# ── 完整激活会话创建 ──────────────────────────────────────────────────────────


def create_activation_session(
    cloud_url: str,
    agent_version: str = "0.2.0",
    ha_version: str | None = None,
    install_type: str = "Home Assistant OS",
) -> dict:
    """创建完整的激活会话（本地生成码 + QR payload）。

    返回：
    {
        "activation_key": "454-84-149",
        "setup_id": "PLCE",
        "install_nonce": "a1b2c3d4...",
        "qr_payload": "udhub://activate?v=1&sid=PLCE&key=454-84-149&cloud=...",
        "expires_in": 900,
        "key_version": "v3",
        "agent_version": "0.2.0",
    }
    """
    activation_key = generate_homekit_code()
    setup_id = generate_setup_id()
    install_nonce = generate_install_nonce()
    qr_payload = build_qr_payload(
        activation_key=activation_key,
        setup_id=setup_id,
        cloud_url=cloud_url,
        install_nonce=install_nonce,
        agent_version=agent_version,
    )

    return {
        "activation_key": activation_key,
        "setup_id": setup_id,
        "install_nonce": install_nonce,
        "qr_payload": qr_payload,
        "expires_in": ACTIVATION_EXPIRES_SEC,
        "key_version": "v3",
        "agent_version": agent_version,
        "ha_version": ha_version,
        "install_type": install_type,
    }
