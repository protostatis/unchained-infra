#!/usr/bin/env bash
# dev.sh — Start an isolated local stack for development/testing.
#
# Usage:
#   ./dev.sh              # Start relay + web
#   ./dev.sh agent-view   # Also start private core, Chrome bridge, and chat agent
#   ./dev.sh stop         # Kill processes started by either mode
#
# Dev auth (no Google OAuth) is enabled by default.
# Visit http://localhost:8080 to access the UI.
# Use POST /auth/dev to log in:
#   curl -X POST http://localhost:8080/auth/dev \
#     -H 'Content-Type: application/json' \
#     -d '{"email":"dev@localhost"}'

set -euo pipefail
ROOT_DIR=$(cd "$(dirname "$0")" && pwd)
UNCHAINED_DIR="$ROOT_DIR/unchained"
cd "$UNCHAINED_DIR"

RELAY_PORT=${RELAY_PORT:-8765}
WEB_PORT=${WEB_PORT:-8080}
PRIVATE_CORE_PORT=${PRIVATE_CORE_PORT:-8770}
BRIDGE_PORT=${BRIDGE_PORT:-9223}
DEV_EMAIL=${DEV_EMAIL:-dev@localhost}
DEV_PROFILE=${DEV_PROFILE:-dev}
PIDDIR="/tmp/unchained-dev"
DEV_DB_PATH=${UNCHAINED_DB_PATH:-$PIDDIR/auth.db}
DEV_ANALYTICS_DB_PATH=${UNCHAINED_ANALYTICS_DB_PATH:-$PIDDIR/analytics.db}
PRIVATE_CORE_DIR=${PRIVATE_CORE_DIR:-$ROOT_DIR/../unchained-core-private/unchained}

mkdir -p "$PIDDIR"
chmod 700 "$PIDDIR"

stop_servers() {
    for svc in chat-agent bridge web private-core relay; do
        pidfile="$PIDDIR/$svc.pid"
        if [ -f "$pidfile" ]; then
            pid=$(cat "$pidfile")
            if kill -0 "$pid" 2>/dev/null; then
                echo "[dev] Stopping $svc (pid $pid)"
                kill "$pid" 2>/dev/null || true
                sleep 1
                kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
            fi
            rm -f "$pidfile"
        fi
    done
}

start_failed() {
    local message="$1"
    local log_path="${2:-}"
    echo "[dev] ERROR: $message" >&2
    if [ -n "$log_path" ] && [ -f "$log_path" ]; then
        cat "$log_path" >&2
    fi
    stop_servers
    exit 1
}

MODE=${1:-servers}
if [ "$MODE" = "stop" ]; then
    stop_servers
    echo "[dev] Servers stopped."
    exit 0
fi
if [ "$MODE" != "servers" ] && [ "$MODE" != "agent-view" ]; then
    echo "Usage: ./dev.sh [agent-view|stop]" >&2
    exit 2
fi
if [ "$MODE" = "agent-view" ] && [ ! -f "$PRIVATE_CORE_DIR/private_core_server.py" ]; then
    echo "[dev] ERROR: Private core not found at $PRIVATE_CORE_DIR" >&2
    echo "[dev] Set PRIVATE_CORE_DIR to the private unchained source directory." >&2
    exit 1
fi

secret_from_file() {
    local env_value="$1"
    local path="$2"
    local saved=""
    if [ -n "$env_value" ]; then
        printf '%s' "$env_value"
    elif [ -s "$path" ]; then
        IFS= read -r saved < "$path" || true
        printf '%s' "$saved"
    else
        python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
    fi
}

JWT_SECRET=$(secret_from_file "${JWT_SECRET:-}" "$PIDDIR/jwt-secret")
PRIVATE_CORE_TOKEN=$(secret_from_file "${PRIVATE_CORE_TOKEN:-}" "$PIDDIR/private-core-token")
umask 077
printf '%s' "$JWT_SECRET" > "$PIDDIR/jwt-secret"
printf '%s' "$PRIVATE_CORE_TOKEN" > "$PIDDIR/private-core-token"

