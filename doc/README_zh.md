# UDHUB 云枢 Agent — 中文交付说明

面向集成商现场交付与客户自助安装。产品定位：在标准 Home Assistant 上安装 `udhub_agent`，出站接入云枢 SaaS，**日常调试在云枢完成，不在客户 HA 前端操作**。

**最低 HA Core：2026.9.0**（HACS / manifest 门槛；更低版本请先升级主机）。

## 三条安装通道

| 通道 | 适用 | 步骤摘要 |
|------|------|----------|
| ① git + `install.sh` | 集成商（可锁版本、可回滚） | clone → `git checkout vX.Y.Z` → `./install.sh /config` → 重启 HA |
| ② HACS 自定义存储库 | 客户自助 | HACS → 自定义存储库 → 粘贴仓库地址 → 类别「集成」→ 下载 → 重启 |
| ③ 手动拷贝 | 无 SSH / 排障兜底 | 下载 tag 压缩包 → 拷贝 `custom_components/udhub_agent` → 重启 |

### 双源地址

| 源 | URL |
|----|-----|
| GitHub（主） | `https://github.com/udhubs/ha_hub_agent.git` |
| Gitee（镜像） | `https://gitee.com/udhubs/ha_hub_agent.git` |

现场网络差时优先 Gitee。

### 通道① 示例

```bash
cd /config   # 或 SSH 进 HAOS 后进入 config
git clone https://gitee.com/udhubs/ha_hub_agent.git
cd ha_hub_agent
git checkout v0.4.43
./install.sh /config
```

然后：**设置 → 系统 → 重启** → **设置 → 设备与服务 → 添加集成 → UDHUB Agent**。

## 配对（集成商操作卡）

1. 确认云端 URL 为 `https://www.udhub.com`（默认即可）。  
2. **模式 A（推荐）**：HA 出示激活码 → 云枢控制台「认领项目」输入该码。  
3. **模式 B**：控制台客户侧「重新绑定」生成码 → HA 选择「输入云端激活码」。  
4. 控制台显示主机在线后，再做业主授权与首次全量同步。

## 更新

| 方式 | 说明 |
|------|------|
| git | `git fetch && git checkout <新 tag> && ./install.sh /config` → 重启 |
| HACS | 有更新提示时一键更新 → 重启 |
| 云端 OTA | Agent ≥ 0.2.20 且在线时，控制台「从云端更新 Agent」 |

## 卸载 / 撤销

在 HA 中删除「UDHUB Agent」集成卡片 → Agent 停止出站连接；云枢侧心跳超时标记离线。吊销凭证后云端不可再读控该主机。

## 不要做的事

- 不向云端上传 HA Long-Lived Access Token  
- 不开放公网 8123 作为运维入口  
- 不把本集成拆包成其他远程运维产品（见仓库 `LICENSE.md`）
