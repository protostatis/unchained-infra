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
RHYTHM_SRC="${RHYTHM_SRC:-$SCRIPT_DIR/../rhythm}"

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
FORCE_FULL_DEPLOY=false
for arg in "$@"; do
    case "$arg" in
        --build)
            FORCE_BUILD=true
            ;;
        --full)
            FORCE_FULL_DEPLOY=true
            ;;
    esac
done

# Smart-deploy snapshot files. The remote snapshot is taken BEFORE any
# upload (but AFTER local prep — overlay + rhythm copy) so that diffing
# it against the post-upload local state yields the set of files this
# deploy actually changes. The list is fed to deploy/classify_changes.py
# to decide which services need a rebuild.
REMOTE_CHECKSUMS_FILE="$(mktemp -t uc_remote_checksums.XXXXXX)"
LOCAL_CHECKSUMS_FILE="$(mktemp -t uc_local_checksums.XXXXXX)"
DEPLOYED_PATHS_FILE="$(mktemp -t uc_deployed_paths.XXXXXX)"
trap 'rm -f "$REMOTE_CHECKSUMS_FILE" "$LOCAL_CHECKSUMS_FILE" "$DEPLOYED_PATHS_FILE"' EXIT

# Pick the local SHA-256 tool. macOS without GNU coreutils has `shasum`
# but not `sha256sum`; Linux has `sha256sum`. Both produce the same
# `<hash>  <path>` output format so they're interchangeable for diff.
# Remote (EC2 Linux) always has sha256sum — no detection needed there.
if command -v sha256sum >/dev/null 2>&1; then
    LOCAL_HASH_CMD="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
    LOCAL_HASH_CMD="shasum -a 256"
else
    echo "ERROR: need either sha256sum or shasum installed locally" >&2
    exit 1
fi

# Emit the relative paths of every file that this deploy will upload.
# Used to bound the checksum scan to files that actually exist on both
# sides so the diff doesn't catch local-only dev cruft (.venv, tests,
# benchmark data, etc.). Must be called AFTER overlay + rhythm copy so
# the local files are in their final pre-upload state.
emit_deployed_paths() {
    local f
    for f in "${TOP_LEVEL_CONTEXT_FILES[@]}"; do
        printf '%s\n' "$f"
    done
    for f in "${UNCHAINED_RUNTIME_FILES[@]}"; do
        printf '%s\n' "unchained/$f"
    done
    for f in "${BENCHMARK_CONTEXT_FILES[@]}"; do
        printf '%s\n' "unchained/benchmark/$f"
    done
    find unchained/web_app -type f \
        -not -path "*/__pycache__/*" -not -name "*.pyc" 2>/dev/null
    find unchained/installers -maxdepth 1 -type f 2>/dev/null
    for f in "${RESEARCH_DESK_VENDOR_ROOT_FILES[@]}"; do
        printf '%s\n' "research_desk_vendor/$f"
    done
    find research_desk_vendor/unchained_pyreplab -maxdepth 1 -type f -name "*.py" \
        2>/dev/null
    # NOTE: rhythm is intentionally excluded. The local rhythm copy step
    # produces a nested layout (rhythm/rhythm/__init__.py) while remote
    # SCP flattens it (rhythm/__init__.py), so path-based diff would
    # always show false positives. Use `--full` if you change rhythm.
}

echo "==> Deploying to $EC2_HOST"

if $FORCE_FULL_DEPLOY; then
    echo "    Mode: --full (skipping change classification, rebuilding everything)"
fi

# Auto-install private core overlay when available.
if [[ -x "$INSTALL_PRIVATE_CORE_SCRIPT" && -d "$PRIVATE_CORE_SRC" ]]; then
    echo "==> Installing private core overlay..."
    "$INSTALL_PRIVATE_CORE_SCRIPT" "$PRIVATE_CORE_SRC" "$PRIVATE_CORE_DST"
else
    echo "==> Private core auto-install skipped (set PRIVATE_CORE_SRC if needed)."
fi

