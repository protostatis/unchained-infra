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

remote_bash() {
    "${SSH_CMD[@]}" bash -s -- "$@"
}

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
remote_bash "$REMOTE_DIR" <<'EOF'
set -euo pipefail
remote_dir="$1"
mkdir -p "$remote_dir"
EOF
"${SCP_CMD[@]}" \
    "${TOP_LEVEL_CONTEXT_FILES[@]}" \
    "$EC2_USER@$EC2_HOST:$REMOTE_DIR/"

# Upload Python modules
echo "==> Uploading Python modules..."
remote_bash "$REMOTE_DIR" <<'EOF'
set -euo pipefail
remote_dir="$1"
mkdir -p "$remote_dir/unchained/benchmark"
EOF
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
remote_bash "$REMOTE_DIR" <<'EOF'
set -euo pipefail
remote_dir="$1"
mkdir -p "$remote_dir/unchained/web_app/handlers"
EOF
"${SCP_CMD[@]}" -r \
    unchained/web_app/* \
    "$EC2_USER@$EC2_HOST:$REMOTE_DIR/unchained/web_app/"

# Upload native installer assets
echo "==> Uploading installer assets..."
remote_bash "$REMOTE_DIR" <<'EOF'
set -euo pipefail
remote_dir="$1"
mkdir -p "$remote_dir/unchained/installers"
EOF
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
RESEARCH_DESK_VENDOR_ROOT_UPLOAD_FILES=()
for rel in "${RESEARCH_DESK_VENDOR_ROOT_FILES[@]}"; do
    vendor_path="research_desk_vendor/$rel"
    if [[ ! -f "$vendor_path" ]]; then
        echo "ERROR: missing Research Desk vendor file: $vendor_path" >&2
        exit 1
    fi
    RESEARCH_DESK_VENDOR_ROOT_UPLOAD_FILES+=("$vendor_path")
done
shopt -s nullglob
RESEARCH_DESK_VENDOR_FILES=(research_desk_vendor/unchained_pyreplab/*.py)
shopt -u nullglob
if [[ "${#RESEARCH_DESK_VENDOR_FILES[@]}" -eq 0 ]]; then
    echo "ERROR: Research Desk vendor package has no Python files" >&2
    exit 1
fi
REMOTE_VENDOR_STAGE="$(
    remote_bash "$REMOTE_DIR" <<'EOF'
set -euo pipefail
remote_dir="$1"
stage_dir="$(mktemp -d "$remote_dir/research_desk_vendor.stage.XXXXXX")"
mkdir -p "$stage_dir/unchained_pyreplab"
printf '%s\n' "$stage_dir"
EOF
)"
"${SCP_CMD[@]}" \
    "${RESEARCH_DESK_VENDOR_ROOT_UPLOAD_FILES[@]}" \
    "$EC2_USER@$EC2_HOST:$REMOTE_VENDOR_STAGE/"
"${SCP_CMD[@]}" \
    "${RESEARCH_DESK_VENDOR_FILES[@]}" \
    "$EC2_USER@$EC2_HOST:$REMOTE_VENDOR_STAGE/unchained_pyreplab/"
remote_bash "$REMOTE_VENDOR_STAGE" <<'EOF'
set -euo pipefail
stage_dir="$1"
test -s "$stage_dir/manifest.json"
test -s "$stage_dir/README.md"
test -s "$stage_dir/pyproject.toml"
test -s "$stage_dir/setup.py"
find "$stage_dir/unchained_pyreplab" -maxdepth 1 -type f -name '*.py' | grep -q .
EOF
remote_bash "$REMOTE_DIR" "$REMOTE_VENDOR_STAGE" <<'EOF'
set -euo pipefail
remote_dir="$1"
stage_dir="$2"
live_dir="$remote_dir/research_desk_vendor"
backup_dir="$(mktemp -d "$remote_dir/research_desk_vendor.prev.XXXXXX")"
rmdir "$backup_dir"
restore_live_dir() {
    if [[ ! -e "$live_dir" && -e "$backup_dir" ]]; then
        mv "$backup_dir" "$live_dir"
    fi
    rm -rf "$stage_dir"
}
trap restore_live_dir EXIT
if [[ -e "$live_dir" ]]; then
    mv "$live_dir" "$backup_dir"
fi
mv "$stage_dir" "$live_dir"
rm -rf "$backup_dir"
trap - EXIT
EOF

# Rebuild images (without restarting containers yet)
echo "==> Building container images..."
if $FORCE_BUILD; then
    remote_bash "$REMOTE_DIR" <<'EOF'
set -euo pipefail
cd "$1"
docker compose build --no-cache
EOF
else
    remote_bash "$REMOTE_DIR" <<'EOF'
set -euo pipefail
cd "$1"
docker compose build
EOF
fi

# Staged restart: bring up backend services first, then MCP last to minimize
# its downtime. Health checks in docker-compose.yml ensure each layer is ready
# before the next starts.
echo "==> Restarting backend services (relay, private-core)..."
remote_bash "$REMOTE_DIR" <<'EOF'
set -euo pipefail
cd "$1"
docker compose up -d --no-deps relay private-core
EOF

echo "==> Waiting for backend health checks..."
remote_bash "$REMOTE_DIR" <<'HEALTHEOF'
set -euo pipefail
cd "$1"
# Wait up to 30s for relay and private-core to be healthy
for svc in relay private-core; do
    for i in $(seq 1 30); do
        cid=$(docker compose ps -q "$svc" 2>/dev/null | head -1)
        if [ -z "$cid" ]; then sleep 1; continue; fi
        status=$(docker inspect --format='{{.State.Health.Status}}' "$cid" 2>/dev/null || echo "missing")
        if [ "$status" = "healthy" ]; then
            echo "    $svc is healthy"
            break
        fi
        if [ "$i" -eq 30 ]; then
            echo "WARNING: $svc did not become healthy in 30s (status: $status), continuing anyway"
            break
        fi
        sleep 1
    done
done
HEALTHEOF

# Staged service lists — update if docker-compose.yml adds new services.
# The assertion below will fail loudly if they drift.
STAGED_SERVICES="caddy mcp private-core relay scheduler trial-agent web"
echo "==> Verifying service list matches docker-compose.yml..."
remote_bash "$REMOTE_DIR" "$STAGED_SERVICES" <<'EOF'
set -euo pipefail
cd "$1"
actual=$(docker compose config --services | sort | tr '\n' ' ' | sed 's/ $//')
expected="$2"
if [ "$actual" != "$expected" ]; then
    echo "ERROR: docker-compose.yml services ($actual) differ from deploy.sh ($expected)" >&2
    echo "Update STAGED_SERVICES in deploy.sh to match." >&2
    exit 1
fi
EOF

echo "==> Restarting remaining services (web, mcp, scheduler, trial-agent)..."
remote_bash "$REMOTE_DIR" <<'EOF'
set -euo pipefail
cd "$1"
docker compose up -d --no-deps web mcp scheduler trial-agent
EOF

echo "==> Waiting for MCP health check..."
remote_bash "$REMOTE_DIR" <<'HEALTHEOF'
set -euo pipefail
cd "$1"
for i in $(seq 1 30); do
    cid=$(docker compose ps -q mcp 2>/dev/null | head -1)
    if [ -z "$cid" ]; then sleep 1; continue; fi
    status=$(docker inspect --format='{{.State.Health.Status}}' "$cid" 2>/dev/null || echo "missing")
    if [ "$status" = "healthy" ]; then
        echo "    MCP is healthy"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "WARNING: MCP did not become healthy in 30s (status: $status)"
        break
    fi
    sleep 1
done
HEALTHEOF

# Refresh Caddy after upstream containers are recreated.
# This avoids stale/no-such-host upstream resolution windows after deploys.
echo "==> Restarting Caddy reverse proxy..."
remote_bash "$REMOTE_DIR" <<'EOF'
set -euo pipefail
cd "$1"
docker compose restart caddy
EOF

# Show status
echo ""
echo "==> Container status:"
remote_bash "$REMOTE_DIR" <<'EOF'
set -euo pipefail
remote_dir="$1"
docker compose -f "$remote_dir/docker-compose.yml" ps
EOF

echo ""
echo "==> Relay logs (last 5):"
remote_bash "$REMOTE_DIR" <<'EOF'
set -euo pipefail
remote_dir="$1"
docker compose -f "$remote_dir/docker-compose.yml" logs relay --tail 5
EOF

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
