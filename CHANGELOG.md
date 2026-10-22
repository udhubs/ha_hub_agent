# Changelog — UDHub Agent

## 0.5.25

- 显示名统一为 **UDHub Agent**；打包并上传 icon/logo（`/agent/icon.png`、`/agent/logo.png`）

## 0.5.24

- 安装完成 / 待绑定状态明确提示打开 https://www.udhub.com 注册或登录并认领设备码

## 0.5.23

- `update.install` / `skip` / `clear_skipped`：HA 2026.9+ 注入管理员 Context（无 admin 时明确失败，避免静默拒绝）

## 0.5.21

- config_write：automation package 按 automation 实体校验，不再误用 scene_not_registered 回滚

## 0.5.20

- 集成安装：zip 成员上限 2000 → 10000（修复 HACS `too_many_members`）

## 0.5.19

- 集成安装：GitHub 下载增加 ghfast / ghproxy 回退；zip 上限 80MB（适配 HACS）
- 已知种子增加 `hacs`；优先本库镜像安装商店

## 0.5.18

- 集成商店：取消硬编码精选白名单门禁
- 安装策略：本库镜像仓有的均可装；缺失时可 `confirm=true` 直装任意 GitHub Release zip
- 卸载按 domain；探测列出已知种子 + 主机已装 custom_components

## 0.5.17

- 米家远程首次 OAuth：`config_get` 返回 `oauth_reachable_candidates`（external_url / LAN），供控制台一键改写回调主机

## 0.5.16

- Matter Multi-Admin：开配对窗结果规范化（setup QR / 手工码 / PIN），控制台可复制共享码
- Matter：下发 Thread Operational Dataset（`set_thread_dataset`，不回传内容）

## 0.5.15

- 精选集成安装：优先从云枢 SaaS 镜像仓下载（米家 MVP），失败回退 GitHub
- 兼容官方扁平 zip（无 `custom_components/` 前缀，如 xiaomi_home）

## 0.5.14

- 设备在线：`unknown` 不再算离线；按实体可用性写 online_status（跳过 diagnostic/config）

## 0.5.13

- Matter：修正 WS 为 message_id/command；节点规范化、开配对窗、查 OTA、Wi‑Fi 凭证下发
- 米家 brand_probe：data_keys / reason / OAuth 基址 / 云 HEAD（不落密）

## 0.5.12

- ZHA 诊断：LQI/RSSI 采样与弱链路提示（best-effort zigpy）

## 0.5.11

- 诊断加深：米家凭证字段探测（不落密）、ZHA 设备/离线估计
- 配合 SaaS：B-write 回滚告警、策略包 JSON、schema 空表壳

## 0.5.10

- 策略规则包驱动：catalog_version 11
- diagnostics.post_upgrade：升级后集成/pip/协议巡检
- Matter commission_with_code（配对码入网骨架）

## 0.5.9

- diagnostics：品牌/协议/链路探针（`diagnostics.link_probe`）
- Matter：WebSocket JSON-RPC（`matter.nodes` / `matter.rpc`）+ HTTP 回退
- 能力探测：`hassio_available`、`pip_deps` / `pip_deps_ok`
- B-write：可选 fail-closed 备份；域健康失败默认回滚
- 分发：`hacs.json` / `install.sh` / LICENSE

## 0.5.8

- HA 基线对齐 2026.9.0；`hacs.json`；Matter HTTP probe 骨架
- catalog_version 9（matter.probe / matter.rpc）

## 0.5.7

- 既有控制面能力（registry / supervisor / blueprint / config write）
