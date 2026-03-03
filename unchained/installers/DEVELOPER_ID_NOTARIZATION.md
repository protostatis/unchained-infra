# macOS Developer ID Signing + Notarization

This runbook explains how to sign and notarize the Unchained macOS installer package produced by:

```bash
./installers/build_mac_installer.sh
```

The unsigned output is:

- `installers/unchained-installer-mac.pkg`

After following this guide, publish:

- `installers/unchained-installer-mac.pkg` (required, signed/notarized)
- `installers/unchained-installer-mac.dmg` (optional but recommended wrapper for user-friendly download UX)

---

## 1) Prerequisites

- Active Apple Developer Program membership.
- A **Developer ID Installer** certificate in your login keychain.
- Xcode Command Line Tools (`xcrun`, `productsign`, `stapler`, `notarytool`).
- Team ID.

Check tools:

```bash
xcrun --find notarytool
xcrun --find stapler
productsign --help >/dev/null
```

List signing identities:

```bash
security find-identity -v -p basic | grep "Developer ID Installer"
```

---

## 2) Create/Install Developer ID Installer Certificate

In Apple Developer Certificates, create:

- `Developer ID Installer`

Install the `.cer` into Keychain Access and confirm it appears under "My Certificates" with private key attached.

Reference:
- https://developer.apple.com/help/account/certificates/create-developer-id-certificates/

---

## 3) Build Unsigned Package

From `unchained-infra/unchained`:

```bash
./installers/build_mac_installer.sh
```

Output:

- `installers/unchained-installer-mac.pkg` (unsigned)

---

## 4) Sign the Package (Developer ID Installer)

Use your exact certificate common name:

```bash
CERT="Developer ID Installer: <Your Company, Inc.> (<TEAMID>)"
IN_PKG="installers/unchained-installer-mac.pkg"
SIGNED_PKG="installers/unchained-installer-mac-signed.pkg"

productsign --sign "$CERT" "$IN_PKG" "$SIGNED_PKG"
```

Verify signature:

```bash
pkgutil --check-signature "$SIGNED_PKG"
```

Expected: shows `Developer ID Installer` signer chain (not "no signature").

---

## 5) Configure Notary Credentials (one-time)

Store credentials in keychain profile (recommended):

```bash
xcrun notarytool store-credentials "unchained-notary" \
  --apple-id "<apple-id-email>" \
  --team-id "<TEAMID>" \
  --password "<app-specific-password>"
```

Alternative: App Store Connect API key auth is also supported by `notarytool`.

Reference:
- https://developer.apple.com/documentation/security/customizing-the-notarization-workflow

---

## 6) Submit for Notarization

```bash
xcrun notarytool submit "$SIGNED_PKG" \
  --keychain-profile "unchained-notary" \
  --wait
```

If accepted, optionally inspect logs:

```bash
xcrun notarytool log <submission-id> \
  --keychain-profile "unchained-notary"
```

---

## 7) Staple Notarization Ticket

```bash
xcrun stapler staple "$SIGNED_PKG"
xcrun stapler validate "$SIGNED_PKG"
```

Gatekeeper check:

```bash
spctl -a -vvv -t install "$SIGNED_PKG"
```

Expected: accepted result with Developer ID / notarization context.

---

## 8) Publish to Unchained Installer Asset Path

Publish package artifact:

```bash
cp "$SIGNED_PKG" "installers/unchained-installer-mac.pkg"
```

Optional DMG wrapper (recommended):

```bash
# Build drag-install DMG (contains Unchained Installer.app + Applications shortcut).
./installers/build_mac_dmg.sh "installers/unchained-installer-mac.dmg"

# Sign the DMG with Developer ID Application cert.
APP_CERT="Developer ID Application: <Your Company, Inc.> (<TEAMID>)"
codesign --force --timestamp --sign "$APP_CERT" "installers/unchained-installer-mac.dmg"

# Notarize + staple the DMG.
xcrun notarytool submit "installers/unchained-installer-mac.dmg" \
  --keychain-profile "unchained-notary" \
  --wait
xcrun stapler staple "installers/unchained-installer-mac.dmg"
xcrun stapler validate "installers/unchained-installer-mac.dmg"
```

`/web/download-installer?os=mac` prefers `.dmg` first, then falls back to `.pkg`.

---

## 9) CI/Automation Notes

- Keep certificate private key on secure build host or signing service.
- Never commit Apple credentials.
- Add a release script that performs:
  1. build pkg
  2. productsign
  3. notarytool submit --wait
  4. stapler staple/validate
  5. build/sign/notarize/staple dmg (optional, recommended)
  6. publish final pkg + dmg

---

## Common Failure Modes

- `no signature`: `productsign` step was skipped or used wrong certificate.
- notarization rejected: use `notarytool log` for precise reason.
- staple fails: notarization not accepted yet or wrong artifact.
- Gatekeeper blocks install: signed pkg expired/revoked cert, or unstapled/not notarized package.
