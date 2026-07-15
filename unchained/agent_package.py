"""agent_package.py — Build a downloadable ZIP containing the agent runtime.

Bundles the chat agent + Chrome bridge for a user's local machine.
The package contains only a short-lived install token; start.sh exchanges
it for the real API key on first run. Proprietary tools (DDM, intel, CDP)
stay server-side — the packaged cdp_tool.py calls the server API.

Usage (from web.py):
    from agent_package import build_agent_zip
    zip_bytes = build_agent_zip(api_key="uc_live_...", relay_host="api.unchainedsky.com")
"""

import io
import json
import hashlib
import os
from pathlib import Path
from typing import Optional
import zipfile

VERSION = "0.3.116"  # reconnect answer loss: connection-independent emit, grace period, relay gen scoping
# 0.3.49-0.3.52 were consumed by earlier iterations of the startup-tab
# fix during PR review; keep the version monotonic for packaged clients.
# 0.3.57 is the first packaged client version that advertises the
# one-click Research Desk install capability to the hosted page.
# 0.3.46 is the first packaged client version that reliably includes the
# archive-restore safety fix on users' machines, so anything older must update.
MIN_VERSION = "0.3.46"
RESEARCH_DESK_VERSION = "0.1.0"
_RESEARCH_DESK_VENDOR_ROOT_FILES = ("pyproject.toml", "README.md", "setup.py")
_RESEARCH_DESK_VENDOR_PACKAGE_DIR = "unchained_pyreplab"
_RESEARCH_DESK_VENDOR_MANIFEST = "manifest.json"


def _resolve_research_desk_vendor_dir(module_path: Optional[Path] = None) -> Path:
    """Find the vendored Research Desk tree in both repo and container layouts."""
    resolved_module_path = Path(module_path or __file__).resolve()
    candidates: list[Path] = []
    for base_dir in (resolved_module_path.parent.parent, resolved_module_path.parent):
        candidate = base_dir / "research_desk_vendor"
        if candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


_RESEARCH_DESK_VENDOR_DIR = _resolve_research_desk_vendor_dir()

# Source files to include as-is (non-proprietary)
_PACKAGE_FILES = {
    # dest path in ZIP → source filename
    "unchained/chrome_bridge.py": "chrome_bridge.py",
    "unchained/chat_agent_cli.py": "chat_agent_cli.py",
    "unchained/chat_event_transport.py": "chat_event_transport.py",
    "unchained/scheduled_tasks.py": "scheduled_tasks.py",
    "unchained/scheduler_tool.py": "scheduler_tool.py",
    "scheduled_jobs.json": "scheduled_jobs.example.json",
    "unchained/auth.py": "auth.py",
    "unchained/nudge.py": "nudge.py",
    "unchained/cdp_tool.py": "cdp_tool_packaged.py",
}

_REQUIREMENTS = """\
websockets>=13.0
httpx
aiohttp==3.10.11
PyJWT==2.9.0
cryptography>=42.0
certifi>=2026.1.4
"""


# NOTE: The packaged cdp_tool.py source of truth is cdp_tool_packaged.py,
# mapped via _PACKAGE_FILES above. A legacy inline string was removed here
# because _PACKAGE_FILES overwrites it at ZIP build time.


def _make_env(relay_host: str, install_token: str = "") -> str:
    return f"""\
UNCHAINED_API_KEY=
UNCHAINED_INSTALL_TOKEN={install_token}
UNCHAINED_SERVER=wss://{relay_host}/chat/ws
UNCHAINED_RELAY_HOST={relay_host}
UNCHAINED_RELAY_PORT=443
UNCHAINED_API_URL=https://{relay_host}
CODEX_MAX_RUNTIME_S=300
"""


