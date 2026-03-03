Native installer assets for `/web/download-installer`.

Default expected files:

- `unchained-installer-mac.pkg`
- `unchained-installer-windows.exe`

Build mac package locally:

- `./installers/build_mac_installer.sh`

This generates `installers/unchained-installer-mac.pkg`.

For production trust/signing:

- See `installers/DEVELOPER_ID_NOTARIZATION.md` for Developer ID signing + notarization.

You can override paths/names with env vars:

- `UNCHAINED_INSTALLER_ASSETS_DIR`
- `UNCHAINED_MAC_INSTALLER_FILE`
- `UNCHAINED_WINDOWS_INSTALLER_FILE`

Behavior:

- If a native installer file exists, `/web/download-installer` serves it directly.
- If native files are missing, the endpoint returns `503` by default.
- Set `UNCHAINED_ALLOW_SCRIPT_INSTALLER=1` only for temporary script fallback.
