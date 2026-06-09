#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNCHAINED_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_DMG="${1:-$SCRIPT_DIR/unchained-installer-mac.dmg}"
VOL_NAME="${UNCHAINED_DMG_VOLUME_NAME:-Unchained Installer}"
APP_NAME="Unchained Installer.app"

if ! command -v hdiutil >/dev/null 2>&1; then
  echo "ERROR: hdiutil is required (macOS)." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required." >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

APP_ROOT="$WORKDIR/$APP_NAME/Contents"
MACOS_DIR="$APP_ROOT/MacOS"
RES_DIR="$APP_ROOT/Resources"
TEMPLATE_DIR="$RES_DIR/agent-template"
DMG_ROOT="$WORKDIR/dmg-root"

mkdir -p "$MACOS_DIR" "$TEMPLATE_DIR" "$DMG_ROOT"

PYTHONPATH="$UNCHAINED_DIR" python3 - "$TEMPLATE_DIR" <<'PY'
from __future__ import annotations

import io
import os
import stat
import sys
import zipfile

from agent_package import build_agent_zip

dest = sys.argv[1]
zip_bytes = build_agent_zip(api_key="", relay_host="api.unchainedsky.com", install_token="")
with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
    prefix = "unchained-agent/"
    for name in zf.namelist():
        if not name.startswith(prefix):
            continue
        rel = name[len(prefix):]
        if not rel:
            continue
        out_path = os.path.join(dest, rel)
        if name.endswith("/"):
            os.makedirs(out_path, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(zf.read(name))

env_path = os.path.join(dest, ".env")
if os.path.exists(env_path):
    lines = []
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("UNCHAINED_API_KEY="):
                lines.append("UNCHAINED_API_KEY=\n")
            elif line.startswith("UNCHAINED_INSTALL_TOKEN="):
                lines.append("UNCHAINED_INSTALL_TOKEN=\n")
            else:
                lines.append(line)
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)

for path in ("start.sh", "stop.sh", "update.sh"):
    p = os.path.join(dest, path)
    if os.path.exists(p):
        mode = os.stat(p).st_mode
        os.chmod(p, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
PY

cat > "$APP_ROOT/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>
  <string>Unchained Installer</string>
  <key>CFBundleDisplayName</key>
  <string>Unchained Installer</string>
  <key>CFBundleIdentifier</key>
  <string>com.unchained.installer.app</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleExecutable</key>
  <string>launch-unchained</string>
  <key>LSMinimumSystemVersion</key>
  <string>12.0</string>
</dict>
</plist>
PLIST

cat > "$MACOS_DIR/launch-unchained" <<'COMMAND'
#!/bin/bash
set -euo pipefail

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE_DIR="$APP_ROOT/Resources/agent-template"
DEST="$HOME/unchained-agent"
ENV_BACKUP="$DEST/.env.preinstall.$$"

mkdir -p "$DEST"
if [ -f "$DEST/.env" ]; then
  cp "$DEST/.env" "$ENV_BACKUP"
fi
cp -R "$TEMPLATE_DIR"/. "$DEST"/
if [ -f "$ENV_BACKUP" ]; then
  mv -f "$ENV_BACKUP" "$DEST/.env"
fi
chmod +x "$DEST/start.sh" "$DEST/stop.sh" "$DEST/update.sh" 2>/dev/null || true

if command -v osascript >/dev/null 2>&1; then
  osascript <<'OSA' >/dev/null 2>&1 || true
tell application "Terminal"
  do script "cd \"$HOME/unchained-agent\" && ./start.sh --enable-autostart"
  activate
end tell
OSA
fi
COMMAND
chmod +x "$MACOS_DIR/launch-unchained"

cat > "$DMG_ROOT/README.txt" <<'README'
Drag Install Flow
=================

1) Drag "Unchained Installer.app" into Applications.
2) Open it once from Applications.
3) Complete browser authorization when prompted.
README

cp -R "$WORKDIR/$APP_NAME" "$DMG_ROOT/$APP_NAME"
ln -s /Applications "$DMG_ROOT/Applications"

mkdir -p "$(dirname "$OUT_DMG")"
rm -f "$OUT_DMG"

hdiutil create \
  -volname "$VOL_NAME" \
  -srcfolder "$DMG_ROOT" \
  -format UDZO \
  "$OUT_DMG"

echo "Built mac disk image: $OUT_DMG"