_START_SH = r"""#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

DAEMON=true
ENABLE_AUTOSTART=false
DISABLE_AUTOSTART=false
for arg in "$@"; do
  case "$arg" in
    --foreground|-f) DAEMON=false ;;
    --daemon|-d) DAEMON=true ;;
    --enable-autostart) ENABLE_AUTOSTART=true ;;
    --disable-autostart) DISABLE_AUTOSTART=true ;;
  esac
done

AUTOSTART_LABEL="com.unchained.agent"
AUTOSTART_PLIST="$HOME/Library/LaunchAgents/$AUTOSTART_LABEL.plist"
AGENT_DIR="$(pwd)"
SCRIPT_PATH="$AGENT_DIR/start.sh"
OS_NAME="$(uname -s 2>/dev/null || echo unknown)"

install_autostart() {
  if [[ "$OS_NAME" != "Darwin" ]]; then
    echo "Autostart setup skipped: unsupported OS ($OS_NAME)."
    return 0
  fi
  if ! command -v launchctl >/dev/null 2>&1; then
    echo "Autostart setup skipped: launchctl not found."
    return 0
  fi
  if [[ "$(id -u)" == "0" ]]; then
    echo "ERROR: Do not run --enable-autostart with sudo."
    echo "  LaunchAgents run as your user — root access is not needed."
    echo "  Run: ./start.sh --enable-autostart"
    return 1
  fi
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$AUTOSTART_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$AUTOSTART_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$SCRIPT_PATH</string>
    <string>--daemon</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <!-- KeepAlive=false: start.sh --daemon forks the supervised loop into a
       background subshell and exits 0 quickly. The supervised loop is the
       supervisor (it has the crash circuit breaker + rollback). KeepAlive=true
       would loop infinitely re-spawning start.sh whenever it returns. -->
  <key>KeepAlive</key>
  <false/>
  <!-- AbandonProcessGroup=true: when start.sh --daemon exits after forking,
       launchd should NOT kill its child processes. The forked supervised loop
       must keep running. Default false would tear down the whole process tree. -->
  <key>AbandonProcessGroup</key>
  <true/>
  <key>WorkingDirectory</key>
  <string>$AGENT_DIR</string>
  <key>StandardOutPath</key>
  <string>$AGENT_DIR/autostart.log</string>
  <key>StandardErrorPath</key>
  <string>$AGENT_DIR/autostart.log</string>
</dict>
</plist>
PLIST
  launchctl bootout "gui/$(id -u)/$AUTOSTART_LABEL" >/dev/null 2>&1 || true
  launchctl enable "gui/$(id -u)/$AUTOSTART_LABEL" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$AUTOSTART_PLIST"
  echo "Autostart enabled: $AUTOSTART_LABEL"
  echo "LaunchAgent: $AUTOSTART_PLIST"
}

disable_autostart() {
  if [[ "$OS_NAME" != "Darwin" ]]; then
    echo "Autostart disable skipped: unsupported OS ($OS_NAME)."
    return 0
  fi
  if ! command -v launchctl >/dev/null 2>&1; then
    echo "Autostart disable skipped: launchctl not found."
    return 0
  fi
  launchctl bootout "gui/$(id -u)/$AUTOSTART_LABEL" >/dev/null 2>&1 || true
  launchctl disable "gui/$(id -u)/$AUTOSTART_LABEL" >/dev/null 2>&1 || true
  rm -f "$AUTOSTART_PLIST"
  echo "Autostart disabled: $AUTOSTART_LABEL"
}

if $DISABLE_AUTOSTART; then
  disable_autostart
  exit 0
fi

if $ENABLE_AUTOSTART; then
  # Register launchd plist + exit. RunAtLoad=true causes launchd to immediately
  # spawn `start.sh --daemon`, which forks the supervised loop. So this single
  # command both starts the daemon now AND persists across reboots.
  install_autostart
  exit 0
fi

# Load config
if [ ! -f .env ]; then
  echo "ERROR: .env not found. Re-download from the web UI."; exit 1
fi
set -a; source .env; set +a

# If no API key and no install token are configured, run browser-based claim flow.
if [ -z "${UNCHAINED_API_KEY:-}" ] && [ -z "${UNCHAINED_INSTALL_TOKEN:-}" ]; then
  API_URL="${UNCHAINED_API_URL:-https://api.unchainedsky.com}"
  CLAIM_ID=$(python3 - <<'PY'
import secrets
print(secrets.token_hex(16))
PY
)
  CLAIM_SECRET=$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)
  START_PAYLOAD=$(CLAIM_ID="$CLAIM_ID" CLAIM_SECRET="$CLAIM_SECRET" python3 - <<'PY'
import json, os
print(json.dumps({"claim_id": os.environ["CLAIM_ID"], "claim_secret": os.environ["CLAIM_SECRET"]}))
PY
)
  curl -sf \
    -H "Content-Type: application/json" \
    -d "$START_PAYLOAD" \
    "$API_URL/web/install/claim/start" >/dev/null || {
      echo "ERROR: could not initialize installer auth claim."; exit 1;
    }
  CLAIM_URL="$API_URL/install/claim/$CLAIM_ID"
  echo "Authorize this installation in your browser:"
  echo "  $CLAIM_URL"
  if command -v open >/dev/null 2>&1; then
    open "$CLAIM_URL" >/dev/null 2>&1 || true
  fi
  echo "Waiting for approval..."
  INSTALL_TOKEN=""
  for _ in $(seq 1 150); do
    POLL_PAYLOAD=$(CLAIM_ID="$CLAIM_ID" CLAIM_SECRET="$CLAIM_SECRET" python3 - <<'PY'
import json, os
print(json.dumps({"claim_id": os.environ["CLAIM_ID"], "claim_secret": os.environ["CLAIM_SECRET"]}))
PY
)
    POLL_RESP=$(curl -sf \
      -H "Content-Type: application/json" \
      -d "$POLL_PAYLOAD" \
      "$API_URL/web/install/claim/poll" 2>/dev/null || true)
    STATUS=$(printf '%s' "$POLL_RESP" | python3 -c 'import json,sys; print((json.load(sys.stdin).get("status","") if sys.stdin.readable() else ""))' 2>/dev/null || true)
    if [ "$STATUS" = "approved" ]; then
      INSTALL_TOKEN=$(printf '%s' "$POLL_RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("install_token",""))' 2>/dev/null || true)
      break
    fi
    if [ "$STATUS" = "expired" ]; then
      break
    fi
    sleep 2
  done
  if [ -z "$INSTALL_TOKEN" ]; then
    echo "ERROR: installer approval timed out. Re-run ./start.sh and approve in browser."
    exit 1
  fi
  grep -v '^UNCHAINED_INSTALL_TOKEN=' .env > .env.tmp || true
  printf 'UNCHAINED_INSTALL_TOKEN=%s\n' "$INSTALL_TOKEN" >> .env.tmp
  mv .env.tmp .env
  export UNCHAINED_INSTALL_TOKEN="$INSTALL_TOKEN"
fi

# Exchange the short-lived install token for the real API key on first run.
if [ -z "${UNCHAINED_API_KEY:-}" ] && [ -n "${UNCHAINED_INSTALL_TOKEN:-}" ]; then
  API_URL="${UNCHAINED_API_URL:-https://api.unchainedsky.com}"
  echo "Fetching agent credentials..."
  PAYLOAD=$(TOKEN="$UNCHAINED_INSTALL_TOKEN" python3 - <<'PY'
import json, os
print(json.dumps({"token": os.environ["TOKEN"]}))
PY
)
  BOOTSTRAP=$(curl -sf \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    "$API_URL/web/install/bootstrap") || {
      echo "ERROR: install token exchange failed."; exit 1;
    }
  NEW_KEY=$(printf '%s' "$BOOTSTRAP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("api_key",""))' 2>/dev/null || true)
  if [ -z "$NEW_KEY" ]; then
    ERR=$(printf '%s' "$BOOTSTRAP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("error",""))' 2>/dev/null || true)
    echo "ERROR: ${ERR:-Install token exchange failed.}"
    exit 1
  fi
  grep -v '^UNCHAINED_API_KEY=' .env | grep -v '^UNCHAINED_INSTALL_TOKEN=' > .env.tmp || true
  printf 'UNCHAINED_API_KEY=%s\n' "$NEW_KEY" >> .env.tmp
  mv .env.tmp .env
  export UNCHAINED_API_KEY="$NEW_KEY"
  unset UNCHAINED_INSTALL_TOKEN
fi

# launchd starts with a minimal PATH; add common CLI locations before any checks.
# Homebrew/system installs are placed ahead of ~/.local/bin so stale local shims do not win.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH:$HOME/.local/bin"

# Ensure uv is available (handles python resolution across all platforms)
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | UV_NO_MODIFY_PATH=1 sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# Install deps if needed
if [ ! -d .venv ]; then
  echo "Setting up Python environment..."
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

# Activate venv so Claude's Bash tool finds the right python
source .venv/bin/activate
if [ -z "${CLAUDE_BIN:-}" ] && command -v claude >/dev/null 2>&1; then
  export CLAUDE_BIN="$(command -v claude)"
fi
if [ -z "${CODEX_BIN:-}" ] && command -v codex >/dev/null 2>&1; then
  export CODEX_BIN="$(command -v codex)"
fi
if [ -z "${OPENCODE_BIN:-}" ] && command -v opencode >/dev/null 2>&1; then
  export OPENCODE_BIN="$(command -v opencode)"
fi

# Derive agent_id from API key (same hash as chat_agent_cli.py)
_DERIVED_AGENT_ID=""
if [ -n "${UNCHAINED_API_KEY:-}" ]; then
  _DERIVED_AGENT_ID=$(python3 -c "import hashlib; print('claude-' + hashlib.sha256('${UNCHAINED_API_KEY}'.encode()).hexdigest()[:8])")
fi

if $DAEMON; then
  # --- Daemon mode: run in background, log to file ---
  LOGFILE="$(pwd)/agent.log"
  PIDFILE="$(pwd)/.agent.pid"

  # Check if already running — verify cmdline to guard against stale PID files
  # (after reboot the PID slot may be reused by an unrelated process)
  if [ -f "$PIDFILE" ]; then
    _OLD_PID="$(cat "$PIDFILE")"
    if kill -0 "$_OLD_PID" 2>/dev/null && \
       ps -p "$_OLD_PID" -o args= 2>/dev/null | grep -q "start\.sh\|chat_agent_cli"; then
      echo "Agent is already running (PID $_OLD_PID)."
      echo "Stop: ./stop.sh"
      exit 0
    fi
    rm -f "$PIDFILE"
  fi

  # Daemon mode runs the supervised loop in a background subshell. It does
  # NOT touch launchd — autostart is opt-in via `--enable-autostart`.
  echo "Starting in daemon mode..."
  echo "Log file: $LOGFILE"

  # Launch both processes in a subshell, redirect to log
  (
    # --- Crash circuit breaker ---
    # If a process crashes MAX_RAPID_CRASHES times within RAPID_WINDOW seconds,
    # stop retrying and attempt a rollback to the previous version.
    MAX_RAPID_CRASHES=5
    RAPID_WINDOW=60  # seconds

    supervised_loop() {
      # Usage: supervised_loop <label> <command...>
      local label="$1"; shift
      local crash_times=()

      while true; do
        echo "[$(date)] Starting $label..."
        local start_epoch
        start_epoch=$(date +%s)
        set +e
        "$@"
        local exit_code=$?
        set -e
        local end_epoch
        end_epoch=$(date +%s)
        local runtime=$((end_epoch - start_epoch))

        echo "[$(date)] $label exited (code $exit_code, ran ${runtime}s)."

        # Only count rapid crashes (ran < 10 seconds)
        if [ "$runtime" -lt 10 ]; then
          crash_times+=("$end_epoch")
          # Trim old entries outside the window
          local cutoff=$((end_epoch - RAPID_WINDOW))
          local recent=()
          for t in "${crash_times[@]}"; do
            if [ "$t" -ge "$cutoff" ]; then
              recent+=("$t")
            fi
          done
          crash_times=("${recent[@]}")

          if [ "${#crash_times[@]}" -ge "$MAX_RAPID_CRASHES" ]; then
            echo "[$(date)] CIRCUIT BREAKER: $label crashed ${#crash_times[@]} times in ${RAPID_WINDOW}s."
            # Attempt rollback if backup exists
            if [ -d "unchained/.backup" ]; then
              echo "[$(date)] Rolling back $label to previous version..."
              cp -f unchained/.backup/*.py unchained/ 2>/dev/null || true
              if [ -f unchained/.backup/version.txt ]; then
                cp -f unchained/.backup/version.txt . 2>/dev/null || true
              fi
              echo "[$(date)] Rollback complete. Resetting crash counter."
              crash_times=()
              # Remove backup so we don't loop rollbacks
              rm -rf unchained/.backup
            else
              echo "[$(date)] No backup available. $label is stopped. Run ./update.sh to fix."
              return 1
            fi
          fi
        else
          # Healthy run — reset crash counter
          crash_times=()
        fi

        echo "[$(date)] Restarting $label in 5s..."
        sleep 5
      done
    }

    bridge_loop() {
      supervised_loop "Chrome bridge" \
        python unchained/chrome_bridge.py start \
          --relay "wss://$UNCHAINED_RELAY_HOST/tunnel"
    }

    cleanup() {
      if [ -n "${BRIDGE_SUP_PID:-}" ]; then
        kill "$BRIDGE_SUP_PID" 2>/dev/null || true
      fi
      pkill -f "chrome_bridge.py start" 2>/dev/null || true
    }

    trap "cleanup; exit" INT TERM

    bridge_loop &
    BRIDGE_SUP_PID=$!
    sleep 2

    # caffeinate -i prevents macOS App Nap from suspending the agent.
    # supervised_loop prints "Starting chat agent..." itself.
    AGENT_CMD=(env PYTHONUNBUFFERED=1 python unchained/chat_agent_cli.py)
    if command -v caffeinate &>/dev/null; then
      AGENT_CMD=(caffeinate -i -- "${AGENT_CMD[@]}")
    fi
    supervised_loop "chat agent" "${AGENT_CMD[@]}"
  ) >> "$LOGFILE" 2>&1 &
  DAEMON_PID=$!

  echo "$DAEMON_PID" > "$PIDFILE"
  echo "Agent started (PID $DAEMON_PID)"
  if [ -n "$_DERIVED_AGENT_ID" ]; then
    echo ""
    echo "  Agent ID:  $_DERIVED_AGENT_ID"
    echo "  API key:   ${UNCHAINED_API_KEY}"
    echo ""
    echo "  Add to Claude Code:"
    echo "    claude mcp add unchainedsky \\"
    echo "      https://api.unchainedsky.com/mcp \\"
    echo "      -t http \\"
    echo "      -H \"Authorization: Bearer ${UNCHAINED_API_KEY}\""
    echo ""
    echo "  Then restart Claude Code for tools to take effect."
  fi
  echo ""
  echo "  Logs:  tail -f $LOGFILE"
  echo "  Stop:  ./stop.sh"
else
  # --- Foreground mode: interactive terminal ---
  echo "Starting Chrome bridge..."
  python unchained/chrome_bridge.py start \
    --relay "wss://$UNCHAINED_RELAY_HOST/tunnel" &
  BRIDGE_PID=$!

  # Give bridge a moment to connect
  sleep 2

  echo "Starting chat agent..."
  trap "kill $BRIDGE_PID 2>/dev/null; exit" INT TERM
  # caffeinate -i prevents macOS App Nap from suspending the agent
  if command -v caffeinate &>/dev/null; then
    caffeinate -i -- env PYTHONUNBUFFERED=1 python unchained/chat_agent_cli.py
  else
    PYTHONUNBUFFERED=1 python unchained/chat_agent_cli.py
  fi
fi
"""


_STOP_SH = r"""#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

PIDFILE="$(pwd)/.agent.pid"
AUTOSTART_LABEL="com.unchained.agent"
OS_NAME="$(uname -s 2>/dev/null || echo unknown)"

stop_launchd_autostart() {
  if [[ "$OS_NAME" != "Darwin" ]]; then
    return 0
  fi
  if ! command -v launchctl >/dev/null 2>&1; then
    return 0
  fi
  launchctl bootout "gui/$(id -u)/$AUTOSTART_LABEL" >/dev/null 2>&1 && \
    echo "Stopped autostart job: $AUTOSTART_LABEL" || true
}

stop_launchd_autostart

if [ ! -f "$PIDFILE" ]; then
  echo "No agent PID file found. Is the agent running in daemon mode?"
  # Try to find and kill any running agent processes anyway
  pkill -f "chat_agent_cli.py" 2>/dev/null && echo "Stopped chat agent." || true
  pkill -f "chrome_bridge.py start" 2>/dev/null && echo "Stopped chrome bridge." || true
  exit 0
fi

PID=$(cat "$PIDFILE")
if kill -0 "$PID" 2>/dev/null; then
  echo "Stopping agent (PID $PID)..."
  # Kill the subshell and its children
  kill -- -"$PID" 2>/dev/null || kill "$PID" 2>/dev/null || true
  # Also kill any stragglers
  pkill -f "chat_agent_cli.py" 2>/dev/null || true
  pkill -f "chrome_bridge.py start" 2>/dev/null || true
  rm -f "$PIDFILE"
  echo "Agent stopped."
else
  echo "Agent not running (stale PID file). Cleaning up..."
  rm -f "$PIDFILE"
  pkill -f "chat_agent_cli.py" 2>/dev/null || true
  pkill -f "chrome_bridge.py start" 2>/dev/null || true
fi
"""


