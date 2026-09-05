# Brands 提交材料（home-assistant/brands）

向 [home-assistant/brands](https://github.com/home-assistant/brands) 提交自定义集成图标。

## 本目录内容

```
custom_integrations/udhub_agent/
  icon.png   # 256×256，白底品牌标（GitHub / HA brands）
  logo.png   # 同 icon
```

头像（GitHub 个人资料 / 社媒）：仓库根 `media/github-avatar.png`（512）与 `media/github-avatar-1024.png`。

设置 GitHub 头像：打开 https://github.com/settings/profile → Profile picture → Upload → 选 `media/github-avatar-1024.png`。

## 提交流程（一次性）

1. Fork `home-assistant/brands`  
2. 将本目录 `custom_integrations/udhub_agent/` 拷入 fork 对应路径  
3. PR 说明：domain=`udhub_agent`，产品名 UDHUB / 云枢  
4. 合并后 HA 内集成卡片可显示正规图标；`my.home-assistant.io` brand 跳转需另按 brands 仓库规范声明 domain  

## 设计来源

与云枢控制台一致：`web/public/favicon.svg` / 侧栏 `BrandMark`（深蓝圆角底 + 云枢青菱形描边 + 圆心）。正式发版前可用设计定稿 SVG 重导出替换。

## 状态

- [x] 本地 icon/logo PNG  
- [ ] 向 brands 仓库开 PR（需 GitHub 账号与维护者操作）
