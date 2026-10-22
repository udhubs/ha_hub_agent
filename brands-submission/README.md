# HA brands 提交材料

路径约定对齐 [home-assistant/brands](https://github.com/home-assistant/brands)：

```text
custom_integrations/udhub_agent/icon.png
custom_integrations/udhub_agent/logo.png
```

提 PR 时把本目录内容合并进 brands 仓对应路径。同步到分发仓：`./scripts/sync-ha-hub-agent.sh`。

## 在线地址

- Icon: https://www.udhub.com/agent/icon.png
- Logo: https://www.udhub.com/agent/logo.png
- 显示名：**UDHub Agent**

## HA 2026.3+ 本地品牌图（优先）

集成目录内放置（OTA 随包下发，无需 brands PR）：

```text
custom_components/udhub_agent/brand/icon.png
custom_components/udhub_agent/brand/logo.png
```

参见 https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/