_START_PS1 = r"""#Requires -Version 5.1
param(
  [switch]$Daemon
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Load-DotEnv([string]$Path) {
  foreach ($line in Get-Content -Path $Path) {
    if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith("#")) {
      continue
    }
    $parts = $line -split "=", 2
    if ($parts.Count -eq 2) {
      [Environment]::SetEnvironmentVariable($parts[0], $parts[1], "Process")
    }
  }
}

function Write-DotEnvApiKey([string]$Path, [string]$ApiKey) {
  $existing = @()
  if (Test-Path $Path) {
    $existing = Get-Content -Path $Path | Where-Object {
      $_ -notmatch '^UNCHAINED_API_KEY=' -and $_ -notmatch '^UNCHAINED_INSTALL_TOKEN='
    }
  }
  $all = @($existing + "UNCHAINED_API_KEY=$ApiKey")
  Set-Content -Path $Path -Value $all
}

function Write-DotEnvInstallToken([string]$Path, [string]$InstallToken) {
  $existing = @()
  if (Test-Path $Path) {
    $existing = Get-Content -Path $Path | Where-Object {
      $_ -notmatch '^UNCHAINED_INSTALL_TOKEN='
    }
  }
  $all = @($existing + "UNCHAINED_INSTALL_TOKEN=$InstallToken")
  Set-Content -Path $Path -Value $all
}

function Ensure-WindowsAutostart([string]$ScriptRoot) {
  try {
    $startupDir = [Environment]::GetFolderPath("Startup")
    if ([string]::IsNullOrWhiteSpace($startupDir)) {
      return
    }
    $launcherPath = Join-Path $startupDir "Unchained Agent.cmd"
    $startScript = Join-Path $ScriptRoot "start.ps1"
    $lines = @(
      "@echo off",
      "setlocal",
      "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startScript`" -Daemon >nul 2>&1"
    )
    Set-Content -Path $launcherPath -Value $lines -Encoding ASCII
  } catch {
  }
}

if (-not (Test-Path ".env")) {
  Write-Error "ERROR: .env not found. Re-download from the web UI."
  exit 1
}
Load-DotEnv ".env"

# If no API key and no install token are configured, run browser-based claim flow.
if ([string]::IsNullOrWhiteSpace($env:UNCHAINED_API_KEY) -and [string]::IsNullOrWhiteSpace($env:UNCHAINED_INSTALL_TOKEN)) {
  $apiUrl = if ([string]::IsNullOrWhiteSpace($env:UNCHAINED_API_URL)) { "https://api.unchainedsky.com" } else { $env:UNCHAINED_API_URL }
  $claimId = [Guid]::NewGuid().ToString("N").ToLowerInvariant()
  $claimSecret = [Guid]::NewGuid().ToString("N") + [Guid]::NewGuid().ToString("N")
  $startPayload = @{ claim_id = $claimId; claim_secret = $claimSecret } | ConvertTo-Json -Compress

  try {
    Invoke-RestMethod -Method Post -Uri "$apiUrl/web/install/claim/start" -ContentType "application/json" -Body $startPayload | Out-Null
  } catch {
    Write-Error "ERROR: could not initialize installer auth claim."
    exit 1
  }

  $claimUrl = "$apiUrl/install/claim/$claimId"
  Write-Host "Authorize this installation in your browser:"
  Write-Host "  $claimUrl"
  try {
    Start-Process $claimUrl | Out-Null
  } catch {
  }
  Write-Host "Waiting for approval..."

  $installToken = ""
  for ($i = 0; $i -lt 150; $i++) {
    Start-Sleep -Seconds 2
    $pollPayload = @{ claim_id = $claimId; claim_secret = $claimSecret } | ConvertTo-Json -Compress
    $poll = $null
    try {
      $poll = Invoke-RestMethod -Method Post -Uri "$apiUrl/web/install/claim/poll" -ContentType "application/json" -Body $pollPayload
    } catch {
      continue
    }
    $status = [string]$poll.status
    if ($status -eq "approved") {
      $installToken = [string]$poll.install_token
      if (-not [string]::IsNullOrWhiteSpace($installToken)) {
        break
      }
    }
    if ($status -eq "expired") {
      Write-Error "ERROR: installer authorization expired."
      exit 1
    }
  }

  if ([string]::IsNullOrWhiteSpace($installToken)) {
    Write-Error "ERROR: timed out waiting for installer authorization."
    exit 1
  }

  Write-DotEnvInstallToken ".env" $installToken
  $env:UNCHAINED_INSTALL_TOKEN = $installToken
}

if ([string]::IsNullOrWhiteSpace($env:UNCHAINED_API_KEY) -and -not [string]::IsNullOrWhiteSpace($env:UNCHAINED_INSTALL_TOKEN)) {
  $apiUrl = if ([string]::IsNullOrWhiteSpace($env:UNCHAINED_API_URL)) { "https://api.unchainedsky.com" } else { $env:UNCHAINED_API_URL }
  Write-Host "Fetching agent credentials..."
  try {
    $payload = @{ token = $env:UNCHAINED_INSTALL_TOKEN } | ConvertTo-Json -Compress
    $resp = Invoke-RestMethod -Method Post -Uri "$apiUrl/web/install/bootstrap" -ContentType "application/json" -Body $payload
  } catch {
    Write-Error "ERROR: install token exchange failed."
    exit 1
  }
  $newKey = [string]$resp.api_key
  if ([string]::IsNullOrWhiteSpace($newKey)) {
    $err = [string]$resp.error
    if ([string]::IsNullOrWhiteSpace($err)) {
      $err = "Install token exchange failed."
    }
    Write-Error "ERROR: $err"
    exit 1
  }
  Write-DotEnvApiKey ".env" $newKey
  $env:UNCHAINED_API_KEY = $newKey
  Remove-Item Env:UNCHAINED_INSTALL_TOKEN -ErrorAction SilentlyContinue
}

function Test-PythonCommand([string]$Source, [string[]]$Prefix) {
  if ([string]::IsNullOrWhiteSpace($Source)) { return $false }
  $args = @()
  if ($Prefix) { $args += $Prefix }
  $args += @("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)")
  try {
    & $Source @args *> $null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

function Find-PythonCommand() {
  $pyCmd = Get-Command py -ErrorAction SilentlyContinue
  if ($pyCmd) {
    $pySource = [string]$pyCmd.Source
    if (Test-PythonCommand $pySource @("-3")) {
      return @{ Source = $pySource; Prefix = @("-3") }
    }
  }

  foreach ($name in @("python", "python3")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    $source = [string]$cmd.Source
    if ([string]::IsNullOrWhiteSpace($source)) { continue }
    # Skip Microsoft Store alias shim; it often prints "Python was not found..."
    if ($source -like "*WindowsApps*") { continue }
    if (Test-PythonCommand $source @()) {
      return @{ Source = $source; Prefix = @() }
    }
  }

  return $null
}

function Install-PythonRuntime() {
  # Use uv as the Python package feed: arch-aware (works on x86_64 + ARM64),
  # and avoids the python.org direct installer's ARM-incompatibility silent failure.
  # Local 'Continue' preference: uv emits warnings to stderr (e.g. shim collision)
  # that would otherwise be promoted to terminating errors by the script-wide 'Stop'.
  $oldPref = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    Write-Host "Python 3.8+ not found. Installing uv to manage Python..."
    Invoke-Expression (Invoke-RestMethod -Uri "https://astral.sh/uv/install.ps1")
    $uvBin = $null
    foreach ($cand in @(
      (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
      "$env:LOCALAPPDATA\Programs\uv\uv.exe"
    )) {
      if (Test-Path $cand) { $uvBin = $cand; break }
    }
    if (-not $uvBin) {
      $uvBin = (Get-Command uv -ErrorAction SilentlyContinue).Source
    }
    if (-not $uvBin -or -not (Test-Path $uvBin)) {
      Write-Host "ERROR: uv installer did not produce uv.exe."
      return $false
    }

    Write-Host "Installing Python 3.13 via uv..."
    # --force overwrites any stale executable shim from a prior partial install.
    & $uvBin python install 3.13 --force 2>&1 | ForEach-Object { Write-Host $_ }
    # uv exits 0 even when shim install warns; check that python is actually resolvable.
    $pythonPath = (& $uvBin python find 3.13 2>&1 | Select-Object -Last 1).ToString().Trim()
    if (-not $pythonPath -or -not (Test-Path $pythonPath)) {
      Write-Host "ERROR: 'uv python find 3.13' returned no usable path: $pythonPath"
      return $false
    }
    $pythonDir = Split-Path $pythonPath -Parent
    $env:Path = "$pythonDir;$env:Path"
    return $true
  } catch {
    Write-Host "ERROR: Install-PythonRuntime failed: $($_.Exception.Message)"
    return $false
  } finally {
    $ErrorActionPreference = $oldPref
  }
}

function Resolve-PythonCommand() {
  $found = Find-PythonCommand
  if ($null -ne $found) {
    return $found
  }

  if (-not (Install-PythonRuntime)) {
    Write-Error "ERROR: Python 3.8+ could not be installed automatically. Install it from https://www.python.org/downloads/windows/ and disable the Microsoft Store App Execution Alias for python.exe."
    exit 1
  }

  $found = Find-PythonCommand
  if ($null -ne $found) {
    return $found
  }

  Write-Error "ERROR: Python 3.8+ is required. Install from https://www.python.org/downloads/windows/ and disable the Microsoft Store App Execution Alias for python.exe."
  exit 1
}

$pythonInfo = Resolve-PythonCommand
$pythonCmd = [string]$pythonInfo.Source
$pythonPrefixArgs = @()
if ($pythonInfo.Prefix) {
  $pythonPrefixArgs += $pythonInfo.Prefix
}
$venvDir = Join-Path $PSScriptRoot ".venv"
$pythonExe = Join-Path $venvDir "Scripts\python.exe"
$requirementsFile = Join-Path $PSScriptRoot "requirements.txt"

if (-not (Test-Path $venvDir)) {
  Write-Host "Setting up Python environment..."
  & $pythonCmd @($pythonPrefixArgs + @("-m", "venv", $venvDir))
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $pythonExe)) {
    Write-Error "ERROR: failed to create Python virtual environment (.venv). Ensure Python 3.8+ from python.org is installed (not Microsoft Store alias)."
    exit 1
  }
  & $pythonExe -m pip install -q --upgrade pip
  if ($LASTEXITCODE -ne 0) {
    Write-Error "ERROR: failed to upgrade pip in .venv."
    exit 1
  }
  & $pythonExe -m pip install -q -r $requirementsFile
  if ($LASTEXITCODE -ne 0) {
    Write-Error "ERROR: failed to install Python dependencies."
    exit 1
  }
}

if (-not (Test-Path $pythonExe)) {
  Write-Error "ERROR: Missing virtualenv Python: $pythonExe"
  exit 1
}

# Conda and other older Windows Python distributions can carry an expired
# OpenSSL CA store even when Windows itself trusts the server certificate.
# Force Python networking (WebSockets, urllib, aiohttp, and httpx) to use the
# current certifi bundle installed in this isolated environment. Operators can
# supply a private CA bundle explicitly without weakening certificate checks.
& $pythonExe -c "import sys; from importlib.metadata import version; parts=tuple(int(x) for x in version(sys.argv[1]).split(sys.argv[2])[:3]); raise SystemExit(0 if parts >= (2026, 1, 4) else 1)" certifi "." 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Refreshing TLS certificate authorities..."
  & $pythonExe -m pip install -q "certifi>=2026.1.4"
  if ($LASTEXITCODE -ne 0) {
    Write-Error "ERROR: failed to install a current TLS CA bundle."
    exit 1
  }
}
$caBundle = [string]$env:UNCHAINED_CA_BUNDLE
if ([string]::IsNullOrWhiteSpace($caBundle)) {
  $caBundleOutput = @(& $pythonExe -c "import certifi; print(certifi.where())" 2>$null)
  if ($LASTEXITCODE -eq 0 -and $caBundleOutput.Count -gt 0) {
    $caBundle = ([string]$caBundleOutput[-1]).Trim()
  }
}
if ([string]::IsNullOrWhiteSpace($caBundle) -or -not (Test-Path -LiteralPath $caBundle -PathType Leaf)) {
  Write-Error "ERROR: No usable TLS CA bundle was found. Reinstall dependencies with: .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
  exit 1
}
& $pythonExe -c 'import ssl,sys; ssl.create_default_context(cafile=sys.argv[1])' $caBundle 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Error "ERROR: TLS CA bundle is not a valid PEM certificate file: $caBundle"
  exit 1
}
$env:SSL_CERT_FILE = $caBundle

function Resolve-DaemonPythonExe([string]$VenvPythonExe, [string]$BasePythonSource) {
  $venvDir = Split-Path -Parent $VenvPythonExe
  $venvPythonw = Join-Path $venvDir "pythonw.exe"
  if (Test-Path $venvPythonw) {
    return $venvPythonw
  }

  if (-not [string]::IsNullOrWhiteSpace($BasePythonSource)) {
    $baseDir = Split-Path -Parent $BasePythonSource
    $basePythonw = Join-Path $baseDir "pythonw.exe"
    if (Test-Path $basePythonw) {
      try {
        Copy-Item -Path $basePythonw -Destination $venvPythonw -Force
      } catch {
      }
      if (Test-Path $venvPythonw) {
        return $venvPythonw
      }
    }
  }

  return $VenvPythonExe
}

$daemonPythonExe = Resolve-DaemonPythonExe $pythonExe $pythonCmd
if ($daemonPythonExe -eq $pythonExe) {
  Write-Host "Warning: pythonw.exe unavailable; daemon processes may still appear as python.exe."
}

if ($Daemon) {
  if ($env:UNCHAINED_DISABLE_AUTOSTART -ne "1") {
    Ensure-WindowsAutostart $PSScriptRoot
  }

  function Test-UnchainedProcess([int]$ProcessId, [string[]]$Needles) {
    if ($ProcessId -le 0) { return $false }
    try {
      Get-Process -Id $ProcessId -ErrorAction Stop | Out-Null
    } catch {
      return $false
    }

    $cmd = ""
    try {
      $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
      $cmd = [string]$proc.CommandLine
    } catch {
      $cmd = ""
    }
    if ([string]::IsNullOrWhiteSpace($cmd)) {
      return $false
    }

    foreach ($needle in $Needles) {
      if ([string]::IsNullOrWhiteSpace($needle)) { continue }
      if ($cmd -notlike "*$needle*") {
        return $false
      }
    }
    return $true
  }

  $pidPath = Join-Path (Get-Location) ".agent.pid.json"
  if (Test-Path $pidPath) {
    $pidState = $null
    try {
      $pidState = Get-Content $pidPath -Raw | ConvertFrom-Json
    } catch {
      $pidState = $null
    }

    $agentPid = 0
    $bridgePid = 0
    if ($pidState) {
      try { $agentPid = [int]$pidState.agent_pid } catch {}
      try { $bridgePid = [int]$pidState.bridge_pid } catch {}
    }

    $agentAlive = Test-UnchainedProcess $agentPid @("chat_agent_cli.py")
    $bridgeAlive = Test-UnchainedProcess $bridgePid @("chrome_bridge.py", "start")
    if ($agentAlive -or $bridgeAlive) {
      Write-Host "Agent is already running."
      Write-Host "  Stop:  .\stop.ps1"
      exit 0
    }

    # Stale or invalid pid file from a previous crash/startup.
    Remove-Item $pidPath -Force -ErrorAction SilentlyContinue
  }
  $bridgeLog = Join-Path (Get-Location) "bridge.log"
  $bridgeErrLog = Join-Path (Get-Location) "bridge.err.log"
  $agentLog = Join-Path (Get-Location) "agent.log"
  $agentErrLog = Join-Path (Get-Location) "agent.err.log"

  Write-Host "Starting in daemon mode..."
  $bridgeProc = Start-Process -FilePath $daemonPythonExe `
    -ArgumentList @("unchained/chrome_bridge.py", "start", "--relay", "wss://$($env:UNCHAINED_RELAY_HOST)/tunnel") `
    -RedirectStandardOutput $bridgeLog -RedirectStandardError $bridgeErrLog -PassThru -WindowStyle Hidden
  Start-Sleep -Seconds 2
  $agentProc = Start-Process -FilePath $daemonPythonExe `
    -ArgumentList @("unchained/chat_agent_cli.py") `
    -RedirectStandardOutput $agentLog -RedirectStandardError $agentErrLog -PassThru -WindowStyle Hidden
  @{ bridge_pid = $bridgeProc.Id; agent_pid = $agentProc.Id } | ConvertTo-Json | Set-Content $pidPath
  Write-Host "Agent started."
  Write-Host "  Logs:  Get-Content -Path .\agent.log -Wait"
  Write-Host "  Errors: Get-Content -Path .\agent.err.log -Wait"
  Write-Host "  Autostart: enabled at Windows login"
  Write-Host "  Stop:  .\stop.ps1"
  exit 0
}

Write-Host "Starting Chrome bridge..."
$bridgeProc = Start-Process -FilePath $pythonExe `
  -ArgumentList @("unchained/chrome_bridge.py", "start", "--relay", "wss://$($env:UNCHAINED_RELAY_HOST)/tunnel") `
  -PassThru -NoNewWindow

Start-Sleep -Seconds 2
Write-Host "Starting chat agent..."
try {
  $env:PYTHONUNBUFFERED = "1"
  & $pythonExe "unchained/chat_agent_cli.py"
} finally {
  if ($bridgeProc -and -not $bridgeProc.HasExited) {
    Stop-Process -Id $bridgeProc.Id -Force -ErrorAction SilentlyContinue
  }
}
"""


