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
import os
import zipfile

VERSION = "0.3.48"
# 0.3.46 is the first packaged client version that reliably includes the
# archive-restore safety fix on users' machines, so anything older must update.
MIN_VERSION = "0.3.46"

# Source files to include as-is (non-proprietary)
_PACKAGE_FILES = {
    # dest path in ZIP → source filename
    "unchained/chrome_bridge.py": "chrome_bridge.py",
    "unchained/chat_agent_cli.py": "chat_agent_cli.py",
    "unchained/scheduled_tasks.py": "scheduled_tasks.py",
    "unchained/scheduler_tool.py": "scheduler_tool.py",
    "scheduled_jobs.json": "scheduled_jobs.example.json",
    "unchained/auth.py": "auth.py",
    "unchained/nudge.py": "nudge.py",
}

_REQUIREMENTS = """\
websockets>=13.0
httpx
aiohttp==3.10.11
PyJWT==2.9.0
cryptography>=42.0
"""

# ---------------------------------------------------------------------------
# Packaged cdp_tool.py — thin HTTP client calling /web/cmd on the server.
# Proprietary tools (ddm, intel, cdp) run server-side only.
# ---------------------------------------------------------------------------
_PACKAGED_CDP_TOOL = r'''"""cdp_tool.py — CLI wrapper for browser tools via server API.

Calls the server's /web/cmd endpoint. DDM, page intelligence, and CDP
run on the relay server — this is a thin HTTP client.
Tab management (tabs, new-tab, close-tab) calls Chrome's local HTTP API
directly since Chrome always runs on the same machine.

Usage (called by Claude via Bash):
    python cdp_tool.py ddm --llm-2pass --cols 60
    python cdp_tool.py navigate https://example.com
    python cdp_tool.py pdf
    python cdp_tool.py tabs
    python cdp_tool.py new-tab https://example.com
    python cdp_tool.py close-tab <tab_id>
"""

import json
import os
import sys
import urllib.request
import urllib.error

API_KEY = os.environ.get("UNCHAINED_API_KEY", "")
API_URL = os.environ.get("UNCHAINED_API_URL", "https://api.unchainedsky.com")
DEFAULT_NEW_TAB_PATH = "/tab"
TAB_ID = os.environ.get("CDP_TAB_ID", "auto")
CDP_HOST = os.environ.get("CDP_HOST", "127.0.0.1")
CDP_PORT = int(os.environ.get("CDP_PORT", "9222"))


def cmd(action, **kwargs):
    body = json.dumps({"action": action, "tab_id": TAB_ID, **kwargs}).encode()
    req = urllib.request.Request(
        f"{API_URL}/web/cmd",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        try:
            err = json.loads(err).get("error", err)
        except Exception:
            pass
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, TimeoutError):
        print("Error: Cannot reach the server. Check your internet connection.", file=sys.stderr)
        sys.exit(1)


def _chrome_tabs():
    """List page tabs from local Chrome's HTTP API."""
    req = urllib.request.Request(f"http://{CDP_HOST}:{CDP_PORT}/json")
    with urllib.request.urlopen(req, timeout=5) as resp:
        tabs = json.loads(resp.read())
    return [t for t in tabs if t.get("type") == "page"]


def main():
    if len(sys.argv) < 2:
        print("Usage: cdp_tool.py <command> [--tab <id>] [args...]")
        print("Commands: ddm, navigate, click, type, js, screenshot, intel, pdf, tabs, new-tab, close-tab")
        print("Use --tab <id> to target a specific tab (default: auto = first tab)")
        sys.exit(1)

    command = sys.argv[1]
    args = sys.argv[2:]

    # Parse --tab <id> from args (works on any command)
    tab_id = TAB_ID
    filtered = []
    i = 0
    while i < len(args):
        if args[i] == "--tab" and i + 1 < len(args):
            tab_id = args[i + 1]
            i += 2
        else:
            filtered.append(args[i])
            i += 1
    args = filtered

    try:
        # --- Tab management: call Chrome's local HTTP API directly ---
        if command == "tabs":
            tabs = _chrome_tabs()
            print(f"=== Open Tabs ({len(tabs)}) ===")
            for t in tabs:
                tid = t["id"][:12]
                title = (t.get("title") or "(no title)")[:50]
                url = (t.get("url") or "")[:80]
                print(f"  {tid}  {title}  {url}")
            return

        elif command == "new-tab":
            url = args[0] if args else f"{API_URL.rstrip('/')}{DEFAULT_NEW_TAB_PATH}"
            req = urllib.request.Request(
                f"http://{CDP_HOST}:{CDP_PORT}/json/new?{url}", method="PUT")
            with urllib.request.urlopen(req, timeout=5) as resp:
                tab_info = json.loads(resp.read())
            new_id = tab_info["id"][:12]
            print(f"Created tab {new_id}")
            tabs = _chrome_tabs()
            for t in tabs:
                tid = t["id"][:12]
                title = (t.get("title") or "(no title)")[:50]
                marker = " *" if t["id"].startswith(new_id) else ""
                print(f"  {tid}  {title}{marker}")
            return

        elif command == "close-tab":
            if not args:
                print("Usage: cdp_tool.py close-tab <tab_id>", file=sys.stderr)
                sys.exit(1)
            tabs = _chrome_tabs()
            matches = [t for t in tabs if t["id"].startswith(args[0])]
            if not matches:
                print(f"Tab {args[0]} not found", file=sys.stderr)
                sys.exit(1)
            if len(matches) > 1:
                print(f"Ambiguous prefix '{args[0]}'", file=sys.stderr)
                sys.exit(1)
            close_id = matches[0]["id"]
            req = urllib.request.Request(
                f"http://{CDP_HOST}:{CDP_PORT}/json/close/{close_id}", method="PUT")
            urllib.request.urlopen(req, timeout=5)
            print(f"Closed tab {close_id[:12]}")
            return

        # --- Browser commands: go through server API ---
        if command == "ddm":
            result = cmd("ddm", tab_id=tab_id, flags=args or ["--llm-2pass", "--cols", "60"])
        elif command == "navigate":
            if not args:
                print("Usage: cdp_tool.py navigate <url>", file=sys.stderr)
                sys.exit(1)
            result = cmd("navigate", tab_id=tab_id, url=args[0])
        elif command == "click":
            if len(args) < 2:
                print("Usage: cdp_tool.py click <x> <y>", file=sys.stderr)
                sys.exit(1)
            result = cmd("click", tab_id=tab_id, x=int(args[0]), y=int(args[1]))
        elif command == "type":
            if not args:
                print("Usage: cdp_tool.py type <text>", file=sys.stderr)
                sys.exit(1)
            result = cmd("type", tab_id=tab_id, text=" ".join(args))
        elif command == "js":
            if not args:
                print("Usage: cdp_tool.py js <expression>", file=sys.stderr)
                sys.exit(1)
            result = cmd("js", tab_id=tab_id, expression=" ".join(args))
        elif command == "screenshot":
            result = cmd("screenshot", tab_id=tab_id)
        elif command == "intel":
            result = cmd("intel", tab_id=tab_id, flags=args or ["--probe"])
        elif command == "pdf":
            result = cmd("ddm", tab_id=tab_id, flags=["--pdf"] + args)
        else:
            print(f"Unknown command: {command}", file=sys.stderr)
            sys.exit(1)

        if result.get("type") == "image":
            import tempfile
            data = result.get("data", "")
            sc_path = os.path.join(tempfile.gettempdir(), "unchained_last_screenshot.b64")
            with open(sc_path, "w") as f:
                f.write(data)
            print(f"[screenshot captured: {len(data)} bytes, saved:{sc_path}]")
        else:
            print(result.get("data", ""))

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
'''


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

