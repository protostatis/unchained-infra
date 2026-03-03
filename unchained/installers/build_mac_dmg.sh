#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IN_PKG="${1:-$SCRIPT_DIR/unchained-installer-mac.pkg}"
OUT_DMG="${2:-$SCRIPT_DIR/unchained-installer-mac.dmg}"
VOL_NAME="${UNCHAINED_DMG_VOLUME_NAME:-Unchained Installer}"

if ! command -v hdiutil >/dev/null 2>&1; then
  echo "ERROR: hdiutil is required (macOS)." >&2
  exit 1
fi
if [ ! -f "$IN_PKG" ]; then
  echo "ERROR: Input pkg not found: $IN_PKG" >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

PKG_NAME="Unchained Installer.pkg"
cp "$IN_PKG" "$WORKDIR/$PKG_NAME"

cat > "$WORKDIR/Install Unchained.command" <<'COMMAND'
#!/bin/bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
open "$HERE/Unchained Installer.pkg"
COMMAND
chmod +x "$WORKDIR/Install Unchained.command"

cat > "$WORKDIR/README.txt" <<'README'
Unchained Installer
===================

1) Double-click "Install Unchained.command"
   or open "Unchained Installer.pkg" directly.
2) Follow macOS installer prompts.
3) Complete browser authorization when prompted.
README

mkdir -p "$(dirname "$OUT_DMG")"
rm -f "$OUT_DMG"

hdiutil create \
  -volname "$VOL_NAME" \
  -srcfolder "$WORKDIR" \
  -format UDZO \
  "$OUT_DMG"

echo "Built mac disk image: $OUT_DMG"
