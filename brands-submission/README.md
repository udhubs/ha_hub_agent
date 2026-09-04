# Brands 提交材料（home-assistant/brands）

向 [home-assistant/brands](https://github.com/home-assistant/brands) 提交自定义集成图标。

## 本目录内容

```
custom_integrations/udhub_agent/
  icon.png   # 256×256，白底品牌标
  logo.png   # 同 icon（可后续换成横版字标）
```

## 提交流程（一次性）

1. Fork `home-assistant/brands`  
2. 将本目录 `custom_integrations/udhub_agent/` 拷入 fork 对应路径  
3. PR 说明：domain=`udhub_agent`，产品名 UDHUB / 云枢  
4. 合并后 HA 内集成卡片可显示正规图标；`my.home-assistant.io` brand 跳转需另按 brands 仓库规范声明 domain  

## 设计来源

基于云枢 `docs/dev-only/doc/brand/udhub-logo-icon.svg`（菱形 + 云枢青圆点）栅格化；正式发版前可用设计定稿 SVG 重导出替换。

## 状态

- [x] 本地 icon/logo PNG  
- [ ] 向 brands 仓库开 PR（需 GitHub 账号与维护者操作）
