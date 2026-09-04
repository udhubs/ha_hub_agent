# Changelog

版本号与 Git tag、`custom_components/udhub_agent/manifest.json` 的 `version` 三处必须一致。

## [0.4.44] — 2026-09-04

### Safety

- OTA 后 **云端健康检查**（须在约 90s 内重新 `hello.ok`）
- 失败 / 超时自动从 `udhub_agent.bak` **回滚**并重载集成
- 写入 `udhub_agent.ota_pending.json`；HA 重启后若标记仍在则继续监视或回滚
- 回滚成功后 HA 持久通知提示

## [0.4.43] — 2026-09-04

### Distribution

- 独立仓 `ha_hub_agent` 初始化（L1 自有仓库分发，对齐云枢文档 `21`）
- 新增 `hacs.json`、双源友好 `install.sh`、商业 `LICENSE.md`、brands 提交材料
- **最低 HA 版本抬至 `2026.9.0`**（manifest + hacs.json；低于此版本 HACS 不提供安装/更新）

### Agent（自 monorepo 同步）

- Config Flow A/B + reauth + options flow
- 出站 WebSocket：hello / 心跳 / sync.full|delta / command.execute
- 能力目录 `catalog_version` + HA 2026.8–2026.9 注册表字段探测
- 云端 OTA `agent_update`（sha256 + 备份 + 重载）