# Copy rhythm repo into build context (for Dockerfile COPY rhythm/ rhythm/).
# Only the files needed at runtime are copied — not tests, data, or docs.
RHYTHM_DST="$SCRIPT_DIR/rhythm"
if [[ -d "$RHYTHM_SRC/rhythm" ]]; then
    echo "==> Copying rhythm into build context..."
    rm -rf "$RHYTHM_DST"
    mkdir -p "$RHYTHM_DST/rhythm"
    cp "$RHYTHM_SRC/rhythm/__init__.py" "$RHYTHM_DST/rhythm/"
    cp "$RHYTHM_SRC/rhythm/tools.py" "$RHYTHM_DST/rhythm/"
    for f in rhythm_binding.py schema_registry.py schema_db.py state_machine.py \
             catcher_agent.js prime_interceptor.js executor_binding.js; do
        cp "$RHYTHM_SRC/$f" "$RHYTHM_DST/"
    done
    echo "    Rhythm files copied."
else
    echo "==> Rhythm not found at $RHYTHM_SRC — skipping (tools will return 'not available')."
    mkdir -p "$RHYTHM_DST"
    touch "$RHYTHM_DST/.empty"
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

# Build the deployed-paths list now that overlay+rhythm prep is done.
# The list is reused for the remote snapshot below and the local snapshot
# after upload.
emit_deployed_paths | sort -u > "$DEPLOYED_PATHS_FILE"
DEPLOYED_PATHS_COUNT=$(wc -l < "$DEPLOYED_PATHS_FILE" | awk '{print $1}')

# Snapshot remote file checksums BEFORE upload so we can later detect
# what this deploy actually changed. Files in the deployed list that
# don't exist on the remote (new files) are silently skipped — they'll
# show up in the post-upload local snapshot but not here, which the
# diff treats as "added" → triggers correct service rebuild.
if ! $FORCE_FULL_DEPLOY; then
    echo "==> Snapshotting remote file checksums ($DEPLOYED_PATHS_COUNT paths)..."
    if "${SSH_CMD[@]}" "cd $REMOTE_DIR && xargs -r sha256sum 2>/dev/null | sort" \
            < "$DEPLOYED_PATHS_FILE" > "$REMOTE_CHECKSUMS_FILE"
    then
        echo "    $(wc -l < "$REMOTE_CHECKSUMS_FILE" | awk '{print $1}') file(s) hashed on remote."
    else
        echo "    (snapshot failed — falling back to full deploy)"
        FORCE_FULL_DEPLOY=true
    fi
fi

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

# Upload rhythm build-context directory
if [[ -d "$RHYTHM_DST/rhythm" ]]; then
    echo "==> Uploading rhythm..."
    remote_bash "$REMOTE_DIR" <<EOF
