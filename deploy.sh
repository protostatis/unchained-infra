#!/bin/bash
# Deploy unchained to EC2 production instance.
#
# Usage:
#   ./deploy.sh                    # Deploy with defaults
#   ./deploy.sh --build            # Force rebuild containers
#   EC2_HOST=1.2.3.4 ./deploy.sh   # Override host
#
# Prerequisites:
#   - SSH agent access or KEY_PATH pointing to your SSH private key
#   - EC2 instance running with Docker

set -euo pipefail

EC2_HOST="${EC2_HOST:?EC2_HOST env var is required (e.g. EC2_HOST=1.2.3.4 ./deploy.sh)}"
EC2_USER="${EC2_USER:-ec2-user}"
REMOTE_DIR="/home/$EC2_USER/unchained"
SSH_OPTS=()
if [[ -n "${KEY_PATH:-}" ]]; then
    SSH_OPTS+=(-i "$KEY_PATH")
fi
# Note: accept the host key manually first time with: ssh "${SSH_OPTS[@]}" "$EC2_USER@$EC2_HOST"
SSH_CMD=(ssh "${SSH_OPTS[@]}" "$EC2_USER@$EC2_HOST")
SCP_CMD=(scp "${SSH_OPTS[@]}")

FORCE_BUILD=false
if [[ "${1:-}" == "--build" ]]; then
    FORCE_BUILD=true
fi

echo "==> Deploying to $EC2_HOST"

# Upload top-level files
echo "==> Uploading config files..."
"${SCP_CMD[@]}" \
    Dockerfile \
    docker-compose.yml \
    Caddyfile \
    "$EC2_USER@$EC2_HOST:$REMOTE_DIR/"

# Upload Python modules
echo "==> Uploading Python modules..."
"${SSH_CMD[@]}" "mkdir -p $REMOTE_DIR/unchained/benchmark"
"${SCP_CMD[@]}" \
    unchained/relay.py \
    unchained/rate_limit.py \
    unchained/auth.py \
    unchained/cloud_tools.py \
    unchained/private_core_client.py \
    unchained/private_core_contracts.py \
    unchained/private_core_engine.py \
    unchained/private_core_server.py \
    unchained/api.py \
    unchained/mcp_server.py \
    unchained/orchestrator.py \
    unchained/cdp.py \
    unchained/ddm.py \
    unchained/intel.py \
    unchained/web.py \
    unchained/web_cmd.py \
    unchained/web_state.py \
    unchained/analytics.py \
    unchained/provision_helpers.py \
    unchained/template_utils.py \
    unchained/agent_package.py \
    unchained/chrome_bridge.py \
    unchained/chat_agent_cli.py \
    unchained/chat_agent_openrouter.py \
    unchained/chat_agent_gemini.py \
    unchained/chat_agent_codex.py \
    unchained/chat_agent_sdk.py \
    unchained/signup_agent.py \
    unchained/nudge.py \
    unchained/reflex.py \
    unchained/pyproject.toml \
    unchained/CLAUDE.md \
    unchained/scheduled_tasks.py \
    unchained/scheduled_jobs.example.json \
    unchained/favicon.svg \
    "$EC2_USER@$EC2_HOST:$REMOTE_DIR/unchained/"

"${SCP_CMD[@]}" \
    unchained/benchmark/__init__.py \
    unchained/benchmark/progress_critic.py \
    unchained/benchmark/intermediate_goal.py \
    "$EC2_USER@$EC2_HOST:$REMOTE_DIR/unchained/benchmark/"

# Upload native installer assets
echo "==> Uploading installer assets..."
"${SSH_CMD[@]}" "mkdir -p $REMOTE_DIR/unchained/installers"
shopt -s nullglob
INSTALLER_FILES=(unchained/installers/*)
shopt -u nullglob
if [[ "${#INSTALLER_FILES[@]}" -gt 0 ]]; then
    "${SCP_CMD[@]}" \
        "${INSTALLER_FILES[@]}" \
        "$EC2_USER@$EC2_HOST:$REMOTE_DIR/unchained/installers/"
fi

# Rebuild and restart
echo "==> Rebuilding and restarting containers..."
if $FORCE_BUILD; then
    "${SSH_CMD[@]}" "cd $REMOTE_DIR && docker compose build --no-cache && docker compose up -d"
else
    "${SSH_CMD[@]}" "cd $REMOTE_DIR && docker compose up -d --build"
fi

# Show status
echo ""
echo "==> Container status:"
"${SSH_CMD[@]}" "docker compose -f $REMOTE_DIR/docker-compose.yml ps"

echo ""
echo "==> Relay logs (last 5):"
"${SSH_CMD[@]}" "docker compose -f $REMOTE_DIR/docker-compose.yml logs relay --tail 5"

# Restore any overlaid files back to their committed state
echo ""
echo "==> Cleaning working tree..."
git -C "$(dirname "$0")" checkout -- unchained/ 2>/dev/null \
    && echo "    Working tree restored." \
    || echo "    (skipped — not in a git repo or no changes)"

echo ""
echo "==> Deploy complete!"
echo "    Relay:  ws://$EC2_HOST/tunnel"
echo "    MCP:    http://$EC2_HOST/mcp"
echo "    API:    http://$EC2_HOST/api/agents"
