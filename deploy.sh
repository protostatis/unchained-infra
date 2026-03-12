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

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_PRIVATE_CORE_SCRIPT="$SCRIPT_DIR/tools/install_private_core.sh"
PRIVATE_CORE_SRC="${PRIVATE_CORE_SRC:-$SCRIPT_DIR/../unchained-core-private/unchained}"
PRIVATE_CORE_DST="$SCRIPT_DIR/unchained"

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

# Auto-install private core overlay when available.
if [[ -x "$INSTALL_PRIVATE_CORE_SCRIPT" && -d "$PRIVATE_CORE_SRC" ]]; then
    echo "==> Installing private core overlay..."
    "$INSTALL_PRIVATE_CORE_SCRIPT" "$PRIVATE_CORE_SRC" "$PRIVATE_CORE_DST"
else
    echo "==> Private core auto-install skipped (set PRIVATE_CORE_SRC if needed)."
fi

# Prevent shipping public stubs to production by mistake.
if grep -q "Run install_private_core.sh to overlay it." unchained/private_core_server.py 2>/dev/null; then
    echo "ERROR: private core stubs detected in unchained/private_core_server.py" >&2
    echo "Auto-install looked for private core at: $PRIVATE_CORE_SRC" >&2
    echo "Set PRIVATE_CORE_SRC or run ./tools/install_private_core.sh before deploy." >&2
    exit 1
fi

for f in \
    unchained/cdp.py \
    unchained/ddm.py \
    unchained/intel.py \
    unchained/private_core_engine.py \
    unchained/private_core_contracts.py \
    unchained/benchmark/progress_critic.py \
    unchained/benchmark/intermediate_goal.py
do
    if grep -q "Public stub for proprietary" "$f" 2>/dev/null; then
        echo "ERROR: private core stub detected in $f" >&2
        echo "Run ./tools/install_private_core.sh before deploy." >&2
        exit 1
    fi
done

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
    unchained/analytics.py \
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
    unchained/context_compact.py \
    unchained/scheduler_agent.py \
    unchained/scheduler_tool.py \
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

# Upload modular web_app package extracted from web.py
echo "==> Uploading web_app package..."
"${SSH_CMD[@]}" "mkdir -p $REMOTE_DIR/unchained/web_app/handlers"
"${SCP_CMD[@]}" -r \
    unchained/web_app/* \
    "$EC2_USER@$EC2_HOST:$REMOTE_DIR/unchained/web_app/"

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

# Refresh Caddy after upstream containers are recreated.
# This avoids stale/no-such-host upstream resolution windows after deploys.
echo "==> Restarting Caddy reverse proxy..."
"${SSH_CMD[@]}" "docker compose -f $REMOTE_DIR/docker-compose.yml restart caddy"

# Show status
echo ""
echo "==> Container status:"
"${SSH_CMD[@]}" "docker compose -f $REMOTE_DIR/docker-compose.yml ps"

echo ""
echo "==> Relay logs (last 5):"
"${SSH_CMD[@]}" "docker compose -f $REMOTE_DIR/docker-compose.yml logs relay --tail 5"

# Restore overlaid private-core files back to committed/public state.
echo ""
if [[ "${DEPLOY_RESTORE_WORKTREE:-1}" == "1" ]]; then
    echo "==> Restoring private-core overlay files..."
    OVERLAY_FILES=(
        "unchained/cdp.py"
        "unchained/ddm.py"
        "unchained/intel.py"
        "unchained/private_core_engine.py"
        "unchained/private_core_server.py"
        "unchained/private_core_contracts.py"
        "unchained/CLAUDE.md"
        "unchained/LABEL_RESOLUTION.md"
        "unchained/benchmark/progress_critic.py"
        "unchained/benchmark/intermediate_goal.py"
    )
    if git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        for rel in "${OVERLAY_FILES[@]}"; do
            if git -C "$SCRIPT_DIR" ls-files --error-unmatch "$rel" >/dev/null 2>&1; then
                git -C "$SCRIPT_DIR" restore --source=HEAD -- "$rel" >/dev/null 2>&1 \
                    || git -C "$SCRIPT_DIR" checkout -- "$rel" >/dev/null 2>&1 \
                    || true
            else
                rm -f "$SCRIPT_DIR/$rel"
            fi
        done
        echo "    Private-core overlay files restored."
    else
        echo "    (skipped — not in a git repo)"
    fi
else
    echo "==> Keeping overlaid private-core files (set DEPLOY_RESTORE_WORKTREE=1 to auto-restore)."
fi

echo ""
echo "==> Deploy complete!"
echo "    Relay:  ws://$EC2_HOST/tunnel"
echo "    MCP:    http://$EC2_HOST/mcp"
echo "    API:    http://$EC2_HOST/api/agents"
