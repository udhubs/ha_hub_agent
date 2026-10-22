DOMAIN = "udhub_agent"
import json as _json
from pathlib import Path as _Path

def _agent_version_from_manifest() -> str:
    """Single source of truth: manifest.json (what HA UI shows)."""
    try:
        data = _json.loads(
            _Path(__file__).with_name("manifest.json").read_text(encoding="utf-8")
        )
        ver = str(data.get("version") or "").strip()
        if ver:
            return ver
    except Exception:  # noqa: BLE001
        pass
    return "0.0.0"

AGENT_VERSION = _agent_version_from_manifest()
PROTOCOL_VERSION = "0.1"
DEFAULT_CLOUD_URL = "https://www.udhub.com"
# HA 安装完成 / 待绑定状态：引导用户到官网注册并认领
CONSOLE_CLAIM_HINT = (
    "请打开 https://www.udhub.com 注册/登录并认领本机设备码。"
)
# 已连接后：二维码页引导前往注册页
CONSOLE_CONNECTED_HINT = (
    "前往 https://www.udhub.com/register 注册使用"
)
# HA「设备与服务」集成条目标题
ENTRY_TITLE_PREFIX = "UDHub Agent"


def entry_title(activation_key: str | None = None, *, pending: bool = False) -> str:
    """Build config entry title: UDHub Agent 设备码 xxx-xx-xxx."""
    key = (activation_key or "").strip()
    if key:
        base = f"{ENTRY_TITLE_PREFIX} 设备码 {key}"
    else:
        base = ENTRY_TITLE_PREFIX
    if pending:
        return f"{base} · 待认领"
    return base


# Back-compat aliases (static fallbacks; prefer entry_title())
ENTRY_TITLE = ENTRY_TITLE_PREFIX
ENTRY_TITLE_PENDING = f"{ENTRY_TITLE_PREFIX} · 待认领"
CONF_CLOUD_URL = "cloud_url"
CONF_GATEWAY_ID = "gateway_id"
CONF_ACCESS_TOKEN = "access_token"
CONF_USER_CODE = "user_code"
CONF_ACTIVATION_KEY = "activation_key"
CONF_SETUP_ID = "setup_id"
CONF_QR_PAYLOAD = "qr_payload"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACTIVATION_ID = "activation_id"
# Install-time secret proving this HA instance may poll for gateway tokens.
# Never shown in the UI or QR. Old entries lack it and must be re-activated.
CONF_DEVICE_SECRET = "device_secret"

# Pending setup: HA finished; waiting for SaaS claim
CLAIM_STATUS_PENDING = "pending"
CLAIM_STATUS_CLAIMED = "claimed"
CONF_CLAIM_STATUS = "claim_status"

# How this host was paired: local device code vs provider cloud code
CONF_BIND_SOURCE = "bind_source"
BIND_SOURCE_LOCAL = "local"
BIND_SOURCE_CLOUD = "cloud"
CONF_OPTIONS_CLOUD_BOUND = "cloud_bound"

# Background wait for cloud claim: 0 = no timeout (pairing code does not expire)
CLAIM_POLL_INTERVAL_SEC = 3
CLAIM_WAIT_TIMEOUT_SEC = 0

# Heartbeat interval in seconds (aligned with protocol v0.1 default ~30s)
HEARTBEAT_INTERVAL = 30

# Reconnect backoff: start at 1s, double up to 60s
RECONNECT_BASE = 1
RECONNECT_MAX = 60

# --- Connection self-healing (supervisor, 0.4.24+) ---
# aiohttp protocol-level ping: detects half-open TCP at transport level.
# Missed pong → aiohttp closes the socket → read loop exits → reconnect.
WS_PROTO_HEARTBEAT_SEC = 45
# Supervisor check interval (independent watchdog task).
SUPERVISOR_INTERVAL_SEC = 15
# Rotate to next cloud URL after this many consecutive failed connect attempts
# (cloud_url supports comma-separated fallback endpoints).
URL_ROTATE_AFTER_FAILURES = 4
# Connection loop considered STUCK if no new connect attempt for this long
# while unhealthy (OTA-reload-hang signature) → detached entry reload.
STUCK_NO_ATTEMPT_SEC = 120
# Min gap between self-triggered entry reloads (avoid reload loops).
SELF_RELOAD_COOLDOWN_SEC = 1800

# Standard tier — safe for daily operations.
ALLOWED_DOMAINS = {
    # Core control
    "light",
    "switch",
    "scene",
    "automation",
    "climate",
    "cover",
    "fan",
    "media_player",
    "homeassistant",
    # Input / helper
    "input_boolean",
    "input_select",
    "input_number",
    "input_text",
    "input_datetime",
    "select",
    "number",
    "button",
    "script",
    # Monitoring / info
    "binary_sensor",
    "sensor",
    "device_tracker",
    "person",
    "zone",
    "sun",
    "group",
    "weather",
}

# Extended tier — debugging / explicit elevated maintenance (PRD §5.4G).
EXTENDED_DOMAINS = ALLOWED_DOMAINS | {
    # Advanced control
    "vacuum",
    "humidifier",
    "water_heater",
    "camera",
    "lock",
    "remote",
    "notify",
    "text",
    "datetime",
    "time",
    "counter",
    "timer",
    "air_quality",
    "persistent_notification",
    "tts",
    "image_processing",
    "calendar",
    "todo",
    "update",
    "backup",
    "conversation",
}

CAPABILITY_TIER_STANDARD = "standard"
CAPABILITY_TIER_EXTENDED = "extended"

# homeassistant 域仅允许运维类服务（禁止 stop 等危险操作）
ALLOWED_HA_SERVICES = {
    "restart",
    "reload_core_config",
    "reload_all",
    "reload_config_entry",
    "toggle",
    "turn_on",
    "turn_off",
    "check_config",
}

# Entity domains we sync to the cloud (exclude noisy/internal domains)
SYNC_DOMAINS = {
    "light",
    "switch",
    "binary_sensor",
    "sensor",
    "climate",
    "cover",
    "fan",
    "lock",
    "media_player",
    "camera",
    "vacuum",
    "scene",
    "automation",
    "humidifier",
    "water_heater",
    "input_boolean",
    "input_select",
    "input_number",
    "input_text",
    "input_datetime",
    "select",
    "number",
    "button",
    "device_tracker",
    "person",
    "remote",
    "calendar",
    "todo",
    "update",
    "text",
    "counter",
    "timer",
}

# Always denied (even in extended tier).
DENIED_DOMAINS = {"alarm_control_panel"}

# Denied in standard tier only.
STANDARD_DENIED_DOMAINS = {"lock"}