# Create the dev user before any service starts, then use that one key for the
# relay, web/private-core calls, browser bridge, and chat client. This prevents
# the easy-to-miss split identity that otherwise produces relay code 4003.
DEV_API_KEY=$(UNCHAINED_DB_PATH="$DEV_DB_PATH" DEV_EMAIL="$DEV_EMAIL" uv run python - <<'PY'
import os
from auth import Auth

auth = Auth()
user = auth.get_or_create_user(os.environ["DEV_EMAIL"], name="Local Developer")
if not user.get("api_key"):
    user = auth.approve_user(os.environ["DEV_EMAIL"])
if not user or not user.get("api_key"):
    raise SystemExit("could not create local development API key")
print(user["api_key"])
PY
)
printf '%s' "$DEV_API_KEY" > "$PIDDIR/api-key"

# Stop any existing servers first
stop_servers
sleep 1

PRIVATE_CORE_URL_VALUE=""
PRIVATE_CORE_MODE_VALUE="inprocess"
if [ "$MODE" = "agent-view" ]; then
    PRIVATE_CORE_URL_VALUE="http://127.0.0.1:$PRIVATE_CORE_PORT"
    PRIVATE_CORE_MODE_VALUE="http"
fi

# Start relay
echo "[dev] Starting relay on port $RELAY_PORT ..."
JWT_SECRET="$JWT_SECRET" \
UNCHAINED_DB_PATH="$DEV_DB_PATH" \
PRIVATE_CORE_TOKEN="$PRIVATE_CORE_TOKEN" \
    uv run python relay.py --port "$RELAY_PORT" > "$PIDDIR/relay.log" 2>&1 &
echo $! > "$PIDDIR/relay.pid"
sleep 1

if ! kill -0 "$(cat "$PIDDIR/relay.pid")" 2>/dev/null; then
    start_failed "Relay failed to start. Check $PIDDIR/relay.log" "$PIDDIR/relay.log"
fi
echo "[dev] Relay running (pid $(cat "$PIDDIR/relay.pid"))"

if [ "$MODE" = "agent-view" ]; then
    echo "[dev] Starting private core on port $PRIVATE_CORE_PORT ..."
    (
        cd "$PRIVATE_CORE_DIR"
        exec env \
            PRIVATE_CORE_TOKEN="$PRIVATE_CORE_TOKEN" \
            UNCHAINED_API_KEY="$DEV_API_KEY" \
            RELAY_INTERNAL_HOST=127.0.0.1 \
            RELAY_INTERNAL_PORT="$RELAY_PORT" \
            uv run python private_core_server.py --host 127.0.0.1 --port "$PRIVATE_CORE_PORT"
    ) > "$PIDDIR/private-core.log" 2>&1 &
    echo $! > "$PIDDIR/private-core.pid"
    sleep 1
    if ! kill -0 "$(cat "$PIDDIR/private-core.pid")" 2>/dev/null; then
        start_failed "Private core failed to start. Check $PIDDIR/private-core.log" "$PIDDIR/private-core.log"
    fi
    echo "[dev] Private core running (pid $(cat "$PIDDIR/private-core.pid"))"
fi

# Start web server (no GOOGLE_CLIENT_ID = dev auth enabled)
echo "[dev] Starting web server on port $WEB_PORT ..."
JWT_SECRET="$JWT_SECRET" \
UNCHAINED_API_KEY="$DEV_API_KEY" \
UNCHAINED_DB_PATH="$DEV_DB_PATH" \
UNCHAINED_ANALYTICS_DB_PATH="$DEV_ANALYTICS_DB_PATH" \
WEB_PORT="$WEB_PORT" \
RELAY_URL="ws://127.0.0.1:$RELAY_PORT/tunnel" \
RELAY_INTERNAL_URL="ws://127.0.0.1:$RELAY_PORT" \
PRIVATE_CORE_URL="$PRIVATE_CORE_URL_VALUE" \
PRIVATE_CORE_MODE="$PRIVATE_CORE_MODE_VALUE" \
PRIVATE_CORE_TOKEN="$PRIVATE_CORE_TOKEN" \
    uv run python -m web --port "$WEB_PORT" > "$PIDDIR/web.log" 2>&1 &
echo $! > "$PIDDIR/web.pid"
sleep 2

