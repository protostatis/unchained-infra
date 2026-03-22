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
cd "$SCRIPT_DIR"

source "$SCRIPT_DIR/deploy/runtime_context_files.sh"

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
"${SSH_CMD[@]}" "mkdir -p $REMOTE_DIR"
"${SCP_CMD[@]}" \
    "${TOP_LEVEL_CONTEXT_FILES[@]}" \
    "$EC2_USER@$EC2_HOST:$REMOTE_DIR/"

# Upload Python modules
echo "==> Uploading Python modules..."
"${SSH_CMD[@]}" "mkdir -p $REMOTE_DIR/unchained/benchmark"
UNCHAINED_UPLOAD_FILES=()
for rel in "${UNCHAINED_RUNTIME_FILES[@]}"; do
    UNCHAINED_UPLOAD_FILES+=("unchained/$rel")
done
"${SCP_CMD[@]}" "${UNCHAINED_UPLOAD_FILES[@]}" "$EC2_USER@$EC2_HOST:$REMOTE_DIR/unchained/"

BENCHMARK_UPLOAD_FILES=()
for rel in "${BENCHMARK_CONTEXT_FILES[@]}"; do
    BENCHMARK_UPLOAD_FILES+=("unchained/benchmark/$rel")
done
"${SCP_CMD[@]}" "${BENCHMARK_UPLOAD_FILES[@]}" "$EC2_USER@$EC2_HOST:$REMOTE_DIR/unchained/benchmark/"

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

# Upload vendored Research Desk package source for /web/research-desk/files.
echo "==> Uploading Research Desk vendor tree..."
for rel in "${RESEARCH_DESK_VENDOR_ROOT_FILES[@]}"; do
    if [[ ! -f "research_desk_vendor/$rel" ]]; then
        echo "ERROR: missing Research Desk vendor file: research_desk_vendor/$rel" >&2
        exit 1
    fi
done
shopt -s nullglob
RESEARCH_DESK_VENDOR_FILES=(research_desk_vendor/unchained_pyreplab/*.py)
shopt -u nullglob
if [[ "${#RESEARCH_DESK_VENDOR_FILES[@]}" -eq 0 ]]; then
    echo "ERROR: Research Desk vendor package has no Python files" >&2
    exit 1
fi
REMOTE_VENDOR_STAGE="$REMOTE_DIR/research_desk_vendor.stage.$$"
REMOTE_VENDOR_BACKUP="$REMOTE_DIR/research_desk_vendor.prev.$$"
"${SSH_CMD[@]}" "rm -rf '$REMOTE_VENDOR_STAGE' '$REMOTE_VENDOR_BACKUP' && mkdir -p '$REMOTE_VENDOR_STAGE/unchained_pyreplab'"
"${SCP_CMD[@]}" \
    research_desk_vendor/manifest.json \
    research_desk_vendor/README.md \
    research_desk_vendor/pyproject.toml \
    "$EC2_USER@$EC2_HOST:$REMOTE_VENDOR_STAGE/"
"${SCP_CMD[@]}" \
    "${RESEARCH_DESK_VENDOR_FILES[@]}" \
    "$EC2_USER@$EC2_HOST:$REMOTE_VENDOR_STAGE/unchained_pyreplab/"
"${SSH_CMD[@]}" "test -s '$REMOTE_VENDOR_STAGE/manifest.json' && test -s '$REMOTE_VENDOR_STAGE/README.md' && test -s '$REMOTE_VENDOR_STAGE/pyproject.toml' && find '$REMOTE_VENDOR_STAGE/unchained_pyreplab' -maxdepth 1 -type f -name '*.py' | grep -q ."
"${SSH_CMD[@]}" "rm -rf '$REMOTE_VENDOR_BACKUP' && if [ -d '$REMOTE_DIR/research_desk_vendor' ]; then mv '$REMOTE_DIR/research_desk_vendor' '$REMOTE_VENDOR_BACKUP'; fi && mv '$REMOTE_VENDOR_STAGE' '$REMOTE_DIR/research_desk_vendor' && rm -rf '$REMOTE_VENDOR_BACKUP'"

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
        "unchained/editable_helpers.js"
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
