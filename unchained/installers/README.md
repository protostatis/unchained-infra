Native installer assets for `/web/download-installer`.

Default preferred files:

- macOS: `unchained-installer-mac.dmg`, then `unchained-installer-mac.pkg`
- Windows: `unchained-installer-windows.msi`, then `unchained-installer-windows.exe`

Build mac package locally:

- `./installers/build_mac_installer.sh`

This generates `installers/unchained-installer-mac.pkg`.

Build mac DMG wrapper (recommended user-facing artifact):

- `./installers/build_mac_dmg.sh`

This generates a drag-install DMG with:

- `Unchained Installer.app`
- `Applications` shortcut

User flow is Finder-native:

1. Drag `Unchained Installer.app` to Applications.
2. Open the app once to bootstrap the local agent.

Build Windows installer executable:

- `./installers/build_windows_installer.sh`

This generates:

- `installers/unchained-installer-windows.exe`

Prerequisite:

- `makensis` (NSIS compiler), e.g. `brew install nsis`

For production trust/signing:

- See `installers/DEVELOPER_ID_NOTARIZATION.md` for Developer ID signing + notarization.

You can override paths/names with env vars.

List form (preferred):

- `UNCHAINED_MAC_INSTALLER_FILES` (comma-separated, in priority order)
- `UNCHAINED_WINDOWS_INSTALLER_FILES` (comma-separated, in priority order)

Legacy single-file form (still supported):

- `UNCHAINED_INSTALLER_ASSETS_DIR`
- `UNCHAINED_MAC_INSTALLER_FILE`
- `UNCHAINED_WINDOWS_INSTALLER_FILE`

Behavior:

- If a native installer file exists, `/web/download-installer` serves it directly.
- If native files are missing, the endpoint returns `503` by default.
- Set `UNCHAINED_ALLOW_SCRIPT_INSTALLER=1` only for temporary script fallback.

### Desktop app (beta)

The macOS desktop app is a separate client with its own artifact and flag:

- artifact: `unchained-desktop-mac-arm64.dmg` (signed and notarized; built by the desktop
  repo's `installers/build_mac_app.sh`), placed in this directory;
- served by `GET /web/download-desktop` (session or install token; macOS only) under its
  own analytics cta, `download_desktop`;
- enabled with `UNCHAINED_DESKTOP_BETA=1` in the deployment's `.env` — compose passes it to
  the web service. The `/install` card appears only when the flag is on **and** this file is
  present, so turning the flag on without the artifact cannot publish a download that 503s.

The dmg is gitignored, and `deploy.sh` uploads only what exists in this directory on the
runner, so a release must stage it here before enabling the flag. The filename is fixed, so a
redeploy overwrites the previous build rather than accumulating stale versions.