if ! kill -0 "$(cat "$PIDDIR/web.pid")" 2>/dev/null; then
    start_failed "Web server failed to start. Check $PIDDIR/web.log" "$PIDDIR/web.log"
fi
echo "[dev] Web server running (pid $(cat "$PIDDIR/web.pid"))"

if [ "$MODE" = "agent-view" ]; then
    echo "[dev] Starting Chrome bridge profile '$DEV_PROFILE' on port $BRIDGE_PORT ..."
    UNCHAINED_API_KEY="$DEV_API_KEY" \
        uv run python chrome_bridge.py start \
            --relay "ws://127.0.0.1:$RELAY_PORT/tunnel" \
            --key "$DEV_API_KEY" \
            --no-headless \
            --profile "$DEV_PROFILE" \
            --port "$BRIDGE_PORT" > "$PIDDIR/bridge.log" 2>&1 &
    echo $! > "$PIDDIR/bridge.pid"
    sleep 2
    if ! kill -0 "$(cat "$PIDDIR/bridge.pid")" 2>/dev/null; then
        start_failed "Chrome bridge failed to start. Check $PIDDIR/bridge.log" "$PIDDIR/bridge.log"
    fi
    echo "[dev] Chrome bridge running (pid $(cat "$PIDDIR/bridge.pid"))"

    echo "[dev] Starting local chat agent ..."
    CDP_PROFILE="$DEV_PROFILE" \
    UNCHAINED_API_KEY="$DEV_API_KEY" \
    UNCHAINED_DATA_DIR="$PIDDIR/agent-data" \
    UNCHAINED_SERVER="ws://127.0.0.1:$WEB_PORT/chat/ws" \
    UNCHAINED_RELAY_HOST=127.0.0.1 \
    UNCHAINED_RELAY_PORT="$RELAY_PORT" \
    PYTHONUNBUFFERED=1 \
        uv run python chat_agent_cli.py > "$PIDDIR/chat-agent.log" 2>&1 &
    echo $! > "$PIDDIR/chat-agent.pid"
    sleep 2
    if ! kill -0 "$(cat "$PIDDIR/chat-agent.pid")" 2>/dev/null; then
        start_failed "Chat agent failed to start. Check $PIDDIR/chat-agent.log" "$PIDDIR/chat-agent.log"
    fi
    echo "[dev] Chat agent running (pid $(cat "$PIDDIR/chat-agent.pid"))"
fi

echo ""
echo "========================================="
echo " Unchained dev servers running"
echo "========================================="
echo " Web UI:   http://localhost:$WEB_PORT"
echo " First Look: http://localhost:$WEB_PORT/first-look"
echo " Demo (legacy alias): http://localhost:$WEB_PORT/demo"
echo " Setup:    http://localhost:$WEB_PORT/setup"
echo " Gemini:   http://localhost:$WEB_PORT/chat-gemini"
echo " Relay:    ws://localhost:$RELAY_PORT/tunnel"
echo " Dev user: $DEV_EMAIL"
echo " API key:  $PIDDIR/api-key (mode 600)"
if [ "$MODE" = "agent-view" ]; then
    echo " Browser:  Chrome bridge profile '$DEV_PROFILE' on CDP :$BRIDGE_PORT"
    echo " PrivCore: http://127.0.0.1:$PRIVATE_CORE_PORT"
fi
echo ""
if [ "$MODE" = "agent-view" ]; then
    echo " Agent View test:"
    echo "   1. Open http://localhost:$WEB_PORT/local?provider=opencode-cli"
    echo "      in a normal browser window (not the controlled '$DEV_PROFILE' Chrome)."
    echo "   2. Select Dev Login, then prompt the agent to navigate."
    echo "   3. Open Browser Preview to inspect the semantic Agent View."
else
    echo " Full Agent View stack: ./dev.sh agent-view"
fi
echo ""
echo " Logs:     $PIDDIR/relay.log"
echo "           $PIDDIR/web.log"
if [ "$MODE" = "agent-view" ]; then
    echo "           $PIDDIR/private-core.log"
    echo "           $PIDDIR/bridge.log"
    echo "           $PIDDIR/chat-agent.log"
fi
echo ""
echo " Stop:     ./dev.sh stop"
echo "========================================="
