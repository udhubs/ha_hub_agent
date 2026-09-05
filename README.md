# UDHUB · 云枢 Agent (`udhub_agent`)

<p align="center">
  <img src="media/github-avatar.png" width="128" height="128" alt="UDHUB Agent">
</p>

Home Assistant **custom integration** that connects a field HA host to [UDHUB 云枢](https://www.udhub.com) for project ops: status sync, controlled commands, settings surface, and audit.

> Repository root **is** the install package (same layout as [XiaoMi/ha_xiaomi_home](https://github.com/XiaoMi/ha_xiaomi_home)).

| Item | Value |
|------|--------|
| Domain | `udhub_agent` |
| Min Home Assistant | **2026.9.0** |
| Console | https://www.udhub.com |
| Agent WS | `wss://www.udhub.com/agent/v1/ws` |
| License | Custom commercial — see [LICENSE.md](LICENSE.md) |

中文安装与交付说明见 [doc/README_zh.md](doc/README_zh.md)。

## Install channels

### ① git + `install.sh` (integrators, preferred)

```bash
# Prefer Gitee when GitHub is slow on site
git clone https://gitee.com/udhubs/ha_hub_agent.git   # or github.com/udhubs/ha_hub_agent.git
cd ha_hub_agent
git checkout v0.4.43   # pin version
./install.sh /config
# Restart Home Assistant → Settings → Devices & services → Add → UDHUB Agent
```

### ② HACS custom repository

1. HACS → ⋮ → Custom repositories  
2. URL: `https://github.com/udhubs/ha_hub_agent` (or Gitee mirror)  
3. Category: **Integration** → Download → Restart HA  

### ③ Manual copy

Download the tag archive → copy `custom_components/udhub_agent` into `<config>/custom_components/` → restart.

## Pairing

| Mode | On HA | On 云枢 console |
|------|-------|-----------------|
| **A** Agent shows code | Add integration → show activation code | Claim project → enter code |
| **B** Cloud shows code | Enter cloud activation code | Customer → re-bind |

Cloud URL default: `https://www.udhub.com` (do not use legacy `:8787`).

## Version discipline

- Git tag `vX.Y.Z` ≡ `manifest.json` `version` ≡ CHANGELOG section  
- OTA packages for already-online Agents are still built from the UDHUB monorepo (`npm run build:agent-release`); this repo is the **field install / HACS** surface.

## Development sync

Canonical agent source during active development may live in the UDHUB monorepo. Publish with:

```bash
# from UDHUB-SA
./opt/udhub/scripts/sync-ha-hub-agent.sh /path/to/ha_hub_agent
```

## Security

- Outbound WebSocket over TLS only — no inbound ports, no HA Long-Lived Token to the cloud.  
- Commands are capability-catalog + whitelist gated.