set -euo pipefail
[[ -n "$REMOTE_DIR" ]] || { echo 'REMOTE_DIR is empty — aborting'; exit 1; }
rm -rf "$REMOTE_DIR/rhythm"
mkdir -p "$REMOTE_DIR/rhythm"
EOF
    "${SCP_CMD[@]}" -r "$RHYTHM_DST/rhythm" "$EC2_USER@$EC2_HOST:$REMOTE_DIR/"
    shopt -s nullglob
    RHYTHM_TOP_FILES=("$RHYTHM_DST"/*.py "$RHYTHM_DST"/*.js)
    shopt -u nullglob
    if [[ "${#RHYTHM_TOP_FILES[@]}" -gt 0 ]]; then
        "${SCP_CMD[@]}" "${RHYTHM_TOP_FILES[@]}" "$EC2_USER@$EC2_HOST:$REMOTE_DIR/rhythm/"
    fi
else
    echo "==> Rhythm not found locally — keeping existing remote rhythm."
fi

# Verify the docker-compose service list hasn't changed since this script
# was written. If services are added/removed, the classifier needs updating.
echo "==> Verifying service list matches docker-compose.yml..."
remote_bash "$REMOTE_DIR" <<'EOF'
set -euo pipefail
cd "$1"
actual=$(docker compose config --services | sort)
expected=$(printf '%s\n' caddy mcp private-core relay scheduler trial-agent unbrowser-egress unbrowser-mcp web)
if [ "$actual" != "$expected" ]; then
    diff <(echo "$expected") <(echo "$actual") >&2 || true
    echo "ERROR: docker-compose.yml services changed — update deploy/classify_changes.py and deploy.sh" >&2
    exit 1
fi
EOF

# Compute the diff between this deploy and the previous one. The remote
# snapshot was taken before upload; uploads have now run, so any file
# whose hash differs from the snapshot is something this deploy changed.
SERVICES_TO_REBUILD=""
RECREATE_CADDY=false
if $FORCE_FULL_DEPLOY; then
    SERVICES_TO_REBUILD="caddy mcp private-core relay scheduler trial-agent unbrowser-egress unbrowser-mcp web"
    RECREATE_CADDY=true
else
    echo "==> Computing changed files..."
    if (cd "$SCRIPT_DIR" && xargs -r $LOCAL_HASH_CMD 2>/dev/null < "$DEPLOYED_PATHS_FILE" | sort) > "$LOCAL_CHECKSUMS_FILE"; then
        # diff outputs lines like:
        #   < <hash>  <path>          (only on remote — file was deleted)
        #   > <hash>  <path>          (only on local — file is new)
        #   < <oldhash> <path>        (changed: appears with both < and >)
        #   > <newhash> <path>
        # We want unique paths from < and > lines.
        # `|| true` because diff exits 1 when files differ (the normal case),
        # which would otherwise trip set -e + pipefail.
        CHANGED_FILES=$(diff "$REMOTE_CHECKSUMS_FILE" "$LOCAL_CHECKSUMS_FILE" \
            | awk '/^[<>]/ {print $3}' | sort -u || true)

        if [[ -z "$CHANGED_FILES" ]]; then
            echo "    No file changes detected. Nothing to rebuild."
        else
            CHANGED_COUNT=$(echo "$CHANGED_FILES" | wc -l | awk '{print $1}')
            echo "    $CHANGED_COUNT file(s) changed:"
            echo "$CHANGED_FILES" | head -10 | sed 's/^/      /'
            if [[ "$CHANGED_COUNT" -gt 10 ]]; then
                echo "      ... and $((CHANGED_COUNT - 10)) more"
            fi

            # Run the classifier
            CLASSIFIER_OUTPUT=$(echo "$CHANGED_FILES" \
                | python3 "$SCRIPT_DIR/deploy/classify_changes.py" 2>&1)

            if echo "$CLASSIFIER_OUTPUT" | grep -qx ALL; then
                echo "    Classifier: ALL services need rebuild"
                SERVICES_TO_REBUILD="caddy mcp private-core relay scheduler trial-agent unbrowser-egress unbrowser-mcp web"
                RECREATE_CADDY=true
            else
                SERVICES_TO_REBUILD=$(echo "$CLASSIFIER_OUTPUT" | tr '\n' ' ' | sed 's/ *$//')
                if [[ -z "$SERVICES_TO_REBUILD" ]]; then
                    echo "    Classifier: no services need rebuild (docs/test changes only)"
                else
                    echo "    Classifier: rebuilding [$SERVICES_TO_REBUILD]"
                fi
            fi
        fi
    else
        echo "    (local checksum failed — falling back to full deploy)"
        SERVICES_TO_REBUILD="caddy mcp private-core relay scheduler trial-agent unbrowser-egress unbrowser-mcp web"
        RECREATE_CADDY=true
    fi
    cd "$SCRIPT_DIR"
fi

# Build affected services. caddy uses a public image (no build context)
# so we filter it out of the build list. `|| true` guards against grep -v
# exiting 1 when SERVICES_TO_REBUILD is empty (no matches → grep returns 1).
BUILD_SERVICES=$(echo "$SERVICES_TO_REBUILD" | tr ' ' '\n' | grep -v '^$' | grep -v '^caddy$' | tr '\n' ' ' | sed 's/ *$//' || true)

if [[ -n "$BUILD_SERVICES" ]]; then
    echo "==> Building images for: $BUILD_SERVICES"
    if $FORCE_BUILD; then
        remote_bash "$REMOTE_DIR" "$BUILD_SERVICES" <<'EOF'
set -euo pipefail
cd "$1"
read -r -a build_services <<< "$2"
docker compose build --no-cache "${build_services[@]}"
EOF
    else
        remote_bash "$REMOTE_DIR" "$BUILD_SERVICES" <<'EOF'
set -euo pipefail
cd "$1"
read -r -a build_services <<< "$2"
docker compose build "${build_services[@]}"
EOF
    fi
else
    echo "==> No images to build."
fi

# Recreate affected service containers. We use --no-deps --no-build so:
#   --no-deps: dependent services (e.g. scheduler depends on web) are NOT
#              recreated unless they're in our affected list. Their existing
#              connections survive and they'll auto-reconnect if needed.
#   --no-build: skip the build step we already did above.
#
# Each service recreate is a ~3-5s window for that service only; other
# services keep serving the entire time.
RESTART_SERVICES=$(echo "$SERVICES_TO_REBUILD" | tr ' ' '\n' | grep -v '^$' | grep -v '^caddy$' | tr '\n' ' ' | sed 's/ *$//' || true)

if [[ -n "$RESTART_SERVICES" ]]; then
    echo "==> Recreating containers: $RESTART_SERVICES"
    remote_bash "$REMOTE_DIR" "$RESTART_SERVICES" <<'EOF'
set -euo pipefail
cd "$1"
read -r -a restart_services <<< "$2"
docker compose up -d --no-deps --no-build "${restart_services[@]}"
EOF
else
    echo "==> No service containers to recreate."
fi

# Caddy: prefer graceful reload over restart. caddy reload re-reads the
# Caddyfile and updates upstream resolutions in-place with zero downtime.
# We only touch caddy if it's in the affected services list (i.e. the
# Caddyfile changed) or if we're doing a full deploy.
if echo " $SERVICES_TO_REBUILD " | grep -q ' caddy '; then
    if $RECREATE_CADDY; then
        echo "==> Recreating Caddy container..."
        remote_bash "$REMOTE_DIR" <<'EOF'
set -euo pipefail
cd "$1"
docker compose up -d --no-deps --no-build caddy
EOF
    else
        echo "==> Reloading Caddy (graceful, no downtime)..."
        remote_bash "$REMOTE_DIR" <<'EOF' || true
set -euo pipefail
cd "$1"
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile \
    || docker compose restart caddy
EOF
    fi
fi

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
    # Clean up rhythm build context copy
    rm -rf "$SCRIPT_DIR/rhythm"
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

# Prune stale docker images and build cache on the remote to prevent the
# 10GB root partition from filling up between deploys. Images older than
# a week that aren't referenced by any running container get freed. This
# is a belt-and-suspenders hedge against the disk-full build failure we
# hit on 2026-04-11 when docker had accumulated 181 images / 3.7 GB of
# stale layers. Runs AFTER the successful deploy so nothing the new
# containers depend on is touched. Errors are swallowed — pruning is
# best-effort, we don't want a prune failure to fail the deploy.
if [[ "${DEPLOY_PRUNE_OLD_IMAGES:-1}" == "1" ]]; then
    echo "==> Pruning old docker images + build cache (older than 7 days)..."
    remote_bash "$REMOTE_DIR" <<'EOF' || echo "    (prune failed, ignoring)"
set +e
docker image prune -f --filter "until=168h" 2>&1 | tail -1
docker builder prune -f --filter "until=168h" 2>&1 | tail -1
EOF
fi

echo ""
echo "==> Deploy complete!"
echo "    Relay:  ws://$EC2_HOST/tunnel"
echo "    MCP:    http://$EC2_HOST/mcp"
echo "    API:    http://$EC2_HOST/api/agents"