_STOP_PS1 = r"""#Requires -Version 5.1
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

function Get-ProcessCommandLine([int]$ProcessId) {
  if ($ProcessId -le 0) { return "" }
  try {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
    return [string]$proc.CommandLine
  } catch {
    return ""
  }
}

function Remove-WindowsAutostart() {
  try {
    $startupDir = [Environment]::GetFolderPath("Startup")
    if ([string]::IsNullOrWhiteSpace($startupDir)) {
      return
    }
    $launcherPath = Join-Path $startupDir "Unchained Agent.cmd"
    if (Test-Path $launcherPath) {
      Remove-Item -Path $launcherPath -Force -ErrorAction SilentlyContinue
      Write-Host "Removed autostart launcher."
    }
  } catch {
  }
}

Remove-WindowsAutostart

$pidPath = Join-Path (Get-Location) ".agent.pid.json"
if (-not (Test-Path $pidPath)) {
  Write-Host "No agent PID file found. Is the agent running in daemon mode?"
  exit 0
}

try {
  $p = Get-Content $pidPath -Raw | ConvertFrom-Json
} catch {
  $p = $null
}

if ($p) {
  foreach ($field in @("agent_pid", "bridge_pid")) {
    $procId = [int]($p.$field)
    if ($procId -gt 0) {
      $cmd = Get-ProcessCommandLine $procId
      $expected = if ($field -eq "bridge_pid") { "chrome_bridge.py" } else { "chat_agent_cli.py" }
      if ([string]::IsNullOrWhiteSpace($cmd) -or $cmd -notlike "*$expected*") {
        Write-Host "Skipping $field ($procId): PID does not match expected Unchained process."
        continue
      }
      try {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped $field ($procId)."
      } catch {
      }
    }
  }
}

Remove-Item $pidPath -Force -ErrorAction SilentlyContinue
Write-Host "Agent stopped."
"""


_START_BAT = r"""@echo off
setlocal
if "%~1"=="" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Daemon
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
)
set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" (
  echo.
  echo Agent start failed. Review the error above, then press any key to close.
  pause >nul
)
exit /b %EC%
"""


