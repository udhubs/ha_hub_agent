#!/usr/bin/env bash
# Install udhub_agent into a Home Assistant config directory.
# Usage:
#   ./install.sh /config
#   UDHUB_VERSION=v0.4.43 ./install.sh /config
# Dual-source clone (when fetching this repo itself):
#   UDHUB_SOURCE=gitee git clone https://gitee.com/udhubs/ha_hub_agent.git
set -euo pipefail

DEST="${1:-}"
if [[ -z "$DEST" || ! -d "$DEST" ]]; then
  echo "usage: $0 <ha_config_path>" >&2
  echo "example: $0 /config" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPONENT_NAME="udhub_agent"
SOURCE="$SCRIPT_DIR/custom_components/$COMPONENT_NAME"
TARGET_ROOT="$DEST/custom_components"
TARGET="$TARGET_ROOT/$COMPONENT_NAME"

if [[ ! -d "$SOURCE" ]]; then
  echo "ERROR: missing $SOURCE (run from ha_hub_agent repo root)" >&2
  exit 1
fi

mkdir -p "$TARGET_ROOT"
rm -rf "$TARGET"
cp -R "$SOURCE" "$TARGET"

VERSION="$(python3 -c "import json; print(json.load(open('$TARGET/manifest.json'))['version'])" 2>/dev/null || echo unknown)"
echo "OK: $COMPONENT_NAME v$VERSION → $TARGET"
echo "Restart Home Assistant to load the integration."