DAEMON=false
ENABLE_AUTOSTART=false
DISABLE_AUTOSTART=false
for arg in "$@"; do
  case "$arg" in
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
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
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
  launchctl bootstrap "gui/$(id -u)" "$AUTOSTART_PLIST"
  launchctl enable "gui/$(id -u)/$AUTOSTART_LABEL" >/dev/null 2>&1 || true
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

if $ENABLE_AUTOSTART && ! $DAEMON; then
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

# Install deps if needed
if [ ! -d .venv ]; then
  echo "Setting up Python environment..."
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

# Activate venv so Claude's Bash tool finds the right python
source .venv/bin/activate

# launchd starts with a minimal PATH; add common CLI locations, but keep
# Homebrew/system installs ahead of ~/.local/bin so stale local shims do not win.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH:$HOME/.local/bin"
if [ -z "${CLAUDE_BIN:-}" ] && command -v claude >/dev/null 2>&1; then
  export CLAUDE_BIN="$(command -v claude)"
fi
if [ -z "${CODEX_BIN:-}" ] && command -v codex >/dev/null 2>&1; then
  export CODEX_BIN="$(command -v codex)"
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

  # Check if already running
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Agent is already running (PID $(cat "$PIDFILE"))."
    echo "Stop: ./stop.sh"
    exit 0
  fi

  if $ENABLE_AUTOSTART; then
    install_autostart
  fi

  echo "Starting in daemon mode..."
  echo "Log file: $LOGFILE"

  # Launch both processes in a subshell, redirect to log
  (
    bridge_loop() {
      while true; do
        echo "[$(date)] Starting Chrome bridge..."
        # set +e prevents daemon crash when bridge exits non-zero.
        set +e
        python unchained/chrome_bridge.py start \
          --relay "wss://$UNCHAINED_RELAY_HOST/tunnel"
        BRIDGE_EXIT_CODE=$?
        set -e
        echo "[$(date)] Chrome bridge exited (code $BRIDGE_EXIT_CODE). Restarting in 5s..."
        sleep 5
      done
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

    echo "[$(date)] Starting chat agent..."
    # caffeinate -i prevents macOS App Nap from suspending the agent
    AGENT_CMD=(env PYTHONUNBUFFERED=1 python unchained/chat_agent_cli.py)
    if command -v caffeinate &>/dev/null; then
      AGENT_CMD=(caffeinate -i -- "${AGENT_CMD[@]}")
    fi
    while true; do
      # set +e prevents daemon crash when agent exits non-zero.
      set +e
      "${AGENT_CMD[@]}"
      EXIT_CODE=$?
      set -e
      echo "[$(date)] Agent exited (code $EXIT_CODE). Restarting in 5s..."
      sleep 5
    done
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
  $pythonVersion = "3.13.2"
  $installerName = "python-$pythonVersion-amd64.exe"
  $installerUrl = "https://www.python.org/ftp/python/$pythonVersion/$installerName"
  $tmpInstaller = Join-Path $env:TEMP ("unchained-" + $installerName)

  Write-Host "Python 3.8+ not found. Installing Python runtime..."
  try {
    Invoke-WebRequest -UseBasicParsing -Uri $installerUrl -OutFile $tmpInstaller
    $installArgs = @(
      "/quiet",
      "InstallAllUsers=0",
      "Include_test=0",
      "Include_doc=0",
      "Include_dev=0",
      "Include_pip=1",
      "Include_launcher=1",
      "InstallLauncherAllUsers=0",
      "PrependPath=1",
      "SimpleInstall=1"
    )
    $proc = Start-Process -FilePath $tmpInstaller -ArgumentList $installArgs -PassThru -Wait
    if (-not $proc -or $proc.ExitCode -ne 0) {
      return $false
    }
    # Refresh PATH from registry for this process.
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = (($machinePath, $userPath) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) -join ";"
    return $true
  } catch {
    return $false
  } finally {
    Remove-Item $tmpInstaller -Force -ErrorAction SilentlyContinue
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
REMOTE=$(curl -sf -H "Authorization: Bearer $UNCHAINED_API_KEY" "$API_URL/web/agent/version" 2>/dev/null || true)
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
curl -sf -H "Authorization: Bearer $UNCHAINED_API_KEY" "$API_URL/web/agent/files" -o "$TMPDIR/update.zip"
if [ ! -s "$TMPDIR/update.zip" ]; then
  echo "ERROR: Download failed."; exit 1
fi

# Save current requirements for diff
OLD_REQS=""
if [ -f requirements.txt ]; then
  OLD_REQS=$(cat requirements.txt)
fi

# Extract — update code files only, never touch .env or .venv
AGENT_DIR="$(pwd)"
mkdir -p "$AGENT_DIR/unchained"
cd "$TMPDIR" && unzip -qo update.zip
cp -f unchained-agent/unchained/*.py "$AGENT_DIR/unchained/" 2>/dev/null || true
cp -f unchained-agent/CLAUDE.md "$AGENT_DIR/" 2>/dev/null || true
cp -f unchained-agent/version.txt "$AGENT_DIR/" 2>/dev/null || true
cp -f unchained-agent/requirements.txt "$AGENT_DIR/" 2>/dev/null || true
cp -f unchained-agent/update.sh "$AGENT_DIR/" 2>/dev/null || true
cp -f unchained-agent/stop.sh "$AGENT_DIR/" 2>/dev/null || true
chmod +x "$AGENT_DIR/update.sh" "$AGENT_DIR/stop.sh" 2>/dev/null || true
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
  foreach ($name in @("CLAUDE.md", "version.txt", "requirements.txt", "update.sh", "update.ps1", "update.bat", "stop.sh", "stop.ps1")) {
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
  Write-Host "Dependencies changed — reinstalling..."
  & ".\.venv\Scripts\python.exe" -m pip install -q -r requirements.txt
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
echo "  ./start.sh            # foreground (see output, Ctrl+C to stop)"
echo "  ./start.sh --daemon   # background (close terminal safely)"
echo ""
if [ -t 0 ]; then
  read -p "Start now? [d]aemon / [f]oreground / [N]o: " -n 1 -r
  echo ""
elif [ -e /dev/tty ]; then
  read -p "Start now? [d]aemon / [f]oreground / [N]o: " -n 1 -r </dev/tty || REPLY=n
  echo ""
else
  echo "Non-interactive — run ./start.sh manually."
  REPLY=n
fi
if [[ $REPLY =~ ^[Dd]$ ]]; then
  cd "$INSTALL_DIR" && ./start.sh --daemon
elif [[ $REPLY =~ ^[Ff]$ ]]; then
  cd "$INSTALL_DIR" && ./start.sh
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
echo "  ./start.sh            # foreground (see output, Ctrl+C to stop)"
echo "  ./start.sh --daemon   # background (close terminal safely)"
echo "  ./start.sh --enable-autostart   # start daemon on reboot/login (macOS)"
echo "  ./start.sh --disable-autostart  # remove reboot/login autostart"
echo "  ./stop.sh             # stop background agent"
echo ""
if [ -t 0 ]; then
  read -p "Start now? [d]aemon / [f]oreground / [N]o: " -n 1 -r
  echo ""
elif [ -e /dev/tty ]; then
  read -p "Start now? [d]aemon / [f]oreground / [N]o: " -n 1 -r </dev/tty || REPLY=n
  echo ""
else
  echo "Non-interactive — run ./start.sh manually."
  REPLY=n
fi
if [[ $REPLY =~ ^[Dd]$ ]]; then
  cd "$INSTALL_DIR" && ./start.sh --daemon
elif [[ $REPLY =~ ^[Ff]$ ]]; then
  cd "$INSTALL_DIR" && ./start.sh
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
  $pythonVersion = "3.13.2"
  $installerName = "python-$pythonVersion-amd64.exe"
  $installerUrl = "https://www.python.org/ftp/python/$pythonVersion/$installerName"
  $tmpInstaller = Join-Path $env:TEMP ("unchained-" + $installerName)

  Write-Host "Python 3.8+ not found. Installing Python runtime..."
  try {
    Invoke-WebRequest -UseBasicParsing -Uri $installerUrl -OutFile $tmpInstaller
    $installArgs = @(
      "/quiet",
      "InstallAllUsers=0",
      "Include_test=0",
      "Include_doc=0",
      "Include_dev=0",
      "Include_pip=1",
      "Include_launcher=1",
      "InstallLauncherAllUsers=0",
      "PrependPath=1",
      "SimpleInstall=1"
    )
    $proc = Start-Process -FilePath $tmpInstaller -ArgumentList $installArgs -PassThru -Wait
    if (-not $proc -or $proc.ExitCode -ne 0) {
      return $false
    }
    # Refresh PATH from registry for this process.
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = (($machinePath, $userPath) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) -join ";"
    return $true
  } catch {
    return $false
  } finally {
    Remove-Item $tmpInstaller -Force -ErrorAction SilentlyContinue
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
        'CWD = os.path.expanduser("~/Projects/unchained/unchained")',
        'CWD = os.path.dirname(os.path.abspath(__file__))',
    )
    # Use plain python instead of uv run (package has venv activated)
    source = source.replace("uv run python cdp_tool.py", "python cdp_tool.py")
    source = source.replace("uv run python scheduler_tool.py", "python scheduler_tool.py")
    source = source.replace("Bash(uv run python cdp_tool.py:*)", "Bash(python cdp_tool.py:*)")
    source = source.replace("Bash(uv run python scheduler_tool.py:*)", "Bash(python scheduler_tool.py:*)")
    source = source.replace(
        '["uv", "run", "python", "cdp_tool.py",',
        '["python", "cdp_tool.py",',
    )
    # Set API URL for the HTTP-based cdp_tool.py
    source = source.replace(
        '    env["CDP_AGENT_ID"] = AGENT_ID\n'
        '    env["CDP_RELAY_HOST"] = RELAY_HOST\n'
        '    env["CDP_RELAY_PORT"] = str(RELAY_PORT)',
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
6. Optional: enable reboot/login autostart via `./start.sh --enable-autostart` (macOS)

## Requirements

- Python 3.8+ (macOS 12.3+ includes Python shims, but you may still need a real python.org/Homebrew install)
- Google Chrome
- Claude Code CLI (`claude`) — install from https://docs.anthropic.com/en/docs/claude-code

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
    unchained/chat_agent_cli.py Chat agent (uses Claude Code CLI)
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
    # Thin HTTP-based cdp_tool.py (proprietary tools stay server-side)
    zf.writestr(f"{prefix}/unchained/cdp_tool.py", _PACKAGED_CDP_TOOL)

    # Non-proprietary source files
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
        zf.writestr("unchained-agent/start.ps1", _START_PS1)
        zf.writestr("unchained-agent/stop.ps1", _STOP_PS1)
        zf.writestr("unchained-agent/update.ps1", _UPDATE_PS1)
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
    """Build an update ZIP (no .env, no start.sh, no venv).

    Returns the ZIP as bytes, ready to be served as a download.
    """
    src_dir = os.path.dirname(os.path.abspath(__file__))
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # version.txt
        zf.writestr("unchained-agent/version.txt", VERSION)

        # requirements.txt
        zf.writestr("unchained-agent/requirements.txt", _REQUIREMENTS)

        # update.sh (executable)
        info = zipfile.ZipInfo("unchained-agent/update.sh")
        info.external_attr = 0o755 << 16
        zf.writestr(info, _UPDATE_SH)
        zf.writestr("unchained-agent/update.ps1", _UPDATE_PS1)
        zf.writestr("unchained-agent/update.bat", _UPDATE_BAT)

        # stop.sh (executable) so installed agents pick up stop/autostart fixes.
        info = zipfile.ZipInfo("unchained-agent/stop.sh")
        info.external_attr = 0o755 << 16
        zf.writestr(info, _STOP_SH)
        zf.writestr("unchained-agent/stop.ps1", _STOP_PS1)

        # CLAUDE.md
        claude_md_path = os.path.join(src_dir, "CLAUDE.md")
        if os.path.exists(claude_md_path):
            zf.writestr("unchained-agent/CLAUDE.md",
                         open(claude_md_path, "r").read())

        _add_source_files(zf, src_dir, "unchained-agent")

    return buf.getvalue()
