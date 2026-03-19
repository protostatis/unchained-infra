#!/usr/bin/env bash
# bench.sh — Run benchmark against local relay with parallel tabs
#
# Usage:
#   ./bench.sh                      # Full suite, haiku, 4 parallel tabs
#   ./bench.sh --cli-model sonnet   # Use sonnet
#   ./bench.sh --subset hn_top5     # Single task
#   ./bench.sh --parallel-tasks 8   # 8 parallel tabs
#   ./bench.sh --site hackernews    # Filter by site
#
# Prerequisites:
#   - Local relay running on :8765
#   - bench-user API key in auth.db

set -euo pipefail
cd "$(dirname "$0")"

# --- Config ---
RELAY_HOST=127.0.0.1
RELAY_PORT=8765
BENCH_PROFILE=bench_haiku
BENCH_CDP_PORT=9344
BENCH_API_KEY=uc_live_c699960ed33614b85b3daacc
PARALLEL=${PARALLEL:-8}

# --- Ensure relay is running ---
if ! lsof -i :${RELAY_PORT} -sTCP:LISTEN >/dev/null 2>&1; then
    echo "ERROR: No relay on port ${RELAY_PORT}. Start with:"
    echo "  uv run python relay.py --port ${RELAY_PORT}"
    exit 1
fi

# --- Ensure bridge is running ---
BRIDGE_PID=$(pgrep -f "chrome_bridge.py.*${BENCH_PROFILE}" 2>/dev/null || true)
if [ -z "$BRIDGE_PID" ]; then
    echo "Starting bridge (profile=${BENCH_PROFILE}, port=${BENCH_CDP_PORT})..."
    uv run python chrome_bridge.py start \
        --relay "ws://${RELAY_HOST}:${RELAY_PORT}/tunnel" \
        --key "${BENCH_API_KEY}" \
        --profile "${BENCH_PROFILE}" \
        --port "${BENCH_CDP_PORT}" &
    BRIDGE_BG_PID=$!
    sleep 5
    echo "Bridge started (PID ${BRIDGE_BG_PID})"
fi

# --- Discover agent ID ---
AGENT_ID=$(curl -s -H "Authorization: Bearer ${BENCH_API_KEY}" \
    "http://${RELAY_HOST}:${RELAY_PORT}/api/agents" 2>/dev/null \
    | python3 -c "
import json, sys
agents = json.load(sys.stdin)
for a in agents:
    if '${BENCH_PROFILE}' in a.get('agent_id',''):
        print(a['agent_id'])
        break
" 2>/dev/null || true)

if [ -z "$AGENT_ID" ]; then
    echo "ERROR: Could not find agent with profile ${BENCH_PROFILE}"
    echo "Check bridge logs and ensure it connected to the relay."
    exit 1
fi
echo "Agent: ${AGENT_ID}"

# --- Run benchmark ---
echo "Running benchmark (parallel=${PARALLEL})..."
echo "============================================"

CDP_AGENT_ID="${AGENT_ID}" \
CDP_RELAY_HOST="${RELAY_HOST}" \
CDP_RELAY_PORT="${RELAY_PORT}" \
CDP_API_KEY="${BENCH_API_KEY}" \
uv run python -m benchmark.runner \
    --agents cli \
    --cli-model haiku \
    --parallel-tasks "${PARALLEL}" \
    "$@"