_STOP_BAT = r"""@echo off
powershell -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
"""


_UPDATE_BAT = r"""@echo off
powershell -ExecutionPolicy Bypass -File "%~dp0update.ps1" %*
"""


_UPDATE_SH = r"""#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

# Load config
if [ ! -f .env ]; then
  echo "ERROR: .env not found. Re-download from the web UI."; exit 1
fi
set -a; source .env; set +a

API_URL="${UNCHAINED_API_URL:-https://api.unchainedsky.com}"
LOCAL_VERSION="unknown"
if [ -f version.txt ]; then
  LOCAL_VERSION=$(cat version.txt)
fi

echo "Current version: $LOCAL_VERSION"
echo "Checking for updates..."

# Check remote version
REMOTE=$(curl -sf --connect-timeout 10 --max-time 15 -H "Authorization: Bearer $UNCHAINED_API_KEY" "$API_URL/web/agent/version" 2>/dev/null || true)
if [ -z "$REMOTE" ]; then
  echo "ERROR: Cannot reach update server. Check your internet connection."; exit 1
fi
REMOTE_VERSION=$(echo "$REMOTE" | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])" 2>/dev/null || echo "")
if [ -z "$REMOTE_VERSION" ]; then
  echo "ERROR: Invalid server response."; exit 1
fi

if [ "$LOCAL_VERSION" = "$REMOTE_VERSION" ]; then
  echo "Already up to date ($LOCAL_VERSION)."
  exit 0
fi

echo "Update available: $LOCAL_VERSION -> $REMOTE_VERSION"
echo "Downloading update..."

# Download update ZIP
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT
curl -sf --connect-timeout 10 --max-time 60 -H "Authorization: Bearer $UNCHAINED_API_KEY" "$API_URL/web/agent/files" -o "$TMPDIR/update.zip"
if [ ! -s "$TMPDIR/update.zip" ]; then
  echo "ERROR: Download failed."; exit 1
fi

# Save current requirements for diff
OLD_REQS=""
if [ -f requirements.txt ]; then
  OLD_REQS=$(cat requirements.txt)
fi

# Backup current version before overwriting (for crash rollback)
AGENT_DIR="$(pwd)"
mkdir -p "$AGENT_DIR/unchained/.backup"
cp -f "$AGENT_DIR"/unchained/*.py "$AGENT_DIR/unchained/.backup/" 2>/dev/null || true
if [ -f "$AGENT_DIR/version.txt" ]; then
  cp -f "$AGENT_DIR/version.txt" "$AGENT_DIR/unchained/.backup/" 2>/dev/null || true
fi
echo "Backed up current version ($LOCAL_VERSION) for rollback."

# Extract — update code files only, never touch .env or .venv
mkdir -p "$AGENT_DIR/unchained"
cd "$TMPDIR" && unzip -qo update.zip
cp -f unchained-agent/unchained/*.py "$AGENT_DIR/unchained/" 2>/dev/null || true
cp -f unchained-agent/CLAUDE.md "$AGENT_DIR/" 2>/dev/null || true
cp -f unchained-agent/version.txt "$AGENT_DIR/" 2>/dev/null || true
cp -f unchained-agent/requirements.txt "$AGENT_DIR/" 2>/dev/null || true
cp -f unchained-agent/start.sh "$AGENT_DIR/" 2>/dev/null || true
cp -f unchained-agent/update.sh "$AGENT_DIR/" 2>/dev/null || true
cp -f unchained-agent/stop.sh "$AGENT_DIR/" 2>/dev/null || true
chmod +x "$AGENT_DIR/start.sh" "$AGENT_DIR/update.sh" "$AGENT_DIR/stop.sh" 2>/dev/null || true
# Copy scheduled_jobs.json only if it doesn't exist (don't overwrite user edits)
if [ ! -f "$AGENT_DIR/scheduled_jobs.json" ]; then
  cp -f unchained-agent/scheduled_jobs.json "$AGENT_DIR/" 2>/dev/null || true
fi

# Re-install deps if requirements changed
cd "$AGENT_DIR"
NEW_REQS=""
if [ -f requirements.txt ]; then
  NEW_REQS=$(cat requirements.txt)
fi
if [ "$OLD_REQS" != "$NEW_REQS" ] && [ -d .venv ]; then
  echo "Dependencies changed — reinstalling..."
  .venv/bin/pip install -q -r requirements.txt
fi

echo ""
echo "Updated to $REMOTE_VERSION. Restart required: ./start.sh"
"""


_UPDATE_PS1 = r"""#Requires -Version 5.1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Load-DotEnv([string]$Path) {
  foreach ($line in Get-Content -Path $Path) {
    if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith("#")) {
      continue
    }
    $parts = $line -split "=", 2
    if ($parts.Count -eq 2) {
      [Environment]::SetEnvironmentVariable($parts[0], $parts[1], "Process")
    }
  }
}

if (-not (Test-Path ".env")) {
  Write-Error "ERROR: .env not found. Re-download from the web UI."
  exit 1
}
Load-DotEnv ".env"

if ([string]::IsNullOrWhiteSpace($env:UNCHAINED_API_KEY)) {
  Write-Error "ERROR: UNCHAINED_API_KEY missing in .env."
  exit 1
}

$apiUrl = if ([string]::IsNullOrWhiteSpace($env:UNCHAINED_API_URL)) { "https://api.unchainedsky.com" } else { $env:UNCHAINED_API_URL }
$localVersion = "unknown"
if (Test-Path "version.txt") {
  $localVersion = (Get-Content "version.txt" -Raw).Trim()
}

Write-Host "Current version: $localVersion"
Write-Host "Checking for updates..."

$headers = @{ Authorization = "Bearer $($env:UNCHAINED_API_KEY)" }
try {
  $versionResp = Invoke-RestMethod -Method Get -Uri "$apiUrl/web/agent/version" -Headers $headers
} catch {
  Write-Error "ERROR: Cannot reach update server. Check your internet connection."
  exit 1
}

$remoteVersion = [string]$versionResp.version
if ([string]::IsNullOrWhiteSpace($remoteVersion)) {
  Write-Error "ERROR: Invalid server response."
  exit 1
}

if ($localVersion -eq $remoteVersion) {
  Write-Host "Already up to date ($localVersion)."
  exit 0
}

Write-Host "Update available: $localVersion -> $remoteVersion"
Write-Host "Downloading update..."

$tmpDir = Join-Path $env:TEMP ("unchained-update-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmpDir | Out-Null
$zipPath = Join-Path $tmpDir "update.zip"

$oldReqs = ""
if (Test-Path "requirements.txt") {
  $oldReqs = Get-Content "requirements.txt" -Raw
}

try {
  Invoke-WebRequest -UseBasicParsing -Uri "$apiUrl/web/agent/files" -Headers $headers -OutFile $zipPath
  Expand-Archive -Path $zipPath -DestinationPath $tmpDir -Force
  $srcRoot = Join-Path $tmpDir "unchained-agent"

  New-Item -ItemType Directory -Path ".\unchained" -Force | Out-Null
  Copy-Item -Path (Join-Path $srcRoot "unchained\*.py") -Destination ".\unchained" -Force -ErrorAction SilentlyContinue
  foreach ($name in @("CLAUDE.md", "version.txt", "requirements.txt", "start.ps1", "start.bat", "update.sh", "update.ps1", "update.bat", "stop.sh", "stop.ps1")) {
    $src = Join-Path $srcRoot $name
    if (Test-Path $src) {
      Copy-Item -Path $src -Destination ".\" -Force
    }
  }

  if (-not (Test-Path ".\scheduled_jobs.json")) {
    $jobs = Join-Path $srcRoot "scheduled_jobs.json"
    if (Test-Path $jobs) {
      Copy-Item -Path $jobs -Destination ".\" -Force
    }
  }
} finally {
  Remove-Item -Path $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
}

$newReqs = ""
if (Test-Path "requirements.txt") {
  $newReqs = Get-Content "requirements.txt" -Raw
}
if ($oldReqs -ne $newReqs -and (Test-Path ".\.venv\Scripts\python.exe")) {
  Write-Host "Dependencies changed -- reinstalling..."
  & ".\.venv\Scripts\python.exe" -m pip install -q -r requirements.txt
  if ($LASTEXITCODE -ne 0) {
    Write-Error "ERROR: failed to install updated dependencies."
    exit 1
  }
}

Write-Host ""
Write-Host "Updated to $remoteVersion. Restart required: .\start.ps1"
"""


