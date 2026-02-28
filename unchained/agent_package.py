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

VERSION = "0.3.9"
MIN_VERSION = "0.2.0"

# Source files to include as-is (non-proprietary)
_PACKAGE_FILES = {
    # dest path in ZIP → source filename
    "unchained/chrome_bridge.py": "chrome_bridge.py",
    "unchained/chat_agent_cli.py": "chat_agent_cli.py",
    "unchained/scheduled_tasks.py": "scheduled_tasks.py",
    "scheduled_jobs.json": "scheduled_jobs.example.json",
    "unchained/auth.py": "auth.py",
    "unchained/nudge.py": "nudge.py",
}

_REQUIREMENTS = """\
websockets>=13.0
httpx
aiohttp
PyJWT>=2.0
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
        print("Commands: ddm, navigate, click, type, js, screenshot, intel, tabs, new-tab, close-tab")
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
            url = args[0] if args else "about:blank"
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
if [[ "${1:-}" == "--daemon" || "${1:-}" == "-d" ]]; then
  DAEMON=true
fi

# Load config
if [ ! -f .env ]; then
  echo "ERROR: .env not found. Re-download from the web UI."; exit 1
fi
set -a; source .env; set +a

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

if $DAEMON; then
  # --- Daemon mode: run in background, log to file ---
  LOGFILE="$(pwd)/agent.log"
  PIDFILE="$(pwd)/.agent.pid"

  # Check if already running
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Agent is already running (PID $(cat "$PIDFILE")). Stop it first: ./stop.sh"
    exit 1
  fi

  echo "Starting in daemon mode..."
  echo "Log file: $LOGFILE"

  # Launch both processes in a subshell, redirect to log
  (
    echo "[$(date)] Starting Chrome bridge..."
    python unchained/chrome_bridge.py start \
      --relay "wss://$UNCHAINED_RELAY_HOST/tunnel" &
    BRIDGE_PID=$!

    sleep 2

    echo "[$(date)] Starting chat agent..."
    trap "kill $BRIDGE_PID 2>/dev/null; exit" INT TERM
    PYTHONUNBUFFERED=1 python unchained/chat_agent_cli.py
  ) >> "$LOGFILE" 2>&1 &
  DAEMON_PID=$!

  echo "$DAEMON_PID" > "$PIDFILE"
  echo "Agent started (PID $DAEMON_PID)"
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
  PYTHONUNBUFFERED=1 python unchained/chat_agent_cli.py
fi
"""


_STOP_SH = r"""#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

PIDFILE="$(pwd)/.agent.pid"

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
  echo "ERROR: python3 is required. Install Python 3.9+."; exit 1
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
  "$BASE_URL/web/download-agent?install_token=$INSTALL_TOKEN" -o "$TMPFILE")
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

## Requirements

- Python 3.9+ (macOS has this built in)
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

### "unsupported operand type(s) for |"
Python version too old. Needs Python 3.9+. Check with:
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
    The agent needs Chrome and Python 3.9+.
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

        # Inject future annotations for Python 3.9 compat (X | Y syntax)
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
    """Build a code-only update ZIP (no .env, no start.sh, no venv).

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

        # CLAUDE.md
        claude_md_path = os.path.join(src_dir, "CLAUDE.md")
        if os.path.exists(claude_md_path):
            zf.writestr("unchained-agent/CLAUDE.md",
                         open(claude_md_path, "r").read())

        _add_source_files(zf, src_dir, "unchained-agent")

    return buf.getvalue()
