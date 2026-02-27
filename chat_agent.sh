#!/bin/bash
# Start the local chat agent with WebSocket bridge to EC2.
#
# Usage:
#   ./chat_agent.sh
#
# Prerequisites:
#   - .env file in repo root with UNCHAINED_API_KEY
#   - Chrome bridge running (chrome_bridge.py)

set -euo pipefail

cd "$(dirname "$0")"
set -a
. ./.env
set +a
cd unchained

exec env PYTHONUNBUFFERED=1 uv run python chat_agent_cli.py 2>&1 | tee /tmp/chat_agent.log