def _generate_public_install_script(base_url: str) -> str:
    """Generate a public curl|bash install script with browser-based claim flow.

    No secrets are embedded — the script generates a claim_id/secret at runtime,
    opens the browser for sign-in, polls for approval, then downloads + bootstraps.
    """
    return f"""#!/bin/bash
set -euo pipefail

echo "=== Unchained Agent Installer ==="
echo ""

BASE_URL="{base_url}"
INSTALL_DIR="$HOME/unchained-agent"

# ── Prerequisites ────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 is required. Install Python 3.8+."; exit 1
fi
if ! command -v curl &>/dev/null; then
  echo "ERROR: curl is required."; exit 1
fi
if ! command -v unzip &>/dev/null; then
  echo "ERROR: unzip is required."; exit 1
fi

# ── Claim flow (browser sign-in) ────────────────────────────────────
CLAIM_ID=$(python3 -c "import secrets; print(secrets.token_hex(16))")
CLAIM_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")

START_PAYLOAD=$(CLAIM_ID="$CLAIM_ID" CLAIM_SECRET="$CLAIM_SECRET" python3 - <<'PY'
import json, os
print(json.dumps({{"claim_id": os.environ["CLAIM_ID"], "claim_secret": os.environ["CLAIM_SECRET"]}}))
PY
)
curl -sf \\
  -H "Content-Type: application/json" \\
  -d "$START_PAYLOAD" \\
  "$BASE_URL/web/install/claim/start" >/dev/null || {{
    echo "ERROR: could not initialize installer auth claim."; exit 1;
  }}

CLAIM_URL="$BASE_URL/install/claim/$CLAIM_ID"
echo "Sign in to authorize this installation:"
echo "  $CLAIM_URL"
echo ""
if command -v open >/dev/null 2>&1; then
  open "$CLAIM_URL" >/dev/null 2>&1 || true
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$CLAIM_URL" >/dev/null 2>&1 || true
fi
echo "Waiting for approval (5 min timeout)..."

INSTALL_TOKEN=""
for _ in $(seq 1 150); do
  POLL_PAYLOAD=$(CLAIM_ID="$CLAIM_ID" CLAIM_SECRET="$CLAIM_SECRET" python3 - <<'PY'
import json, os
print(json.dumps({{"claim_id": os.environ["CLAIM_ID"], "claim_secret": os.environ["CLAIM_SECRET"]}}))
PY
)
  POLL_RESP=$(curl -sf \\
    -H "Content-Type: application/json" \\
    -d "$POLL_PAYLOAD" \\
    "$BASE_URL/web/install/claim/poll" 2>/dev/null || true)
  STATUS=$(printf '%s' "$POLL_RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' 2>/dev/null || true)
  if [ "$STATUS" = "approved" ]; then
    INSTALL_TOKEN=$(printf '%s' "$POLL_RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("install_token",""))' 2>/dev/null || true)
    break
  fi
  if [ "$STATUS" = "expired" ]; then
    break
  fi
  sleep 2
done

if [ -z "$INSTALL_TOKEN" ]; then
  echo "ERROR: approval timed out. Run the installer again and approve in your browser."
  exit 1
fi
echo "Approved!"
echo ""

# ── Download agent ZIP (before bootstrap — bootstrap consumes the token) ──
echo "Downloading agent package..."
TMPFILE=$(mktemp)
trap "rm -f $TMPFILE" EXIT
HTTP_CODE=$(curl -sf -w '%{{http_code}}' \\
  -H "X-Install-Token: $INSTALL_TOKEN" \\
  "$BASE_URL/web/download-agent" -o "$TMPFILE")
if [ "$HTTP_CODE" != "200" ] || [ ! -s "$TMPFILE" ]; then
  echo "ERROR: Failed to download agent package (HTTP $HTTP_CODE)."; exit 1
fi

# ── Bootstrap (exchange install token for API key — consumes token) ───
echo "Activating credentials..."
PAYLOAD=$(TOKEN="$INSTALL_TOKEN" python3 - <<'PY'
import json, os
print(json.dumps({{"token": os.environ["TOKEN"]}}))
PY
)
BOOTSTRAP=$(curl -sf \\
  -H "Content-Type: application/json" \\
  -d "$PAYLOAD" \\
  "$BASE_URL/web/install/bootstrap") || {{
    echo "ERROR: credential exchange failed."; exit 1;
  }}
API_KEY=$(printf '%s' "$BOOTSTRAP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("api_key",""))' 2>/dev/null || true)
if [ -z "$API_KEY" ]; then
  echo "ERROR: invalid credential response."; exit 1
fi

# ── Extract + setup ──────────────────────────────────────────────────
if [ -d "$INSTALL_DIR" ]; then
  echo "Existing installation found — backing up .env..."
  cp "$INSTALL_DIR/.env" "$INSTALL_DIR/.env.bak" 2>/dev/null || true
fi
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
unzip -qo "$TMPFILE"
if [ -d "unchained-agent" ]; then
  cp -rf unchained-agent/* . 2>/dev/null || true
  cp -f unchained-agent/.env . 2>/dev/null || true
  rm -rf unchained-agent
fi

# Write .env with the real API key (start.sh will skip claim flow)
cat > .env << ENVEOF
UNCHAINED_API_KEY=$API_KEY
UNCHAINED_SERVER=wss://api.unchainedsky.com/chat/ws
UNCHAINED_RELAY_HOST=api.unchainedsky.com
UNCHAINED_RELAY_PORT=443
UNCHAINED_API_URL=https://api.unchainedsky.com
CODEX_MAX_RUNTIME_S=300
ENVEOF

echo "[1/3] Creating Python environment..."
python3 -m venv .venv
echo "[2/3] Upgrading pip..."
.venv/bin/pip install -q --upgrade pip
echo "[3/3] Installing dependencies..."
.venv/bin/pip install -q -r requirements.txt

chmod +x start.sh
chmod +x update.sh 2>/dev/null || true

AGENT_ID=$(python3 -c "import hashlib; print('claude-' + hashlib.sha256('$API_KEY'.encode()).hexdigest()[:8])")

echo ""
echo "=== Installation complete ==="
echo ""
echo "  Location:  $INSTALL_DIR"
echo "  Agent ID:  $AGENT_ID"
echo "  API key:   $API_KEY"
echo ""
echo "  Add to Claude Code (copy-paste this):"
echo ""
echo "    claude mcp add unchainedsky \\\\"
echo "      https://api.unchainedsky.com/mcp \\\\"
echo "      -t http \\\\"
echo "      -H \\"Authorization: Bearer $API_KEY\\""
echo ""
echo "  Then restart Claude Code for tools to take effect."
echo ""
echo "To start the agent:"
echo "  cd $INSTALL_DIR"
echo "  ./start.sh                  # background daemon"
echo "  ./start.sh --enable-autostart   # background daemon + reboot autostart"
echo "  ./start.sh --foreground         # foreground mode (see output, Ctrl+C to stop)"
echo ""
if [ -t 0 ]; then
  read -p "Start now? [D]aemon (default) / [f]oreground / [n]o: " -n 1 -r
  echo ""
elif [ -e /dev/tty ]; then
  read -p "Start now? [D]aemon (default) / [f]oreground / [n]o: " -n 1 -r </dev/tty || REPLY=n
  echo ""
else
  echo "Non-interactive — run ./start.sh manually."
  REPLY=n
fi
if [[ $REPLY =~ ^[Nn]$ ]]; then
  true
elif [[ $REPLY =~ ^[Ff]$ ]]; then
  cd "$INSTALL_DIR" && ./start.sh --foreground
else
  cd "$INSTALL_DIR" && ./start.sh --enable-autostart
fi
"""


def _generate_install_script(install_token: str, relay_host: str, base_url: str) -> str:
    """Generate a curl|bash install script without embedding the long-lived API key."""
    return f"""#!/bin/bash
set -euo pipefail

echo "=== Unchained Agent Installer ==="
echo ""

INSTALL_DIR="$HOME/unchained-agent"
INSTALL_TOKEN="{install_token}"
BASE_URL="{base_url}"

# Check prerequisites
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 is required. Install Python 3.8+."; exit 1
fi
if ! command -v curl &>/dev/null; then
  echo "ERROR: curl is required."; exit 1
fi

# Create install directory
if [ -d "$INSTALL_DIR" ]; then
  echo "Existing installation found at $INSTALL_DIR"
  echo "Backing up .env..."
  cp "$INSTALL_DIR/.env" "$INSTALL_DIR/.env.bak" 2>/dev/null || true
fi
mkdir -p "$INSTALL_DIR"

# Write .env
cat > "$INSTALL_DIR/.env" << 'ENVEOF'
UNCHAINED_API_KEY=
UNCHAINED_INSTALL_TOKEN={install_token}
UNCHAINED_SERVER=wss://{relay_host}/chat/ws
UNCHAINED_RELAY_HOST={relay_host}
UNCHAINED_RELAY_PORT=443
UNCHAINED_API_URL=https://{relay_host}
CODEX_MAX_RUNTIME_S=300
ENVEOF

echo "Install token configured."

# Download agent package
echo "Downloading agent package..."
TMPFILE=$(mktemp)
trap "rm -f $TMPFILE" EXIT
HTTP_CODE=$(curl -sf -w '%{{http_code}}' \\
  -H "X-Install-Token: $INSTALL_TOKEN" \\
  "$BASE_URL/web/download-agent" -o "$TMPFILE")
if [ "$HTTP_CODE" != "200" ] || [ ! -s "$TMPFILE" ]; then
  echo "ERROR: Failed to download agent package (HTTP $HTTP_CODE)."; exit 1
fi

# Extract
cd "$INSTALL_DIR"
unzip -qo "$TMPFILE"
# Move contents from nested dir if present
if [ -d "unchained-agent" ]; then
  cp -rf unchained-agent/* . 2>/dev/null || true
  cp -f unchained-agent/.env . 2>/dev/null || true
  rm -rf unchained-agent
fi
# Re-write .env (the ZIP has its own, but we want the install token we just minted)
cat > .env << 'ENVEOF2'
UNCHAINED_API_KEY=
UNCHAINED_INSTALL_TOKEN={install_token}
UNCHAINED_SERVER=wss://{relay_host}/chat/ws
UNCHAINED_RELAY_HOST={relay_host}
UNCHAINED_RELAY_PORT=443
UNCHAINED_API_URL=https://{relay_host}
CODEX_MAX_RUNTIME_S=300
ENVEOF2

# Setup venv
echo "[1/3] Creating Python environment..."
python3 -m venv .venv
echo "[2/3] Upgrading pip..."
.venv/bin/pip install -q --upgrade pip
echo "[3/3] Installing dependencies..."
.venv/bin/pip install -q -r requirements.txt

# Make scripts executable
chmod +x start.sh
chmod +x update.sh 2>/dev/null || true

echo ""
echo "=== Installation complete ==="
echo "Location: $INSTALL_DIR"
echo ""
echo "To start the agent:"
echo "  ./start.sh                      # background daemon (one-shot)"
echo "  ./start.sh --enable-autostart   # background daemon + autostart on reboot"
echo "  ./start.sh --disable-autostart  # remove reboot autostart"
echo "  ./start.sh --foreground         # foreground mode (see output, Ctrl+C to stop)"
echo "  ./stop.sh                       # stop daemon"
echo ""
if [ -t 0 ]; then
  read -p "Start now? [D]aemon (default) / [f]oreground / [n]o: " -n 1 -r
  echo ""
elif [ -e /dev/tty ]; then
  read -p "Start now? [D]aemon (default) / [f]oreground / [n]o: " -n 1 -r </dev/tty || REPLY=n
  echo ""
else
  echo "Non-interactive — run ./start.sh manually."
  REPLY=n
fi
if [[ $REPLY =~ ^[Nn]$ ]]; then
  true
elif [[ $REPLY =~ ^[Ff]$ ]]; then
  cd "$INSTALL_DIR" && ./start.sh --foreground
else
  cd "$INSTALL_DIR" && ./start.sh --enable-autostart
fi
"""


