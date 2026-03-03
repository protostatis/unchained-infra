#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNCHAINED_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_PKG="${1:-$SCRIPT_DIR/unchained-installer-mac.pkg}"

if ! command -v pkgbuild >/dev/null 2>&1; then
  echo "ERROR: pkgbuild is required (Xcode Command Line Tools)." >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

PAYLOAD_ROOT="$WORKDIR/root"
SCRIPTS_DIR="$WORKDIR/scripts"
TEMPLATE_DIR="$PAYLOAD_ROOT/Library/Application Support/Unchained/agent-template"

mkdir -p "$TEMPLATE_DIR" "$SCRIPTS_DIR"

PYTHONPATH="$UNCHAINED_DIR" python3 - "$TEMPLATE_DIR" <<'PY'
from __future__ import annotations

import io
import os
import stat
import sys
import zipfile

from agent_package import VERSION, build_agent_zip

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

version_path = os.path.join(dest, "version.txt")
with open(version_path, "w", encoding="utf-8") as f:
    f.write(VERSION)
PY

find "$PAYLOAD_ROOT" -name '._*' -type f -delete 2>/dev/null || true

cat > "$SCRIPTS_DIR/postinstall" <<'POSTINSTALL'
#!/bin/bash
set -euo pipefail

SRC="/Library/Application Support/Unchained/agent-template"
TARGET_USER="$(stat -f%Su /dev/console || true)"
if [ -z "$TARGET_USER" ] || [ "$TARGET_USER" = "root" ]; then
  echo "No console user detected. Installed template at: $SRC"
  exit 0
fi

TARGET_HOME="$(dscl . -read "/Users/$TARGET_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
if [ -z "$TARGET_HOME" ]; then
  TARGET_HOME="/Users/$TARGET_USER"
fi

DEST="$TARGET_HOME/unchained-agent"
mkdir -p "$DEST"
cp -R "$SRC"/. "$DEST"/
chown -R "$TARGET_USER":staff "$DEST" || true
chmod +x "$DEST/start.sh" "$DEST/stop.sh" "$DEST/update.sh" 2>/dev/null || true

# Best-effort: open Terminal and start onboarding for the logged-in user.
if command -v launchctl >/dev/null 2>&1; then
  USER_UID="$(id -u "$TARGET_USER" 2>/dev/null || true)"
  if [ -n "$USER_UID" ]; then
    launchctl asuser "$USER_UID" osascript <<'OSA' >/dev/null 2>&1 || true
tell application "Terminal"
  do script "cd \"$HOME/unchained-agent\" && ./start.sh"
  activate
end tell
OSA
  fi
fi

echo "Installed Unchained Agent to: $DEST"
exit 0
POSTINSTALL

chmod +x "$SCRIPTS_DIR/postinstall"

mkdir -p "$(dirname "$OUT_PKG")"
rm -f "$OUT_PKG"

VERSION="$(PYTHONPATH="$UNCHAINED_DIR" python3 - <<'PY'
from agent_package import VERSION
print(VERSION)
PY
)"

pkgbuild \
  --root "$PAYLOAD_ROOT" \
  --scripts "$SCRIPTS_DIR" \
  --identifier "com.unchained.agent" \
  --version "$VERSION" \
  --install-location "/" \
  "$OUT_PKG"

echo "Built mac installer: $OUT_PKG"
