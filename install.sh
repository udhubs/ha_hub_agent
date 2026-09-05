#!/usr/bin/env bash
# UDHUB Agent — install into Home Assistant config (doc 21 / brands·Gitee 分发)
#
# Preferred (SaaS):
#   curl -fsSL https://www.udhub.com/agent/install.sh | bash -s -- /config
#
# Options:
#   UDHUB_SOURCE=github|gitee|auto   (default: auto — try GitHub then Gitee)
#   UDHUB_VERSION=v0.5.15            (optional tag; omit = main/master)
#   UDHUB_GITHUB_REPO=udhubs/ha_hub_agent
#   UDHUB_GITEE_REPO=udhubs/ha_hub_agent
set -euo pipefail

SOURCE="${UDHUB_SOURCE:-auto}"
VERSION="${UDHUB_VERSION:-}"
TARGET_ROOT="${1:-${HA_CONFIG:-/config}}"
COMPONENT_DST="${TARGET_ROOT}/custom_components/udhub_agent"

GITHUB_REPO="${UDHUB_GITHUB_REPO:-udhubs/ha_hub_agent}"
GITEE_REPO="${UDHUB_GITEE_REPO:-udhubs/ha_hub_agent}"

log() { printf '[udhub-agent] %s\n' "$*"; }

if [[ ! -d "$TARGET_ROOT" ]]; then
  log "ERROR: HA config dir not found: $TARGET_ROOT"
  exit 1
fi

TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

github_url() {
  local base="https://github.com/${GITHUB_REPO}"
  if [[ -n "$VERSION" ]]; then
    echo "${base}/archive/refs/tags/${VERSION}.tar.gz"
  else
    echo "${base}/archive/refs/heads/main.tar.gz"
  fi
}

gitee_url() {
  local base="https://gitee.com/${GITEE_REPO}"
  if [[ -n "$VERSION" ]]; then
    echo "${base}/repository/archive/${VERSION}.tar.gz"
  else
    echo "${base}/repository/archive/master.tar.gz"
  fi
}

fetch_url() {
  local url="$1"
  local out="$2"
  if command -v curl >/dev/null; then
    curl -fsSL --connect-timeout 20 --max-time 180 "$url" -o "$out"
  elif command -v wget >/dev/null; then
    wget -qO "$out" "$url"
  else
    log "ERROR: need curl or wget"
    exit 1
  fi
}

URLS=()
case "$SOURCE" in
  gitee|Gitee|GITEE)
    URLS+=("$(gitee_url)")
    ;;
  github|GitHub|GITHUB)
    URLS+=("$(github_url)")
    ;;
  auto|AUTO|"")
    URLS+=("$(github_url)" "$(gitee_url)")
    ;;
  *)
    log "ERROR: unknown UDHUB_SOURCE=$SOURCE (use auto|github|gitee)"
    exit 1
    ;;
esac

ARCHIVE="${TMP}/agent.tgz"
DOWNLOADED=""
for url in "${URLS[@]}"; do
  log "try ${url}"
  if fetch_url "$url" "$ARCHIVE"; then
    DOWNLOADED="$url"
    break
  fi
  log "WARN: fetch failed, trying next mirror…"
done

if [[ -z "$DOWNLOADED" || ! -s "$ARCHIVE" ]]; then
  log "ERROR: could not download Agent package from GitHub/Gitee"
  log "hint: set UDHUB_SOURCE=gitee or pin UDHUB_VERSION=vX.Y.Z"
  exit 1
fi

log "source ok → ${DOWNLOADED}"
mkdir -p "${TMP}/extract"
tar -xzf "$ARCHIVE" -C "${TMP}/extract"

SRC="$(find "${TMP}/extract" -type d -path '*/custom_components/udhub_agent' | head -1 || true)"
if [[ -z "$SRC" ]]; then
  if [[ -f "${TMP}/extract"/*/manifest.json ]]; then
    SRC="$(dirname "$(find "${TMP}/extract" -name manifest.json | head -1)")"
  fi
fi
if [[ -z "$SRC" || ! -f "${SRC}/manifest.json" ]]; then
  log "ERROR: udhub_agent component not found in archive"
  exit 1
fi

mkdir -p "$(dirname "$COMPONENT_DST")"
rm -rf "$COMPONENT_DST"
cp -R "$SRC" "$COMPONENT_DST"
VER="$(python3 -c "import json;print(json.load(open('${COMPONENT_DST}/manifest.json')).get('version',''))" 2>/dev/null || true)"
log "installed ${VER:-unknown} → ${COMPONENT_DST}"
log "请在 HA：设置 → 系统 → 重启 后添加集成「UDHUB Agent」"
log "云端 URL 默认 https://www.udhub.com"
log "切源：UDHUB_SOURCE=gitee|github|auto · 锁版本：UDHUB_VERSION=v${VER:-X.Y.Z}"