_WINDOWS_INSTALLER_TEMPLATE = r"""#Requires -Version 5.1
$ErrorActionPreference = "Stop"

Write-Host "=== Unchained Agent Installer (Windows) ==="
Write-Host ""

$installDir = Join-Path $HOME "unchained-agent"
$installToken = "__INSTALL_TOKEN__"
$baseUrl = "__BASE_URL__"
$relayHost = "__RELAY_HOST__"

function Test-PythonCommand([string]$Source, [string[]]$Prefix) {
  if ([string]::IsNullOrWhiteSpace($Source)) { return $false }
  $args = @()
  if ($Prefix) { $args += $Prefix }
  $args += @("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)")
  try {
    & $Source @args *> $null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

function Find-PythonCommand() {
  $pyCmd = Get-Command py -ErrorAction SilentlyContinue
  if ($pyCmd) {
    $pySource = [string]$pyCmd.Source
    if (Test-PythonCommand $pySource @("-3")) {
      return @{ Source = $pySource; Prefix = @("-3") }
    }
  }

  foreach ($name in @("python", "python3")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    $source = [string]$cmd.Source
    if ([string]::IsNullOrWhiteSpace($source)) { continue }
    # Skip Microsoft Store alias shim; it often prints "Python was not found..."
    if ($source -like "*WindowsApps*") { continue }
    if (Test-PythonCommand $source @()) {
      return @{ Source = $source; Prefix = @() }
    }
  }

  return $null
}

function Install-PythonRuntime() {
  # Use uv as the Python package feed: arch-aware (works on x86_64 + ARM64),
  # and avoids the python.org direct installer's ARM-incompatibility silent failure.
  # Local 'Continue' preference: uv emits warnings to stderr (e.g. shim collision)
  # that would otherwise be promoted to terminating errors by the script-wide 'Stop'.
  $oldPref = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    Write-Host "Python 3.8+ not found. Installing uv to manage Python..."
    Invoke-Expression (Invoke-RestMethod -Uri "https://astral.sh/uv/install.ps1")
    $uvBin = $null
    foreach ($cand in @(
      (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
      "$env:LOCALAPPDATA\Programs\uv\uv.exe"
    )) {
      if (Test-Path $cand) { $uvBin = $cand; break }
    }
    if (-not $uvBin) {
      $uvBin = (Get-Command uv -ErrorAction SilentlyContinue).Source
    }
    if (-not $uvBin -or -not (Test-Path $uvBin)) {
      Write-Host "ERROR: uv installer did not produce uv.exe."
      return $false
    }

    Write-Host "Installing Python 3.13 via uv..."
    # --force overwrites any stale executable shim from a prior partial install.
    & $uvBin python install 3.13 --force 2>&1 | ForEach-Object { Write-Host $_ }
    # uv exits 0 even when shim install warns; check that python is actually resolvable.
    $pythonPath = (& $uvBin python find 3.13 2>&1 | Select-Object -Last 1).ToString().Trim()
    if (-not $pythonPath -or -not (Test-Path $pythonPath)) {
      Write-Host "ERROR: 'uv python find 3.13' returned no usable path: $pythonPath"
      return $false
    }
    $pythonDir = Split-Path $pythonPath -Parent
    $env:Path = "$pythonDir;$env:Path"
    return $true
  } catch {
    Write-Host "ERROR: Install-PythonRuntime failed: $($_.Exception.Message)"
    return $false
  } finally {
    $ErrorActionPreference = $oldPref
  }
}

function Resolve-PythonCommand() {
  $found = Find-PythonCommand
  if ($null -ne $found) {
    return $found
  }

  if (-not (Install-PythonRuntime)) {
    Write-Error "ERROR: Python 3.8+ could not be installed automatically. Install it from https://www.python.org/downloads/windows/ and disable the Microsoft Store App Execution Alias for python.exe."
    exit 1
  }

  $found = Find-PythonCommand
  if ($null -ne $found) {
    return $found
  }

  Write-Error "ERROR: Python 3.8+ is required. Install from https://www.python.org/downloads/windows/ and disable the Microsoft Store App Execution Alias for python.exe."
  exit 1
}

$pythonInfo = Resolve-PythonCommand
$pythonCmd = [string]$pythonInfo.Source
$pythonPrefixArgs = @()
if ($pythonInfo.Prefix) {
  $pythonPrefixArgs += $pythonInfo.Prefix
}
$venvDir = Join-Path $installDir ".venv"
$pythonExe = Join-Path $venvDir "Scripts\python.exe"
$requirementsFile = Join-Path $installDir "requirements.txt"

if (Test-Path $installDir) {
  Write-Host "Existing installation found at $installDir"
  if (Test-Path (Join-Path $installDir ".env")) {
    Copy-Item (Join-Path $installDir ".env") (Join-Path $installDir ".env.bak") -Force -ErrorAction SilentlyContinue
  }
}
New-Item -ItemType Directory -Path $installDir -Force | Out-Null

$envBody = @(
  "UNCHAINED_API_KEY="
  "UNCHAINED_INSTALL_TOKEN=$installToken"
  "UNCHAINED_SERVER=wss://$relayHost/chat/ws"
  "UNCHAINED_RELAY_HOST=$relayHost"
  "UNCHAINED_RELAY_PORT=443"
  "UNCHAINED_API_URL=https://$relayHost"
  "CODEX_MAX_RUNTIME_S=300"
)
Set-Content -Path (Join-Path $installDir ".env") -Value $envBody

Write-Host "Install token configured."
Write-Host "Downloading agent package..."

$tmpZip = Join-Path $env:TEMP ("unchained-agent-" + [guid]::NewGuid().ToString("N") + ".zip")
try {
  $headers = @{ "X-Install-Token" = $installToken }
  Invoke-WebRequest -UseBasicParsing -Headers $headers -Uri "$baseUrl/web/download-agent" -OutFile $tmpZip
  if (-not (Test-Path $tmpZip) -or ((Get-Item $tmpZip).Length -eq 0)) {
    Write-Error "ERROR: Failed to download agent package."
    exit 1
  }

  Expand-Archive -Path $tmpZip -DestinationPath $installDir -Force
  $nested = Join-Path $installDir "unchained-agent"
  if (Test-Path $nested) {
    Copy-Item -Path (Join-Path $nested "*") -Destination $installDir -Recurse -Force
    if (Test-Path (Join-Path $nested ".env")) {
      Copy-Item (Join-Path $nested ".env") (Join-Path $installDir ".env") -Force
    }
    Remove-Item $nested -Recurse -Force
  }
} finally {
  Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue
}

# Re-write .env to ensure installer token is retained.
Set-Content -Path (Join-Path $installDir ".env") -Value $envBody

Push-Location $installDir
try {
  Write-Host "[1/3] Creating Python environment..."
  & $pythonCmd @($pythonPrefixArgs + @("-m", "venv", $venvDir))
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $pythonExe)) {
    Write-Error "ERROR: failed to create Python virtual environment (.venv). Ensure Python 3.8+ from python.org is installed (not Microsoft Store alias)."
    exit 1
  }
  Write-Host "[2/3] Upgrading pip..."
  & $pythonExe -m pip install -q --upgrade pip
  if ($LASTEXITCODE -ne 0) {
    Write-Error "ERROR: failed to upgrade pip in .venv."
    exit 1
  }
  Write-Host "[3/3] Installing dependencies..."
  & $pythonExe -m pip install -q -r $requirementsFile
  if ($LASTEXITCODE -ne 0) {
    Write-Error "ERROR: failed to install Python dependencies."
    exit 1
  }
} finally {
  Pop-Location
}

Write-Host ""
Write-Host "=== Installation complete ==="
Write-Host "Location: $installDir"
Write-Host ""
Write-Host "To start the agent:"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$installDir\start.ps1`""
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$installDir\start.ps1`" -Daemon"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$installDir\stop.ps1`""
Write-Host ""
Write-Host "Starting now in daemon mode (autostart on Windows login enabled)..."
powershell -NoProfile -ExecutionPolicy Bypass -File "$installDir\start.ps1" -Daemon
"""


def _generate_windows_install_script(install_token: str, relay_host: str, base_url: str) -> str:
    """Generate a PowerShell installer script for Windows."""
    script = _WINDOWS_INSTALLER_TEMPLATE
    script = script.replace("__INSTALL_TOKEN__", install_token)
    script = script.replace("__RELAY_HOST__", relay_host)
    script = script.replace("__BASE_URL__", base_url)
    return script


def generate_platform_installer_script(
    platform: str,
    install_token: str,
    relay_host: str,
    base_url: str,
) -> str:
    """Generate an OS-specific installer script."""
    normalized = (platform or "").strip().lower()
    if normalized in {"mac", "macos", "darwin", "osx"}:
        return _generate_install_script(install_token, relay_host, base_url)
    if normalized in {"windows", "win", "win32"}:
        return _generate_windows_install_script(install_token, relay_host, base_url)
    raise ValueError(f"Unsupported installer platform: {platform}")


def _patch_chat_agent_cli(source: str) -> str:
    """Patch chat_agent_cli.py for the downloaded package."""
    # Relative paths instead of hardcoded home dir
    source = source.replace(
        'sys.path.insert(0, os.path.expanduser("~/Projects/unchained/unchained"))',
        'sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))',
    )
    source = source.replace(
        'CWD = os.path.expanduser("~/unchained-agent/unchained")',
        'CWD = os.path.dirname(os.path.abspath(__file__))',
    )
    # Fix the list-form subprocess call to use sys.executable (platform-independent)
    # System prompt and Bash permission patterns keep "uv run python" as-is
    source = source.replace(
        '["uv", "run", "python", "cdp_tool.py",',
        '[sys.executable, "cdp_tool.py",',
    )
    # Set API URL for the HTTP-based cdp_tool.py
    source = source.replace(
        '    env["CDP_AGENT_ID"] = cdp_agent_id or BRIDGE_AGENT_ID or AGENT_ID\n'
        '    env["CDP_RELAY_HOST"] = RELAY_HOST\n'
        '    env["CDP_RELAY_PORT"] = str(RELAY_PORT)',
        '    env["CDP_AGENT_ID"] = cdp_agent_id or BRIDGE_AGENT_ID or AGENT_ID\n'
        '    env["UNCHAINED_API_URL"] = f"https://{RELAY_HOST}"',
    )
    return source


