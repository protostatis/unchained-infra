#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNCHAINED_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_EXE="${1:-$SCRIPT_DIR/unchained-installer-windows.exe}"

if ! command -v makensis >/dev/null 2>&1; then
  echo "ERROR: makensis is required. Install with: brew install nsis" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required." >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

PAYLOAD_DIR="$WORKDIR/payload"
NSI_FILE="$WORKDIR/installer.nsi"

mkdir -p "$PAYLOAD_DIR"
mkdir -p "$(dirname "$OUT_EXE")"
rm -f "$OUT_EXE"

PYTHONPATH="$UNCHAINED_DIR" python3 - "$PAYLOAD_DIR" "$NSI_FILE" "$OUT_EXE" <<'PY'
from __future__ import annotations

import io
import os
import stat
import sys
import zipfile
from pathlib import Path

from agent_package import VERSION, build_agent_zip

payload_dir = Path(sys.argv[1]).resolve()
nsi_file = Path(sys.argv[2]).resolve()
out_exe = Path(sys.argv[3]).resolve()

zip_bytes = build_agent_zip(api_key="", relay_host="api.unchainedsky.com", install_token="")
with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
    prefix = "unchained-agent/"
    for name in zf.namelist():
        if not name.startswith(prefix):
            continue
        rel = name[len(prefix):]
        if not rel:
            continue
        out_path = payload_dir / rel
        if name.endswith("/"):
            out_path.mkdir(parents=True, exist_ok=True)
            continue
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(zf.read(name))

env_path = payload_dir / ".env"
if env_path.exists():
    lines = []
    for line in env_path.read_text(encoding="utf-8").splitlines(keepends=True):
        if line.startswith("UNCHAINED_API_KEY="):
            lines.append("UNCHAINED_API_KEY=\n")
        elif line.startswith("UNCHAINED_INSTALL_TOKEN="):
            lines.append("UNCHAINED_INSTALL_TOKEN=\n")
        else:
            lines.append(line)
    env_path.write_text("".join(lines), encoding="utf-8")

for path in ("start.sh", "stop.sh", "update.sh"):
    p = payload_dir / path
    if p.exists():
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

(payload_dir / "version.txt").write_text(VERSION, encoding="utf-8")

payload_win = str(payload_dir).replace("/", "\\")
out_win = str(out_exe).replace("/", "\\")

nsi_file.write_text(
    f"""!include "MUI2.nsh"
Unicode true
Name "Unchained Agent"
OutFile "{out_win}"
InstallDir "$PROFILE\\unchained-agent"
InstallDirRegKey HKCU "Software\\Unchained Agent" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma

!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\\start.bat"
!define MUI_FINISHPAGE_RUN_TEXT "Start Unchained Agent now"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "Install" SEC_INSTALL
  SetOutPath "$INSTDIR"

  ; Preserve existing credentials for update/reinstall flows.
  IfFileExists "$INSTDIR\\.env" 0 +2
  CopyFiles /SILENT "$INSTDIR\\.env" "$TEMP\\unchained-agent.env.bak"

  File /r "{payload_win}\\*"

  IfFileExists "$TEMP\\unchained-agent.env.bak" 0 +3
  CopyFiles /SILENT "$TEMP\\unchained-agent.env.bak" "$INSTDIR\\.env"
  Delete "$TEMP\\unchained-agent.env.bak"

  WriteUninstaller "$INSTDIR\\uninstall.exe"
  WriteRegStr HKCU "Software\\Unchained Agent" "InstallDir" "$INSTDIR"

  CreateDirectory "$SMPROGRAMS\\Unchained Agent"
  CreateShortcut "$SMPROGRAMS\\Unchained Agent\\Start Unchained Agent.lnk" "$INSTDIR\\start.bat"
  CreateShortcut "$SMPROGRAMS\\Unchained Agent\\Stop Unchained Agent.lnk" "$INSTDIR\\stop.bat"
  CreateShortcut "$SMPROGRAMS\\Unchained Agent\\Uninstall Unchained Agent.lnk" "$INSTDIR\\uninstall.exe"
SectionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\\Unchained Agent\\Start Unchained Agent.lnk"
  Delete "$SMPROGRAMS\\Unchained Agent\\Stop Unchained Agent.lnk"
  Delete "$SMPROGRAMS\\Unchained Agent\\Uninstall Unchained Agent.lnk"
  RMDir "$SMPROGRAMS\\Unchained Agent"
  DeleteRegKey HKCU "Software\\Unchained Agent"
  RMDir /r "$INSTDIR"
SectionEnd
""",
    encoding="utf-8",
)
PY

makensis "$NSI_FILE" >/dev/null

if [ ! -f "$OUT_EXE" ]; then
  echo "ERROR: NSIS did not produce installer output: $OUT_EXE" >&2
  exit 1
fi

echo "Built windows installer: $OUT_EXE"
