#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INSTALLER_CERT="${INSTALLER_CERT:-}"
APP_CERT="${APP_CERT:-}"
NOTARY_PROFILE="${NOTARY_PROFILE:-}"

BUILD_DMG="${BUILD_DMG:-1}"

UNSIGNED_PKG="$SCRIPT_DIR/unchained-installer-mac.pkg"
SIGNED_PKG_TMP="$SCRIPT_DIR/unchained-installer-mac-signed.pkg"
FINAL_PKG="$SCRIPT_DIR/unchained-installer-mac.pkg"
FINAL_DMG="$SCRIPT_DIR/unchained-installer-mac.dmg"

if [[ -z "$INSTALLER_CERT" ]]; then
  echo "ERROR: INSTALLER_CERT is required." >&2
  echo "Example: INSTALLER_CERT='Developer ID Installer: Your Company (TEAMID)'" >&2
  exit 1
fi
if [[ -z "$NOTARY_PROFILE" ]]; then
  echo "ERROR: NOTARY_PROFILE is required." >&2
  echo "Create once: xcrun notarytool store-credentials <profile> --apple-id <id> --team-id <team> --password <app-password>" >&2
  exit 1
fi
if [[ "$BUILD_DMG" == "1" && -z "$APP_CERT" ]]; then
  echo "ERROR: APP_CERT is required when BUILD_DMG=1." >&2
  echo "Example: APP_CERT='Developer ID Application: Your Company (TEAMID)'" >&2
  exit 1
fi

for tool in pkgbuild productsign xcrun stapler; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "ERROR: missing required tool: $tool" >&2
    exit 1
  fi
done

if ! security find-identity -v -p basic | grep -Fq "$INSTALLER_CERT"; then
  echo "ERROR: Installer certificate not found in keychain:" >&2
  echo "  $INSTALLER_CERT" >&2
  exit 1
fi

if [[ "$BUILD_DMG" == "1" ]]; then
  if ! command -v codesign >/dev/null 2>&1; then
    echo "ERROR: codesign is required for DMG flow." >&2
    exit 1
  fi
  if ! security find-identity -v -p codesigning | grep -Fq "$APP_CERT"; then
    echo "ERROR: Application certificate not found in keychain:" >&2
    echo "  $APP_CERT" >&2
    exit 1
  fi
fi

echo "==> Building unsigned PKG..."
"$SCRIPT_DIR/build_mac_installer.sh" "$UNSIGNED_PKG"

echo "==> Signing PKG..."
rm -f "$SIGNED_PKG_TMP"
productsign --sign "$INSTALLER_CERT" "$UNSIGNED_PKG" "$SIGNED_PKG_TMP"
mv -f "$SIGNED_PKG_TMP" "$FINAL_PKG"
pkgutil --check-signature "$FINAL_PKG" >/dev/null

echo "==> Notarizing PKG..."
xcrun notarytool submit "$FINAL_PKG" --keychain-profile "$NOTARY_PROFILE" --wait
xcrun stapler staple "$FINAL_PKG"
xcrun stapler validate "$FINAL_PKG"
spctl -a -vvv -t install "$FINAL_PKG" || true

if [[ "$BUILD_DMG" == "1" ]]; then
  echo "==> Building signed DMG payload app..."
  UNCHAINED_APP_CERT="$APP_CERT" "$SCRIPT_DIR/build_mac_dmg.sh" "$FINAL_DMG"

  echo "==> Notarizing DMG..."
  xcrun notarytool submit "$FINAL_DMG" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$FINAL_DMG"
  xcrun stapler validate "$FINAL_DMG"
fi

echo ""
echo "Done:"
echo "  PKG: $FINAL_PKG"
if [[ "$BUILD_DMG" == "1" ]]; then
  echo "  DMG: $FINAL_DMG"
fi