_README = r"""# Unchained Agent

Browser agent that connects your Chrome to the Unchained chat UI.

## Quick Start

    chmod +x start.sh
    ./start.sh

That's it. Chrome will open automatically and the agent will connect.
Go to https://api.unchainedsky.com/chat to start chatting.

## What start.sh Does

1. Creates a Python virtual environment (first run only)
2. Installs dependencies
3. Opens Chrome with remote debugging enabled
4. Connects Chrome to the Unchained relay server
5. Starts the chat agent (waits for messages from the web UI)
6. Runs as a background daemon. Run `./start.sh --enable-autostart` once to also register macOS reboot autostart.

## Requirements

- Python 3.8+ (macOS 12.3+ includes Python shims, but you may still need a real python.org/Homebrew install)
- Google Chrome
- One local model CLI, depending on your selected lane:
  - Claude Code CLI (`claude`) — install from https://docs.anthropic.com/en/docs/claude-code
  - Codex CLI (`codex`) — run `codex login`
  - OpenCode CLI (`opencode`) — run `opencode auth login`

## Troubleshooting

### "Operation not permitted" when running start.sh
macOS quarantine flag from the ZIP download. Fix:
    chmod +x start.sh

### "pip install" or dependency errors
Old pip version. The script auto-upgrades pip, but if it fails:
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt

### "Chrome bridge check timed out"
Chrome is not open with debugging. Fix:
    - Click the Chrome icon in your dock to reopen it
    - Or re-run ./start.sh (it auto-launches Chrome)

### Agent says "Chrome is not open"
Same as above — Chrome was closed. Click it in the dock or re-run start.sh.

### "invalid syntax" or Python startup errors
Python version too old. Needs Python 3.8+. Check with:
    python3 --version

### Agent connects but commands hang
The Chrome bridge may have lost connection. Stop everything (Ctrl+C) and
re-run ./start.sh for a clean restart.

## Files

    .env                        Your API key and server config (do not share)
    start.sh                    Main entry point — run this
    requirements.txt            Python dependencies
    unchained/chrome_bridge.py  Connects Chrome to the relay server
    unchained/chat_agent_cli.py Chat agent (uses Claude, Codex, or OpenCode CLI)
    unchained/cdp_tool.py       Browser tool CLI (calls server API)
    unchained/auth.py           API key validation

## Claude Code Installation Prompt

If you want another Claude Code instance to set this up, paste this prompt:

    I need you to install and run the Unchained browser agent.
    The package is already downloaded at ./unchained-agent/

    Steps:
    1. cd unchained-agent
    2. chmod +x start.sh
    3. ./start.sh

    If you hit any errors, check the README.txt troubleshooting section.
    The agent needs Chrome and Python 3.8+.
"""


def _add_source_files(zf: zipfile.ZipFile, src_dir: str, prefix: str):
    """Add non-proprietary source files + cdp_tool.py to a ZIP."""
    # Non-proprietary source files (cdp_tool.py comes from cdp_tool_packaged.py
    # via _PACKAGE_FILES mapping)
    for dest, src_name in _PACKAGE_FILES.items():
        src_path = os.path.join(src_dir, src_name)
        if not os.path.exists(src_path):
            continue
        content = open(src_path, "r").read()

        # Patch chat_agent_cli.py for package environment
        if src_name == "chat_agent_cli.py":
            content = _patch_chat_agent_cli(content)

        # Inject future annotations so packaged client files keep older-Python
        # annotation evaluation deferred without duplicating the import.
        # only when missing to avoid duplicate future-import lines.
        if dest.endswith(".py") and "from __future__ import annotations" not in content:
            if content.startswith('"""'):
                end = content.index('"""', 3) + 3
                content = content[:end] + "\nfrom __future__ import annotations\n" + content[end:]
            else:
                content = "from __future__ import annotations\n" + content

        zf.writestr(f"{prefix}/{dest}", content)


def _ps1(content: str) -> str:
    """PowerShell loads .ps1 as ANSI by default; prefix a UTF-8 BOM so any
    non-ASCII character in script bodies (em-dashes, smart quotes, etc.) is
    parsed as UTF-8 instead of breaking string terminators."""
    return content if content.startswith("﻿") else "﻿" + content


def build_agent_zip(api_key: str, relay_host: str, install_token: str = "") -> bytes:
    """Build an in-memory ZIP file with the agent package.

    Returns the ZIP as bytes, ready to be served as a download.
    """
    del api_key  # Intentionally never embed long-lived API keys in downloadable artifacts.
    src_dir = os.path.dirname(os.path.abspath(__file__))
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # .env contains only a short-lived install token; start.sh exchanges it for the real key.
        zf.writestr("unchained-agent/.env", _make_env(relay_host, install_token))

        # README with setup instructions and troubleshooting
        zf.writestr("unchained-agent/README.txt", _README)

        # version.txt
        zf.writestr("unchained-agent/version.txt", VERSION)

        # start.sh (executable)
        info = zipfile.ZipInfo("unchained-agent/start.sh")
        info.external_attr = 0o755 << 16  # rwxr-xr-x
        zf.writestr(info, _START_SH)

        # update.sh (executable)
        info = zipfile.ZipInfo("unchained-agent/update.sh")
        info.external_attr = 0o755 << 16
        zf.writestr(info, _UPDATE_SH)

        # stop.sh (executable)
        info = zipfile.ZipInfo("unchained-agent/stop.sh")
        info.external_attr = 0o755 << 16
        zf.writestr(info, _STOP_SH)

        # Windows scripts
        zf.writestr("unchained-agent/start.ps1", _ps1(_START_PS1))
        zf.writestr("unchained-agent/stop.ps1", _ps1(_STOP_PS1))
        zf.writestr("unchained-agent/update.ps1", _ps1(_UPDATE_PS1))
        zf.writestr("unchained-agent/start.bat", _START_BAT)
        zf.writestr("unchained-agent/stop.bat", _STOP_BAT)
        zf.writestr("unchained-agent/update.bat", _UPDATE_BAT)

        # requirements.txt
        zf.writestr("unchained-agent/requirements.txt", _REQUIREMENTS)

        # CLAUDE.md — agent browsing methodology and CDP rules
        claude_md_path = os.path.join(src_dir, "CLAUDE.md")
        if os.path.exists(claude_md_path):
            zf.writestr("unchained-agent/CLAUDE.md",
                         open(claude_md_path, "r").read())

        _add_source_files(zf, src_dir, "unchained-agent")

    return buf.getvalue()


def build_update_zip() -> bytes:
    """Build an update ZIP (no .env, no venv).

    Returns the ZIP as bytes, ready to be served as a download.
    """
    src_dir = os.path.dirname(os.path.abspath(__file__))
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # version.txt
        zf.writestr("unchained-agent/version.txt", VERSION)

        # requirements.txt
        zf.writestr("unchained-agent/requirements.txt", _REQUIREMENTS)

        # start scripts (executable) so launcher/path discovery fixes reach installed agents.
        info = zipfile.ZipInfo("unchained-agent/start.sh")
        info.external_attr = 0o755 << 16
        zf.writestr(info, _START_SH)
        zf.writestr("unchained-agent/start.ps1", _ps1(_START_PS1))
        zf.writestr("unchained-agent/start.bat", _START_BAT)

        # update.sh (executable)
        info = zipfile.ZipInfo("unchained-agent/update.sh")
        info.external_attr = 0o755 << 16
        zf.writestr(info, _UPDATE_SH)
        zf.writestr("unchained-agent/update.ps1", _ps1(_UPDATE_PS1))
        zf.writestr("unchained-agent/update.bat", _UPDATE_BAT)

        # stop.sh (executable) so installed agents pick up stop/autostart fixes.
        info = zipfile.ZipInfo("unchained-agent/stop.sh")
        info.external_attr = 0o755 << 16
        zf.writestr(info, _STOP_SH)
        zf.writestr("unchained-agent/stop.ps1", _ps1(_STOP_PS1))

        # CLAUDE.md
        claude_md_path = os.path.join(src_dir, "CLAUDE.md")
        if os.path.exists(claude_md_path):
            zf.writestr("unchained-agent/CLAUDE.md",
                         open(claude_md_path, "r").read())

        _add_source_files(zf, src_dir, "unchained-agent")

    return buf.getvalue()


def build_research_desk_zip() -> bytes:
    """Build an installable ZIP snapshot for the Research Desk package."""
    if not _RESEARCH_DESK_VENDOR_DIR.is_dir():
        raise FileNotFoundError(
            f"Research Desk vendor tree missing: {_RESEARCH_DESK_VENDOR_DIR}"
        )

    manifest_path = _RESEARCH_DESK_VENDOR_DIR / _RESEARCH_DESK_VENDOR_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_files = manifest.get("files", {})
    if not isinstance(manifest_files, dict) or not manifest_files:
        raise ValueError("Research Desk vendor manifest is missing file hashes")

    expected_paths = set(_RESEARCH_DESK_VENDOR_ROOT_FILES)
    package_dir = _RESEARCH_DESK_VENDOR_DIR / _RESEARCH_DESK_VENDOR_PACKAGE_DIR
    if not package_dir.is_dir():
        raise FileNotFoundError(
            f"Research Desk vendor package missing: {package_dir}"
        )
    actual_package_paths = {
        path.relative_to(_RESEARCH_DESK_VENDOR_DIR).as_posix()
        for path in sorted(package_dir.glob("*.py"))
        if path.is_file() and not path.is_symlink()
    }
    expected_paths.update(actual_package_paths)
    if set(manifest_files) != expected_paths:
        raise ValueError("Research Desk vendor manifest does not match vendored file set")

    buf = io.BytesIO()
    prefix = f"unchained-pyreplab-{RESEARCH_DESK_VERSION}"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(manifest_path, f"{prefix}/{_RESEARCH_DESK_VENDOR_MANIFEST}")
        for rel_name in sorted(expected_paths):
            path = _RESEARCH_DESK_VENDOR_DIR / rel_name
            if not path.is_file() or path.is_symlink():
                raise FileNotFoundError(f"Research Desk vendor file missing: {path}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if manifest_files.get(rel_name) != digest:
                raise ValueError(f"Research Desk vendor hash mismatch: {rel_name}")
            zf.write(path, f"{prefix}/{rel_name}")
    return buf.getvalue()
