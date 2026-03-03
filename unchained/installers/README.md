Native installer assets for `/web/download-installer`.

Default preferred files:

- macOS: `unchained-installer-mac.dmg`, then `unchained-installer-mac.pkg`
- Windows: `unchained-installer-windows.msi`, then `unchained-installer-windows.exe`

Build mac package locally:

- `./installers/build_mac_installer.sh`

This generates `installers/unchained-installer-mac.pkg`.

Build mac DMG wrapper (recommended user-facing artifact):

- `./installers/build_mac_dmg.sh`

This generates `installers/unchained-installer-mac.dmg` from the `.pkg`.

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
