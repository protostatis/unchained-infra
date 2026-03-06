#!/usr/bin/env bash
# dev.sh — Start local relay + web server for development/testing.
#
# Usage:
#   ./dev.sh          # Start both servers
#   ./dev.sh stop     # Kill running servers
#
# Dev auth (no Google OAuth) is enabled by default.
# Visit http://localhost:8080 to access the UI.
# Use POST /auth/dev to log in:
#   curl -X POST http://localhost:8080/auth/dev \
#     -H 'Content-Type: application/json' \
#     -d '{"email":"dev@localhost"}'

set -euo pipefail
cd "$(dirname "$0")/unchained"

RELAY_PORT=${RELAY_PORT:-8765}
WEB_PORT=${WEB_PORT:-8080}
JWT_SECRET=${JWT_SECRET:-$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)}
PIDDIR="/tmp/unchained-dev"

mkdir -p "$PIDDIR"

stop_servers() {
    for svc in relay web; do
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

if [ "${1:-}" = "stop" ]; then
    stop_servers
    echo "[dev] Servers stopped."
    exit 0
fi

# Stop any existing servers first
stop_servers
sleep 1

# Start relay
echo "[dev] Starting relay on port $RELAY_PORT ..."
JWT_SECRET="$JWT_SECRET" \
    uv run python relay.py --port "$RELAY_PORT" > "$PIDDIR/relay.log" 2>&1 &
echo $! > "$PIDDIR/relay.pid"
sleep 1

if ! kill -0 "$(cat "$PIDDIR/relay.pid")" 2>/dev/null; then
    echo "[dev] ERROR: Relay failed to start. Check $PIDDIR/relay.log"
    cat "$PIDDIR/relay.log"
    exit 1
fi
echo "[dev] Relay running (pid $(cat "$PIDDIR/relay.pid"))"

# Start web server (no GOOGLE_CLIENT_ID = dev auth enabled)
echo "[dev] Starting web server on port $WEB_PORT ..."
JWT_SECRET="$JWT_SECRET" \
WEB_PORT="$WEB_PORT" \
RELAY_URL="ws://127.0.0.1:$RELAY_PORT/tunnel" \
RELAY_INTERNAL_URL="ws://127.0.0.1:$RELAY_PORT" \
    uv run python web.py --port "$WEB_PORT" > "$PIDDIR/web.log" 2>&1 &
echo $! > "$PIDDIR/web.pid"
sleep 2

if ! kill -0 "$(cat "$PIDDIR/web.pid")" 2>/dev/null; then
    echo "[dev] ERROR: Web server failed to start. Check $PIDDIR/web.log"
    cat "$PIDDIR/web.log"
    exit 1
fi
echo "[dev] Web server running (pid $(cat "$PIDDIR/web.pid"))"

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
echo ""
echo " Logs:     $PIDDIR/relay.log"
echo "           $PIDDIR/web.log"
echo ""
echo " Stop:     ./dev.sh stop"
echo "========================================="
