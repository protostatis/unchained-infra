#!/usr/bin/env bash
# dev.sh — Start an isolated local stack for development/testing.
#
# Usage:
#   ./dev.sh              # Start relay + web
#   ./dev.sh agent-view   # Also start private core, Chrome bridge, and chat agent
#   ./dev.sh hosted-trial # Start relay, private core, web, bridge, trial worker + scheduler
#   ./dev.sh smoke-hosted-trial # hosted-trial + runtime readiness smoke, then stop
#   ./dev.sh stop         # Kill processes started by any mode
#
# Dev auth (no Google OAuth) is enabled by default.
# Visit http://localhost:8080 to access the UI.
# Use POST /auth/dev to log in:
#   curl -X POST http://localhost:8080/auth/dev \
#     -H 'Content-Type: application/json' \
#     -d '{"email":"dev@localhost"}'
#
# hosted-trial requires OPENROUTER_API_KEY already exported in the environment.
# Real OpenRouter calls may incur spend.
#
# smoke-hosted-trial uses OPENROUTER_API_KEY=control-plane-placeholder so no
# real provider calls are made. It starts the full stack on alternate ports,
# verifies process health and status endpoint readiness, then stops everything.

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

# --- smoke-hosted-trial: force safe defaults ---
if [ "${1:-}" = "smoke-hosted-trial" ]; then
    RELAY_PORT=18765
    WEB_PORT=18080
    PRIVATE_CORE_PORT=18770
    BRIDGE_PORT=19223
    DEV_EMAIL="dev@localhost"
    DEV_PROFILE="dev"
    PIDDIR="/tmp/unchained-dev-smoke"
    DEV_DB_PATH="$PIDDIR/auth.db"
    DEV_ANALYTICS_DB_PATH="$PIDDIR/analytics.db"
    # Never inherit live credentials or persistent paths in control-plane smoke.
    OPENROUTER_API_KEY="control-plane-placeholder"
    JWT_SECRET=""
    PRIVATE_CORE_TOKEN=""
    TRIAL_AGENT_KEY=""
    HOSTED_AGENT_SERVICE_TOKEN=""
    TRIAL_AGENT_ID="trial-local-smoke"
    ADMIN_EMAILS=""
    unset UNCHAINED_DATA_DIR UNCHAINED_SESSIONS_DIR \
        UNCHAINED_HOSTED_DATA_DIR UNCHAINED_NEW_CHAT_STATUS_DIR \
        UNCHAINED_SCHEDULER_DIR
    mkdir -p "$PIDDIR"
    chmod 700 "$PIDDIR"
fi

stop_servers() {
    for svc in scheduler trial-agent chat-agent bridge web private-core relay; do
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
# smoke-hosted-trial is hosted-trial with extra verification at the end.
IS_HOSTED_TRIAL=false
IS_SMOKE=false

if [ "$MODE" = "stop" ]; then
    stop_servers
    echo "[dev] Servers stopped."
    exit 0
fi
if [ "$MODE" != "servers" ] && [ "$MODE" != "agent-view" ] && [ "$MODE" != "hosted-trial" ] && [ "$MODE" != "smoke-hosted-trial" ]; then
    echo "Usage: ./dev.sh [servers|agent-view|hosted-trial|smoke-hosted-trial|stop]" >&2
    exit 2
fi
if [ "$MODE" = "smoke-hosted-trial" ]; then
    IS_HOSTED_TRIAL=true
    IS_SMOKE=true
elif [ "$MODE" = "hosted-trial" ]; then
    IS_HOSTED_TRIAL=true
fi

# hosted-trial: the Dev Login button hardcodes dev@localhost, so fail
# early if DEV_EMAIL is overridden to something else (it would create a
# second identity that doesn't match the browser button).
if [ "$IS_HOSTED_TRIAL" = true ] && [ "$DEV_EMAIL" != "dev@localhost" ]; then
    echo "[dev] ERROR: hosted-trial and smoke-hosted-trial require DEV_EMAIL=dev@localhost (got '$DEV_EMAIL')." >&2
    echo "[dev] The browser Dev Login button uses dev@localhost; a different email creates a split identity." >&2
    exit 1
fi

# Private core is required for both agent-view and hosted-trial.
NEED_PRIVATE_CORE=false
if [ "$MODE" = "agent-view" ] || [ "$IS_HOSTED_TRIAL" = true ]; then
    NEED_PRIVATE_CORE=true
fi
if [ "$NEED_PRIVATE_CORE" = true ] && [ ! -f "$PRIVATE_CORE_DIR/private_core_server.py" ]; then
    echo "[dev] ERROR: Private core not found at $PRIVATE_CORE_DIR" >&2
    echo "[dev] Set PRIVATE_CORE_DIR to the private unchained source directory." >&2
    exit 1
fi

# hosted-trial requires OPENROUTER_API_KEY already exported.
OPENROUTER_API_KEY_VALUE=""
if [ "$IS_HOSTED_TRIAL" = true ]; then
    if [ -z "${OPENROUTER_API_KEY:-}" ]; then
        echo "[dev] ERROR: OPENROUTER_API_KEY must be exported for $MODE mode." >&2
        echo "[dev] Example: export OPENROUTER_API_KEY=sk-or-..." >&2
        echo "[dev] For smoke testing without real provider calls, use smoke-hosted-trial mode." >&2
        exit 1
    fi
    # Keep the provider credential out of relay, web, private-core, bridge, and
    # scheduler environments. It is passed explicitly only to the trial worker.
    OPENROUTER_API_KEY_VALUE="$OPENROUTER_API_KEY"
    unset OPENROUTER_API_KEY
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
# hosted-trial tokens — distinct from the dev API key.
TRIAL_AGENT_KEY=""
HOSTED_AGENT_SERVICE_TOKEN=""
if [ "$IS_HOSTED_TRIAL" = true ]; then
    TRIAL_AGENT_KEY=$(secret_from_file "${TRIAL_AGENT_KEY:-}" "$PIDDIR/trial-agent-key")
    HOSTED_AGENT_SERVICE_TOKEN=$(secret_from_file "${HOSTED_AGENT_SERVICE_TOKEN:-}" "$PIDDIR/hosted-agent-service-token")
fi
umask 077
printf '%s' "$JWT_SECRET" > "$PIDDIR/jwt-secret"
printf '%s' "$PRIVATE_CORE_TOKEN" > "$PIDDIR/private-core-token"
if [ "$IS_HOSTED_TRIAL" = true ]; then
    printf '%s' "$TRIAL_AGENT_KEY" > "$PIDDIR/trial-agent-key"
    printf '%s' "$HOSTED_AGENT_SERVICE_TOKEN" > "$PIDDIR/hosted-agent-service-token"
fi
chmod 600 "$PIDDIR/jwt-secret" "$PIDDIR/private-core-token"
if [ "$IS_HOSTED_TRIAL" = true ]; then
    chmod 600 "$PIDDIR/trial-agent-key" "$PIDDIR/hosted-agent-service-token"
fi

TRIAL_AGENT_ID="${TRIAL_AGENT_ID:-trial-local}"

# --- hosted-trial: verify token nonempty and distinct ---
if [ "$IS_HOSTED_TRIAL" = true ]; then
    if [ -z "$TRIAL_AGENT_KEY" ] || [ -z "$HOSTED_AGENT_SERVICE_TOKEN" ]; then
        echo "[dev] ERROR: Trial key or hosted service token is empty." >&2
        exit 1
    fi
    if [ "$TRIAL_AGENT_KEY" = "$HOSTED_AGENT_SERVICE_TOKEN" ]; then
        echo "[dev] ERROR: Trial key and hosted service token must be distinct." >&2
        exit 1
    fi
    if [ "$TRIAL_AGENT_KEY" = "$JWT_SECRET" ] || [ "$TRIAL_AGENT_KEY" = "$PRIVATE_CORE_TOKEN" ]; then
        echo "[dev] ERROR: Trial key must not match JWT_SECRET or PRIVATE_CORE_TOKEN." >&2
        exit 1
    fi
    if [ "$HOSTED_AGENT_SERVICE_TOKEN" = "$JWT_SECRET" ] || [ "$HOSTED_AGENT_SERVICE_TOKEN" = "$PRIVATE_CORE_TOKEN" ]; then
        echo "[dev] ERROR: Hosted service token must not match JWT_SECRET or PRIVATE_CORE_TOKEN." >&2
        exit 1
    fi
fi

# --- Local storage paths (hosted-trial only; under $PIDDIR to avoid ~/.unchained or /data) ---
if [ "$IS_HOSTED_TRIAL" = true ]; then
    export UNCHAINED_DATA_DIR="${UNCHAINED_DATA_DIR:-$PIDDIR/app-data}"
    export UNCHAINED_SESSIONS_DIR="${UNCHAINED_SESSIONS_DIR:-$PIDDIR/sessions}"
    export UNCHAINED_HOSTED_DATA_DIR="${UNCHAINED_HOSTED_DATA_DIR:-$PIDDIR/hosted-conversations}"
    export UNCHAINED_NEW_CHAT_STATUS_DIR="${UNCHAINED_NEW_CHAT_STATUS_DIR:-$PIDDIR/sessions/new-chat-transitions}"
    export UNCHAINED_SCHEDULER_DIR="${UNCHAINED_SCHEDULER_DIR:-$PIDDIR/scheduler-jobs}"
    mkdir -p "$UNCHAINED_DATA_DIR" "$UNCHAINED_SESSIONS_DIR" \
        "$UNCHAINED_HOSTED_DATA_DIR" "$UNCHAINED_NEW_CHAT_STATUS_DIR" \
        "$UNCHAINED_SCHEDULER_DIR"
fi

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
chmod 600 "$PIDDIR/api-key"

# Verify the trial key and service token are distinct from the dev API key.
if [ "$IS_HOSTED_TRIAL" = true ]; then
    if [ "$TRIAL_AGENT_KEY" = "$DEV_API_KEY" ] || [ "$HOSTED_AGENT_SERVICE_TOKEN" = "$DEV_API_KEY" ]; then
        echo "[dev] ERROR: Trial key or hosted service token must not match DEV_API_KEY." >&2
        exit 1
    fi
fi

# Stop any existing servers first
stop_servers
sleep 1

PRIVATE_CORE_URL_VALUE=""
PRIVATE_CORE_MODE_VALUE="inprocess"
if [ "$NEED_PRIVATE_CORE" = true ]; then
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

if [ "$NEED_PRIVATE_CORE" = true ]; then
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

# --- Build web env: shared base + mode-specific extras ---
echo "[dev] Starting web server on port $WEB_PORT ..."

# Shared base env (all modes) as an array for safe quoting.
WEB_BASE_ENV=(
    "JWT_SECRET=$JWT_SECRET"
    "UNCHAINED_API_KEY=$DEV_API_KEY"
    "UNCHAINED_DB_PATH=$DEV_DB_PATH"
    "UNCHAINED_ANALYTICS_DB_PATH=$DEV_ANALYTICS_DB_PATH"
    "WEB_PORT=$WEB_PORT"
    "RELAY_URL=ws://127.0.0.1:$RELAY_PORT/tunnel"
    "RELAY_INTERNAL_URL=ws://127.0.0.1:$RELAY_PORT"
    "PRIVATE_CORE_URL=$PRIVATE_CORE_URL_VALUE"
    "PRIVATE_CORE_MODE=$PRIVATE_CORE_MODE_VALUE"
    "PRIVATE_CORE_TOKEN=$PRIVATE_CORE_TOKEN"
)

# Mode-specific extras added to the array.
if [ "$IS_HOSTED_TRIAL" = true ]; then
    WEB_ADMIN="${ADMIN_EMAILS:-$DEV_EMAIL}"
    WEB_BASE_ENV+=(
        "GOOGLE_CLIENT_ID="
        "UNCHAINED_PUBLIC_BASE_URL=http://127.0.0.1:$WEB_PORT"
        "UNCHAINED_DATA_DIR=${UNCHAINED_DATA_DIR:-$PIDDIR/app-data}"
        "UNCHAINED_SESSIONS_DIR=${UNCHAINED_SESSIONS_DIR:-$PIDDIR/sessions}"
        "UNCHAINED_HOSTED_DATA_DIR=${UNCHAINED_HOSTED_DATA_DIR:-$PIDDIR/hosted-conversations}"
        "UNCHAINED_NEW_CHAT_STATUS_DIR=${UNCHAINED_NEW_CHAT_STATUS_DIR:-$PIDDIR/sessions/new-chat-transitions}"
        "UNCHAINED_SCHEDULER_DIR=${UNCHAINED_SCHEDULER_DIR:-$PIDDIR/scheduler-jobs}"
        "TRIAL_AGENT_KEY=$TRIAL_AGENT_KEY"
        "TRIAL_AGENT_ID=$TRIAL_AGENT_ID"
        "HOSTED_AGENT_SERVICE_TOKEN=$HOSTED_AGENT_SERVICE_TOKEN"
        "ADMIN_EMAILS=$WEB_ADMIN"
    )
else
    # servers / agent-view: preserve inherited env, no hosted-specific vars.
    WEB_BASE_ENV+=(
        "GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID:-}"
        "UNCHAINED_PUBLIC_BASE_URL=${UNCHAINED_PUBLIC_BASE_URL:-}"
        "UNCHAINED_DATA_DIR=${UNCHAINED_DATA_DIR:-}"
        "UNCHAINED_SESSIONS_DIR=${UNCHAINED_SESSIONS_DIR:-}"
        "UNCHAINED_HOSTED_DATA_DIR=${UNCHAINED_HOSTED_DATA_DIR:-}"
        "UNCHAINED_NEW_CHAT_STATUS_DIR=${UNCHAINED_NEW_CHAT_STATUS_DIR:-}"
        "UNCHAINED_SCHEDULER_DIR=${UNCHAINED_SCHEDULER_DIR:-}"
        "TRIAL_AGENT_KEY=${TRIAL_AGENT_KEY:-}"
        "TRIAL_AGENT_ID=${TRIAL_AGENT_ID:-}"
        "HOSTED_AGENT_SERVICE_TOKEN=${HOSTED_AGENT_SERVICE_TOKEN:-}"
        "ADMIN_EMAILS=${ADMIN_EMAILS:-}"
    )
fi

env "${WEB_BASE_ENV[@]}" \
    uv run python -m web --host 127.0.0.1 --port "$WEB_PORT" > "$PIDDIR/web.log" 2>&1 &
echo $! > "$PIDDIR/web.pid"
sleep 2

if ! kill -0 "$(cat "$PIDDIR/web.pid")" 2>/dev/null; then
    start_failed "Web server failed to start. Check $PIDDIR/web.log" "$PIDDIR/web.log"
fi
echo "[dev] Web server running (pid $(cat "$PIDDIR/web.pid"))"

# Conditionally start bridge (agent-view and hosted-trial both need it).
NEED_BRIDGE=false
if [ "$MODE" = "agent-view" ] || [ "$IS_HOSTED_TRIAL" = true ]; then
    NEED_BRIDGE=true
fi

if [ "$NEED_BRIDGE" = true ]; then
    echo "[dev] Starting Chrome bridge profile '$DEV_PROFILE' on port $BRIDGE_PORT ..."
    BRIDGE_ENV=("UNCHAINED_API_KEY=$DEV_API_KEY")
    if [ "$IS_HOSTED_TRIAL" = true ]; then
        BRIDGE_ENV+=(
            "UNCHAINED_PUBLIC_BASE_URL=http://127.0.0.1:$WEB_PORT"
            "UNCHAINED_DATA_DIR=${UNCHAINED_DATA_DIR:-$PIDDIR/app-data}"
        )
    fi
    env "${BRIDGE_ENV[@]}" \
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
fi

if [ "$MODE" = "agent-view" ]; then
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

if [ "$IS_HOSTED_TRIAL" = true ]; then
    echo "[dev] Starting hosted trial worker (OpenRouter) ..."
    TRIAL_SESSIONS_DIR="${UNCHAINED_SESSIONS_DIR:-$PIDDIR/sessions}"
    mkdir -p "$TRIAL_SESSIONS_DIR"
    OPENROUTER_API_KEY="$OPENROUTER_API_KEY_VALUE" \
    UNCHAINED_API_KEY="$TRIAL_AGENT_KEY" \
    UNCHAINED_SERVER="ws://127.0.0.1:$WEB_PORT" \
    RELAY_HOST=127.0.0.1 \
    RELAY_PORT="$RELAY_PORT" \
    SESSION_DIR="$TRIAL_SESSIONS_DIR" \
    UNCHAINED_SESSIONS_DIR="$TRIAL_SESSIONS_DIR" \
    UNCHAINED_DATA_DIR="${UNCHAINED_DATA_DIR:-$PIDDIR/app-data}" \
    PRIVATE_CORE_URL="$PRIVATE_CORE_URL_VALUE" \
    PRIVATE_CORE_MODE="$PRIVATE_CORE_MODE_VALUE" \
    PRIVATE_CORE_TOKEN="$PRIVATE_CORE_TOKEN" \
    HOSTED_AGENT_SERVICE_TOKEN="$HOSTED_AGENT_SERVICE_TOKEN" \
    PYTHONUNBUFFERED=1 \
        uv run python chat_agent_openrouter.py \
            --agent "$TRIAL_AGENT_ID" \
            --key "$TRIAL_AGENT_KEY" \
            --server "ws://127.0.0.1:$WEB_PORT" \
            > "$PIDDIR/trial-agent.log" 2>&1 &
    echo $! > "$PIDDIR/trial-agent.pid"
    sleep 2
    if ! kill -0 "$(cat "$PIDDIR/trial-agent.pid")" 2>/dev/null; then
        start_failed "Trial worker failed to start. Check $PIDDIR/trial-agent.log" "$PIDDIR/trial-agent.log"
    fi
    echo "[dev] Trial worker running (pid $(cat "$PIDDIR/trial-agent.pid"))"

    # --- Bounded readiness: use DEV_API_KEY (Bearer) for status check ---
    # handle_dev_auth sets a cookie only, it does NOT return a JSON "token".
    # _authenticate supports API keys directly via Authorization: Bearer.
    echo "[dev] Verifying trial worker readiness (via API key) ..."
    READY=0
    for i in $(seq 1 15); do
        STATUS=$(curl -sSf --max-time 3 \
            "http://127.0.0.1:$WEB_PORT/web/chat/status?model=google/gemini-3.1-flash-lite" \
            -H "Authorization: Bearer $DEV_API_KEY" 2>/dev/null || echo '{}')
        CHAT_OK=$(echo "$STATUS" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("1" if d.get("chat_connected") else "0")' 2>/dev/null || echo "0")
        BRIDGE_OK=$(echo "$STATUS" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("1" if d.get("bridge_connected") else "0")' 2>/dev/null || echo "0")
        if [ "$CHAT_OK" = "1" ] && [ "$BRIDGE_OK" = "1" ]; then
            READY=1
            break
        fi
        sleep 1
    done
    if [ "$READY" -ne 1 ]; then
        start_failed "Trial worker did not become ready within 15s (chat=$CHAT_OK bridge=$BRIDGE_OK). Check $PIDDIR/trial-agent.log, $PIDDIR/bridge.log, and $PIDDIR/web.log"
    fi
    echo "[dev] Trial worker ready (chat and bridge connected)"
fi

# --- hosted-trial: start scheduler daemon ---
if [ "$IS_HOSTED_TRIAL" = true ]; then
    SCHED_JOBS_DIR="${UNCHAINED_SCHEDULER_DIR:-$PIDDIR/scheduler-jobs}"
    mkdir -p "$SCHED_JOBS_DIR"
    echo "[dev] Starting scheduled tasks daemon ..."
    UNCHAINED_API_URL="http://127.0.0.1:$WEB_PORT" \
    SCHEDULER_DEFAULT_MODEL="${OPENROUTER_MODEL:-google/gemini-3.1-flash-lite}" \
        uv run python scheduled_tasks.py daemon-multi \
            --jobs-dir "$SCHED_JOBS_DIR" \
            --db-path "$DEV_DB_PATH" \
            --poll-seconds 30 \
            > "$PIDDIR/scheduler.log" 2>&1 &
    echo $! > "$PIDDIR/scheduler.pid"
    sleep 1
    if ! kill -0 "$(cat "$PIDDIR/scheduler.pid")" 2>/dev/null; then
        start_failed "Scheduler failed to start. Check $PIDDIR/scheduler.log" "$PIDDIR/scheduler.log"
    fi
    echo "[dev] Scheduler running (pid $(cat "$PIDDIR/scheduler.pid"))"
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
if [ "$IS_HOSTED_TRIAL" = true ]; then
    echo " Browser:  Chrome bridge profile '$DEV_PROFILE' on CDP :$BRIDGE_PORT"
    echo " PrivCore: http://127.0.0.1:$PRIVATE_CORE_PORT"
    echo " Trial ID: $TRIAL_AGENT_ID"
    echo ""
    echo " Trial key:    $PIDDIR/trial-agent-key (mode 600)"
    echo " Svc token:    $PIDDIR/hosted-agent-service-token (mode 600)"
fi
echo ""
if [ "$MODE" = "agent-view" ]; then
    echo " Agent View test:"
    echo "   1. Open http://localhost:$WEB_PORT/local?provider=opencode-cli"
    echo "      in a normal browser window (not the controlled '$DEV_PROFILE' Chrome)."
    echo "   2. Select Dev Login, then prompt the agent to navigate."
    echo "   3. Open Browser Preview to inspect the semantic Agent View."
elif [ "$IS_HOSTED_TRIAL" = true ]; then
    echo " Hosted Trial test:"
    echo "   1. Open http://localhost:$WEB_PORT/trial in a browser."
    echo "   2. Select Dev Login, then use the trial chat UI."
    echo "   3. Browser Preview shows the controlled '$DEV_PROFILE' Chrome."
    echo "   NOTE: Real OpenRouter calls may incur spend."
    echo ""
    echo " Grant credit to $DEV_EMAIL (admin panel or cookie jar):"
    echo "   Login via curl:"
    echo "     curl -c $PIDDIR/cookies.txt -X POST http://localhost:$WEB_PORT/auth/dev \\"
    echo "       -H 'Content-Type: application/json' -d '{\"email\":\"$DEV_EMAIL\"}'"
    echo "     ID=\$(curl -sb $PIDDIR/cookies.txt http://localhost:$WEB_PORT/auth/me | python3 -c 'import sys,json;print(json.load(sys.stdin)[\"user_id\"])')"
    echo "     curl -b $PIDDIR/cookies.txt -X POST http://localhost:$WEB_PORT/admin/credit/grant \\"
    echo "       -H 'Content-Type: application/json' \\"
    echo "       -d '{\"user_id\":\"'\$ID'\",\"amount_usd\":\"1.00\",\"operation_id\":\"dev-grant-001\"}'"
    echo ""
    echo " Developer status check:"
    echo "     curl -H \"Authorization: Bearer \$(cat $PIDDIR/api-key)\" \\"
    echo "       'http://localhost:$WEB_PORT/web/chat/status?model=google/gemini-3.1-flash-lite'"
fi
echo ""
echo " Logs:     $PIDDIR/relay.log"
echo "           $PIDDIR/web.log"
if [ "$NEED_PRIVATE_CORE" = true ]; then
    echo "           $PIDDIR/private-core.log"
    echo "           $PIDDIR/bridge.log"
fi
if [ "$MODE" = "agent-view" ]; then
    echo "           $PIDDIR/chat-agent.log"
elif [ "$IS_HOSTED_TRIAL" = true ]; then
    echo "           $PIDDIR/trial-agent.log"
    echo "           $PIDDIR/scheduler.log"
fi
echo ""
echo " Stop:     ./dev.sh stop"
echo "========================================="

# --- smoke-hosted-trial: runtime process + status verification ---
if [ "$IS_SMOKE" = true ]; then
    echo ""
    echo "[smoke] Checking running processes..."
    FAILURES=0
    for svc in relay private-core web bridge trial-agent scheduler; do
        pidfile="$PIDDIR/$svc.pid"
        if ! [ -f "$pidfile" ]; then
            echo "[smoke] FAIL: $svc PID file missing"
            FAILURES=$((FAILURES + 1))
            continue
        fi
        pid=$(cat "$pidfile")
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "[smoke] FAIL: $svc (pid $pid) not running"
            FAILURES=$((FAILURES + 1))
        else
            echo "[smoke] PASS: $svc (pid $pid) running"
        fi
    done

    echo ""
    echo "[smoke] Checking /web/chat/status for hosted chat + bridge..."
    SMOKE_STATUS=$(curl -sS --max-time 5 \
        "http://127.0.0.1:$WEB_PORT/web/chat/status?model=google/gemini-3.1-flash-lite" \
        -H "Authorization: Bearer $DEV_API_KEY" 2>/dev/null || echo '{}')
    CHAT_CONN=$(echo "$SMOKE_STATUS" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("1" if d.get("chat_connected") else "0")' 2>/dev/null || echo "0")
    BRIDGE_CONN=$(echo "$SMOKE_STATUS" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("1" if d.get("bridge_connected") else "0")' 2>/dev/null || echo "0")
    TRIAL_FLAG=$(echo "$SMOKE_STATUS" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("1" if d.get("trial") else "0")' 2>/dev/null || echo "0")
    if [ "$CHAT_CONN" = "1" ]; then
        echo "[smoke] PASS: chat_connected=true"
    else
        echo "[smoke] FAIL: chat_connected=false"
        FAILURES=$((FAILURES + 1))
    fi
    if [ "$BRIDGE_CONN" = "1" ]; then
        echo "[smoke] PASS: bridge_connected=true"
    else
        echo "[smoke] FAIL: bridge_connected=false"
        FAILURES=$((FAILURES + 1))
    fi
    if [ "$TRIAL_FLAG" = "1" ]; then
        echo "[smoke] PASS: trial=true (hosted lane active)"
    else
        echo "[smoke] FAIL: trial=false"
        FAILURES=$((FAILURES + 1))
    fi

    echo ""
    echo "[smoke] Stopping all services..."
    stop_servers
    sleep 1

    # Clean up smoke PIDDIR
    rm -rf "$PIDDIR" 2>/dev/null || true

    if [ "$FAILURES" -gt 0 ]; then
        echo "[smoke] FAILED: $FAILURES check(s) failed. See logs above."
        exit 1
    fi
    echo "[smoke] PASSED: all runtime checks OK (no provider calls made)."
    exit 0
fi
