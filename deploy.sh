#!/bin/bash
# Deploy unchained to EC2 production instance.
#
# Usage:
#   DEPLOY_REVISION=<current-main-sha> EC2_HOST=1.2.3.4 ./deploy.sh
#   DEPLOY_REVISION=<current-main-sha> EC2_HOST=1.2.3.4 ./deploy.sh --build
#
# Prerequisites:
#   - A clean worktree at the freshly fetched origin/main revision
#   - SSH agent access or KEY_PATH pointing to your SSH private key
#   - EC2 instance running with Docker

set -euo pipefail

# GitHub injects these only into the approved production deployment job. Copy
# them into non-exported shell variables, then remove the inherited names before
# invoking any unrelated child process. They are later streamed to the protected
# remote staging directory through verified SSH stdin, never arguments or files.
TURNSTILE_SITE_KEY_INPUT="${FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY-}"
TURNSTILE_SECRET_INPUT="${FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET-}"
export -n TURNSTILE_SITE_KEY_INPUT TURNSTILE_SECRET_INPUT
unset FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET
if [[ ( -z "$TURNSTILE_SITE_KEY_INPUT" && -n "$TURNSTILE_SECRET_INPUT" ) \
    || ( -n "$TURNSTILE_SITE_KEY_INPUT" && -z "$TURNSTILE_SECRET_INPUT" ) ]]; then
    echo "ERROR: both FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY and FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET must be provided together" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

source "$SCRIPT_DIR/deploy/runtime_context_files.sh"
source "$SCRIPT_DIR/deploy/deploy_source_guard.sh"

DEPLOY_REVISION="${DEPLOY_REVISION-}"
verify_deploy_source "$SCRIPT_DIR" "$DEPLOY_REVISION"

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
if [[ -n "${DEPLOY_SSH_KNOWN_HOSTS_FILE:-}" ]]; then
    if [[ ! -s "$DEPLOY_SSH_KNOWN_HOSTS_FILE" ]]; then
        echo "ERROR: DEPLOY_SSH_KNOWN_HOSTS_FILE is missing or empty" >&2
        exit 1
    fi
    SSH_OPTS+=(
        -o "UserKnownHostsFile=$DEPLOY_SSH_KNOWN_HOSTS_FILE"
        -o StrictHostKeyChecking=yes
    )
fi
# Note: accept the host key manually first time with: ssh "${SSH_OPTS[@]}" "$EC2_USER@$EC2_HOST"
SSH_CMD=(ssh "${SSH_OPTS[@]}" "$EC2_USER@$EC2_HOST")
SCP_CMD=(scp "${SSH_OPTS[@]}")

remote_bash() {
    # ssh joins command arguments into one remote shell command. Quote each
    # script argument explicitly so a value such as the space-separated
    # service list remains one positional argument on the remote host.
    local remote_command="bash -s --"
    local arg quoted_arg
    for arg in "$@"; do
        printf -v quoted_arg '%q' "$arg"
        remote_command+=" $quoted_arg"
    done
    "${SSH_CMD[@]}" "$remote_command"
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
DEPLOY_LOCK_WORK_DIR=""
DEPLOY_LOCK_FIFO=""
DEPLOY_LOCK_STATUS_FILE=""
DEPLOY_LOCK_SSH_PID=""
DEPLOY_LOCK_HELD=false
DEPLOY_LOCK_TOKEN=""
DEPLOY_BACKUP_READY=false
DEPLOY_MUTATED=false
DEPLOY_SUCCEEDED=false
DEPLOY_ROLLBACK_ATTEMPTED=false
DEPLOY_OVERLAY_APPLIED=false
DEPLOY_ROLLBACK_ON_FAILURE="${DEPLOY_ROLLBACK_ON_FAILURE:-1}"
DEPLOY_ID="${DEPLOY_ID:-$(python3 -c 'import secrets; print(secrets.token_hex(12))')}"
if [[ ! "$DEPLOY_ID" =~ ^[0-9a-f]{24}$ ]]; then
    echo "ERROR: DEPLOY_ID must be a 24-character lowercase hexadecimal value" >&2
    exit 1
fi
REMOTE_BACKUP_DIR="$REMOTE_DIR/.deploy-backups/$DEPLOY_ID"
REMOTE_DEPLOY_TOOLS_DIR="$REMOTE_DIR/.deploy-tools"
COMPOSE_DIFF_TOOL="$SCRIPT_DIR/deploy/compose_service_diff.py"
FIN_TERMINAL_SECRETS_TOOL="$SCRIPT_DIR/deploy/ensure_fin_terminal_secrets.py"
CADDY_CONFIG_PREFLIGHT_TOOL="$SCRIPT_DIR/deploy/caddy_config_preflight.sh"
REMOTE_CONFIG_STAGE="$REMOTE_DIR/.deploy-staging/$DEPLOY_ID"
REMOTE_CONFIG_STAGE_ACTIVE=false
FIN_TERMINAL_SECRETS_CHANGED=false
ALL_RUNTIME_SERVICES="relay private-core mcp unbrowser-egress unbrowser-mcp fin-terminal web scheduler trial-agent"
ALL_SERVICES="caddy $ALL_RUNTIME_SERVICES"
IFS= read -r CADDY_SITE_LINE < "$SCRIPT_DIR/Caddyfile"
DEFAULT_DEPLOY_HEALTH_HOST="${CADDY_SITE_LINE%%,*}"
DEPLOY_HEALTH_HOST="${DEPLOY_HEALTH_HOST:-$DEFAULT_DEPLOY_HEALTH_HOST}"
FIN_TERMINAL_PUBLIC_HOST="${FIN_TERMINAL_PUBLIC_HOST:-unbrowser.unchainedsky.com}"

if [[ ! "$DEPLOY_HEALTH_HOST" =~ ^[A-Za-z0-9.-]+$ ]]; then
    echo "ERROR: DEPLOY_HEALTH_HOST must be a hostname" >&2
    exit 1
fi
if [[ ! "$FIN_TERMINAL_PUBLIC_HOST" =~ ^[A-Za-z0-9.-]+$ ]]; then
    echo "ERROR: FIN_TERMINAL_PUBLIC_HOST must be a hostname" >&2
    exit 1
fi

restore_private_core_worktree() {
    if [[ "$DEPLOY_OVERLAY_APPLIED" != "true" ]]; then
        return
    fi
    if [[ "${DEPLOY_RESTORE_WORKTREE:-1}" != "1" ]]; then
        echo "==> Keeping overlaid private-core files (set DEPLOY_RESTORE_WORKTREE=1 to auto-restore)."
        return
    fi

    rm -rf "$SCRIPT_DIR/rhythm"
    echo "==> Restoring private-core overlay files..."
    local overlay_files=(
        "unchained/cdp.py"
        "unchained/ddm.py"
        "unchained/intel.py"
        "unchained/editable_helpers.js"
        "unchained/private_core_engine.py"
        "unchained/private_core_server.py"
        "unchained/private_core_contracts.py"
        "unchained/challenge_detection.py"
        "unchained/domain_policy.py"
        "unchained/CLAUDE.md"
        "unchained/LABEL_RESOLUTION.md"
        "unchained/benchmark/progress_critic.py"
        "unchained/benchmark/intermediate_goal.py"
    )
    if git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        local rel
        for rel in "${overlay_files[@]}"; do
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
}

release_remote_deploy_lock() {
    if [[ -n "$DEPLOY_LOCK_SSH_PID" ]]; then
        exec 9>&- || true
        wait "$DEPLOY_LOCK_SSH_PID" 2>/dev/null || true
    fi
    DEPLOY_LOCK_HELD=false
    DEPLOY_LOCK_SSH_PID=""
    if [[ -n "$DEPLOY_LOCK_WORK_DIR" ]]; then
        rm -rf "$DEPLOY_LOCK_WORK_DIR"
    fi
    DEPLOY_LOCK_WORK_DIR=""
    DEPLOY_LOCK_FIFO=""
    DEPLOY_LOCK_STATUS_FILE=""
}

acquire_remote_deploy_lock() {
    local remote_script quoted_script quoted_lock_file quoted_token
    DEPLOY_LOCK_WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/uc-deploy-lock.XXXXXX")"
    DEPLOY_LOCK_FIFO="$DEPLOY_LOCK_WORK_DIR/input"
    DEPLOY_LOCK_STATUS_FILE="$DEPLOY_LOCK_WORK_DIR/status"
    DEPLOY_LOCK_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
    mkfifo "$DEPLOY_LOCK_FIFO"

    remote_script='set -euo pipefail
lock_file="$1"
token="$2"
mkdir -p "$(dirname "$lock_file")"
exec 9>>"$lock_file"
if ! flock -n 9; then
  echo "deployment lock is already held" >&2
  exit 75
fi
printf "%s\\n" "$token"
cat >/dev/null'
    printf -v quoted_script '%q' "$remote_script"
    printf -v quoted_lock_file '%q' "$REMOTE_DIR/.deploy.lock"
    printf -v quoted_token '%q' "$DEPLOY_LOCK_TOKEN"
    "${SSH_CMD[@]}" "bash -c $quoted_script -- $quoted_lock_file $quoted_token" \
        < "$DEPLOY_LOCK_FIFO" > "$DEPLOY_LOCK_STATUS_FILE" 2>&1 &
    DEPLOY_LOCK_SSH_PID=$!
    # Keep the remote stdin open. Its `flock` is released automatically if this
    # process dies or this descriptor is closed in release_remote_deploy_lock.
    exec 9>"$DEPLOY_LOCK_FIFO"

    local attempt lock_status=""
    for attempt in $(seq 1 100); do
        if [[ -s "$DEPLOY_LOCK_STATUS_FILE" ]]; then
            IFS= read -r lock_status < "$DEPLOY_LOCK_STATUS_FILE" || true
            if [[ "$lock_status" == "$DEPLOY_LOCK_TOKEN" ]]; then
                DEPLOY_LOCK_HELD=true
                return 0
            fi
            echo "ERROR: could not acquire remote deployment lock: $lock_status" >&2
            release_remote_deploy_lock
            return 1
        fi
        if ! kill -0 "$DEPLOY_LOCK_SSH_PID" 2>/dev/null; then
            echo "ERROR: remote deployment lock holder exited unexpectedly" >&2
            release_remote_deploy_lock
            return 1
        fi
        sleep 0.1
    done
    echo "ERROR: timed out acquiring remote deployment lock" >&2
    release_remote_deploy_lock
    return 1
}

assert_public_pilot_disabled_for_deploy() {
    remote_bash "$REMOTE_DIR" <<'EOF'
set -euo pipefail
env_path="$1/.env"
python3 - "$env_path" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode):
        raise ValueError("production .env is not a regular file")
    content = bytearray()
    while True:
        chunk = os.read(fd, 65_536)
        if not chunk:
            break
        content.extend(chunk)
        if len(content) > 1024 * 1024:
            raise ValueError("production .env is too large")
finally:
    os.close(fd)

prefix = "FIN_TERMINAL_PUBLIC_ENABLED="
values = []
for line in content.decode("utf-8").splitlines():
    if not line.startswith(prefix):
        continue
    value = line[len(prefix):].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    values.append(value)

if len(values) > 1 or (values and values[0] not in {"true", "false"}):
    raise ValueError("FIN_TERMINAL_PUBLIC_ENABLED must have at most one true/false definition")
if values == ["true"]:
    raise SystemExit(
        "ERROR: normal deployment is blocked while the public terminal pilot is active; "
        "run the approved disable workflow first"
    )
PY
EOF
}

snapshot_remote_release() {
    remote_bash "$REMOTE_DIR" "$REMOTE_BACKUP_DIR" "$DEPLOY_ID" <<'EOF'
set -euo pipefail
remote_dir="$1"
backup_dir="$2"
deploy_id="$3"
[[ "$deploy_id" =~ ^[0-9a-f]{24}$ ]]
mkdir -p "$(dirname "$backup_dir")"
if [[ -e "$backup_dir" ]]; then
    echo "rollback snapshot path already exists: $backup_dir" >&2
    exit 1
fi
test -f "$remote_dir/docker-compose.yml"
test -f "$remote_dir/.env"
test ! -L "$remote_dir/.env"
mkdir -m 700 "$backup_dir"
rollback_tags=()
web_container=""
db_backup_container_path=""
cleanup_snapshot_on_error() {
    local status=$?
    trap - EXIT
    if [[ -n "$web_container" && -n "$db_backup_container_path" ]]; then
        docker exec "$web_container" rm -f -- "$db_backup_container_path" \
            >/dev/null 2>&1 || true
    fi
    if [[ "$status" -ne 0 ]]; then
        if [[ "${#rollback_tags[@]}" -gt 0 ]]; then
            docker image rm "${rollback_tags[@]}" >/dev/null 2>&1 || true
        fi
        rm -rf -- "$backup_dir"
    fi
    exit "$status"
}
trap cleanup_snapshot_on_error EXIT
# The staged preflight may generate a proxy token. Preserve the previous
# environment separately because it is intentionally excluded from source.tgz.
cp -p -- "$remote_dir/.env" "$backup_dir/.env"
chmod 600 "$backup_dir/.env"
# Deployment identity is part of the release transaction. Preserve its exact
# bytes (or explicit absence) so a post-metadata transport failure cannot leave
# old source falsely identified as the new revision after automatic rollback.
if [[ -e "$remote_dir/.deploy-current" ]]; then
    test -f "$remote_dir/.deploy-current"
    test ! -L "$remote_dir/.deploy-current"
    cp -p -- "$remote_dir/.deploy-current" "$backup_dir/.deploy-current"
    printf 'present\n' > "$backup_dir/deploy-current.state"
else
    printf 'absent\n' > "$backup_dir/deploy-current.state"
fi
# Keep the previous Compose file addressable outside the source archive so the
# post-upload runtime comparison can render both revisions with the live .env.
cp -p -- "$remote_dir/docker-compose.yml" "$backup_dir/docker-compose.yml"
test -s "$backup_dir/docker-compose.yml"
    items=(
        Dockerfile
        Dockerfile.unbrowser-mcp
        docker-compose.yml
        docker-compose.public-terminal.yml
        Caddyfile
        deploy/terminal_runtime_reconciler.py
        deploy/terminal-runtime-reconciler.service
        unchained
        research_desk_vendor
        rhythm
    )
present=()
for item in "${items[@]}"; do
    if [[ -e "$remote_dir/$item" ]]; then
        present+=("$item")
    fi
done
printf '%s\n' "${present[@]}" > "$backup_dir/items"
tar -C "$remote_dir" -czf "$backup_dir/source.tgz" "${present[@]}"

# Preserve a verified online SQLite snapshot outside relay_data before any
# candidate container can start and apply an additive auth-schema migration.
# Rollback does not restore this automatically because newer writes may exist;
# the retained snapshot is for explicit recovery after diagnosis.
cd "$remote_dir"
web_container="$(docker compose ps -q web)"
[[ "$web_container" =~ ^[0-9a-f]{12,64}$ ]]
db_backup_container_path="/tmp/unchained-auth-${deploy_id}.db"
docker exec -i "$web_container" python3 - "$db_backup_container_path" <<'PY'
import os
import sqlite3
import sys

destination = sys.argv[1]
if os.path.exists(destination):
    raise SystemExit("temporary auth backup already exists")

source = sqlite3.connect("file:/data/auth.db?mode=ro", uri=True, timeout=30)
target = sqlite3.connect(destination, timeout=30)
try:
    source.backup(target)
    result = target.execute("PRAGMA quick_check").fetchone()
    if result != ("ok",):
        raise RuntimeError(f"auth backup quick_check failed: {result!r}")
finally:
    target.close()
    source.close()
PY
docker cp "$web_container:$db_backup_container_path" "$backup_dir/auth.db.backup"
docker exec "$web_container" rm -f -- "$db_backup_container_path"
db_backup_container_path=""
chmod 600 "$backup_dir/auth.db.backup"
[[ -s "$backup_dir/auth.db.backup" ]]
sha256sum "$backup_dir/auth.db.backup" > "$backup_dir/auth.db.backup.sha256"

# Candidate builds replace Compose image tags. Retain an independently tagged
# reference to every current runtime image so automatic rollback never needs to
# resolve mutable package indexes or rebuild an old source tree.
mapfile -t runtime_services < <(docker compose config --services | grep -v '^caddy$')
[[ "${#runtime_services[@]}" -gt 0 ]]
image_map="$backup_dir/runtime-images.tsv"
: > "$image_map"
for service in "${runtime_services[@]}"; do
    [[ "$service" =~ ^[a-z0-9][a-z0-9-]*$ ]]
    container="$(docker compose ps -q "$service")"
    [[ -n "$container" ]]
    image_id="$(docker inspect --format '{{.Image}}' "$container")"
    image_ref="$(docker inspect --format '{{.Config.Image}}' "$container")"
    [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]]
    if [[ ! "$image_ref" =~ ^[A-Za-z0-9._/:@-]+$ \
        || "$image_ref" == *@* || "$image_ref" == sha256:* ]]; then
        echo "ERROR: runtime service $service does not use a retaggable image reference: $image_ref" >&2
        exit 1
    fi
    rollback_ref="unchained-deploy-rollback:${deploy_id}-${service}"
    docker image tag "$image_id" "$rollback_ref"
    rollback_tags+=("$rollback_ref")
    printf '%s\t%s\t%s\t%s\n' \
        "$service" "$image_ref" "$image_id" "$rollback_ref" >> "$image_map"
done
[[ "$(wc -l < "$image_map")" -eq "${#runtime_services[@]}" ]]
trap - EXIT
EOF
    DEPLOY_BACKUP_READY=true
}

release_remote_rollback_images() {
    remote_bash "$REMOTE_BACKUP_DIR" <<'EOF'
set -euo pipefail
backup_dir="$1"
image_map="$backup_dir/runtime-images.tsv"
[[ -f "$image_map" ]] || exit 0
while IFS=$'\t' read -r service image_ref image_id rollback_ref; do
    [[ -n "$service" && -n "$image_ref" && -n "$image_id" && -n "$rollback_ref" ]]
    docker image rm "$rollback_ref" >/dev/null
done < "$image_map"
EOF
}

create_remote_config_stage() {
    local helper
    for helper in "$CADDY_CONFIG_PREFLIGHT_TOOL" "$FIN_TERMINAL_SECRETS_TOOL"; do
        if [[ ! -f "$helper" ]]; then
            echo "ERROR: missing Caddy preflight helper: $helper" >&2
            return 1
        fi
    done
    remote_bash "$REMOTE_DIR" "$REMOTE_CONFIG_STAGE" "$DEPLOY_ID" <<'EOF'
set -euo pipefail
remote_dir="$1"
stage_dir="$2"
deploy_id="$3"
[[ "$deploy_id" =~ ^[0-9a-f]{24}$ ]]
[[ "$stage_dir" == "$remote_dir/.deploy-staging/$deploy_id" ]]
test -f "$remote_dir/.env" || {
    echo "ERROR: production .env is missing" >&2
    exit 1
}
test ! -L "$remote_dir/.env" || {
    echo "ERROR: refusing symlinked production .env" >&2
    exit 1
}
mkdir -p "$remote_dir/.deploy-staging"
if [[ -e "$stage_dir" ]]; then
    echo "ERROR: Caddy preflight stage already exists" >&2
    exit 1
fi
umask 077
mkdir -m 700 "$stage_dir"
cleanup_stage_on_error() {
    local status=$?
    trap - EXIT
    if [[ "$status" -ne 0 ]]; then
        rm -rf -- "$stage_dir"
    fi
    exit "$status"
}
trap cleanup_stage_on_error EXIT
cp -p -- "$remote_dir/.env" "$stage_dir/.env"
chmod 600 "$stage_dir/.env"
trap - EXIT
EOF
    REMOTE_CONFIG_STAGE_ACTIVE=true
    "${SCP_CMD[@]}" \
        "${TOP_LEVEL_CONTEXT_FILES[@]}" \
        "$CADDY_CONFIG_PREFLIGHT_TOOL" \
        "$FIN_TERMINAL_SECRETS_TOOL" \
        "$EC2_USER@$EC2_HOST:$REMOTE_CONFIG_STAGE/"
}

install_staged_public_turnstile_values() {
    if [[ -z "$TURNSTILE_SITE_KEY_INPUT" ]]; then
        echo "    External Turnstile values not supplied; retaining deployment-host values."
        unset TURNSTILE_SITE_KEY_INPUT TURNSTILE_SECRET_INPUT
        return 0
    fi

    local helper_path="$REMOTE_CONFIG_STAGE/ensure_fin_terminal_secrets.py"
    local env_path="$REMOTE_CONFIG_STAGE/.env"
    local quoted_helper quoted_env result
    printf -v quoted_helper '%q' "$helper_path"
    printf -v quoted_env '%q' "$env_path"
    if ! result="$(
        printf '%s\0%s\0' "$TURNSTILE_SITE_KEY_INPUT" "$TURNSTILE_SECRET_INPUT" \
            | "${SSH_CMD[@]}" \
                "python3 $quoted_helper --install-public-turnstile $quoted_env"
    )"; then
        unset TURNSTILE_SITE_KEY_INPUT TURNSTILE_SECRET_INPUT
        echo "ERROR: failed to provision staged Turnstile values" >&2
        return 1
    fi
    unset TURNSTILE_SITE_KEY_INPUT TURNSTILE_SECRET_INPUT

    case "$result" in
        turnstile_changed=true)
            echo "    Staged external Turnstile values updated."
            ;;
        turnstile_changed=false)
            echo "    Existing external Turnstile values retained."
            ;;
        *)
            echo "ERROR: unexpected staged Turnstile provisioning result" >&2
            return 1
            ;;
    esac
}

ensure_staged_fin_terminal_secrets() {
    local result
    result="$(remote_bash "$REMOTE_CONFIG_STAGE" <<'EOF'
set -euo pipefail
stage_dir="$1"
test -f "$stage_dir/ensure_fin_terminal_secrets.py"
python3 "$stage_dir/ensure_fin_terminal_secrets.py" --ensure-status "$stage_dir/.env"
EOF
)"
    case "$result" in
        fin_terminal_credentials_changed=true)
            FIN_TERMINAL_SECRETS_CHANGED=true
            echo "    Generated independent fin-terminal credential(s) on the host."
            ;;
        fin_terminal_credentials_changed=false)
            echo "    Existing independent fin-terminal credentials retained."
            ;;
        *)
            echo "ERROR: unexpected fin-terminal credential preparation result" >&2
            return 1
            ;;
    esac
}

validate_staged_caddy_config() {
    remote_bash "$REMOTE_CONFIG_STAGE" "$REMOTE_DIR" "$DEPLOY_ID" <<'EOF'
set -euo pipefail
stage_dir="$1"
remote_dir="$2"
deploy_id="$3"
exec bash "$stage_dir/caddy_config_preflight.sh" \
    validate "$stage_dir" "$remote_dir" "$deploy_id"
EOF
}

promote_staged_config() {
    local result
    result="$(remote_bash "$REMOTE_CONFIG_STAGE" "$REMOTE_DIR" "$DEPLOY_ID" <<'EOF'
set -euo pipefail
stage_dir="$1"
remote_dir="$2"
deploy_id="$3"
exec bash "$stage_dir/caddy_config_preflight.sh" \
    promote "$stage_dir" "$remote_dir" "$deploy_id"
EOF
)"
    case "$result" in
        environment_changed=true|environment_changed=false)
            ;;
        *)
            echo "ERROR: unexpected Caddy preflight promotion result: $result" >&2
            return 1
            ;;
    esac
}

cleanup_remote_config_stage() {
    if [[ "$REMOTE_CONFIG_STAGE_ACTIVE" != "true" ]]; then
        return
    fi
    remote_bash "$REMOTE_DIR" "$REMOTE_CONFIG_STAGE" "$DEPLOY_ID" <<'EOF'
set -euo pipefail
remote_dir="$1"
stage_dir="$2"
deploy_id="$3"
[[ "$deploy_id" =~ ^[0-9a-f]{24}$ ]]
[[ "$stage_dir" == "$remote_dir/.deploy-staging/$deploy_id" ]]
rm -rf -- "$stage_dir"
rmdir "$remote_dir/.deploy-staging" 2>/dev/null || true
EOF
    REMOTE_CONFIG_STAGE_ACTIVE=false
}

upload_deploy_helpers() {
    local helper
    for helper in "$COMPOSE_DIFF_TOOL" "$FIN_TERMINAL_SECRETS_TOOL"; do
        if [[ ! -f "$helper" ]]; then
            echo "ERROR: missing deploy helper: $helper" >&2
            return 1
        fi
    done
    remote_bash "$REMOTE_DEPLOY_TOOLS_DIR" <<'EOF'
set -euo pipefail
mkdir -p "$1"
EOF
    "${SCP_CMD[@]}" "$COMPOSE_DIFF_TOOL" "$FIN_TERMINAL_SECRETS_TOOL" \
        "$EC2_USER@$EC2_HOST:$REMOTE_DEPLOY_TOOLS_DIR/"
}

upload_host_runtime_files() {
    local runtime_file
    for runtime_file in "${HOST_RUNTIME_FILES[@]}"; do
        if [[ ! -f "$SCRIPT_DIR/deploy/$runtime_file" ]]; then
            echo "ERROR: missing host runtime file: deploy/$runtime_file" >&2
            return 1
        fi
    done
    remote_bash "$REMOTE_DIR/deploy" <<'EOF'
set -euo pipefail
mkdir -p "$1"
EOF
    local upload_files=()
    for runtime_file in "${HOST_RUNTIME_FILES[@]}"; do
        upload_files+=("$SCRIPT_DIR/deploy/$runtime_file")
    done
    "${SCP_CMD[@]}" "${upload_files[@]}" \
        "$EC2_USER@$EC2_HOST:$REMOTE_DIR/deploy/"
}

compare_compose_services() {
    remote_bash "$REMOTE_DIR" "$REMOTE_BACKUP_DIR" \
        "$REMOTE_DEPLOY_TOOLS_DIR/compose_service_diff.py" <<'EOF'
set -euo pipefail
remote_dir="$1"
backup_dir="$2"
diff_tool="$3"
test -f "$backup_dir/docker-compose.yml"
test -f "$diff_tool"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

# Resolve both files with the live project directory so interpolation uses the
# same .env values. --no-path-resolution avoids false differences caused by
# the backup file living under .deploy-backups rather than the release root.
docker compose --project-directory "$remote_dir" -f "$backup_dir/docker-compose.yml" \
    config --format json --no-path-resolution > "$tmp_dir/old.json"
docker compose --project-directory "$remote_dir" -f "$remote_dir/docker-compose.yml" \
    config --format json --no-path-resolution > "$tmp_dir/new.json"
python3 "$diff_tool" "$tmp_dir/old.json" "$tmp_dir/new.json"
EOF
}

add_services() {
    local services="$1"
    local service
    for service in $services; do
        case "$service" in
            caddy|relay|private-core|mcp|unbrowser-egress|unbrowser-mcp|fin-terminal|web|scheduler|trial-agent|fin-terminal-demo)
                # rollback-only: fin-terminal-demo is retired but accepted so a failed
                # retirement deploy can restore the old snapshot and restart the demo.
                ;;
            "")
                continue
                ;;
            *)
                echo "ERROR: unrecognized service from deploy classifier: $service" >&2
                return 1
                ;;
        esac
        if [[ " $SERVICES_TO_REBUILD " != *" $service "* ]]; then
            SERVICES_TO_REBUILD="${SERVICES_TO_REBUILD:+$SERVICES_TO_REBUILD }$service"
        fi
    done
}

restart_services_serially() {
    local services="$1"
    remote_bash "$REMOTE_DIR" "$services" <<'EOF'
set -euo pipefail
remote_dir="$1"
services="$2"
cd "$remote_dir"

selected() {
    [[ " $services " == *" $1 "* ]]
}

container_state() {
    local service="$1" container
    container="$(docker compose ps -q "$service")"
    [[ -n "$container" ]] || return 1
    docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container"
}

wait_for_state() {
    local service="$1" expected="$2" attempt state
    for attempt in $(seq 1 24); do
        state="$(container_state "$service" 2>/dev/null || true)"
        if [[ "$state" == "$expected" ]]; then
            return 0
        fi
        if [[ "$state" == "unhealthy" || "$state" == "exited" || "$state" == "dead" ]]; then
            docker compose logs --tail 40 "$service" >&2 || true
            return 1
        fi
        sleep 2
    done
    echo "Timed out waiting for $service to become $expected (last state: ${state:-missing})" >&2
    docker compose logs --tail 40 "$service" >&2 || true
    return 1
}

expected_state() {
    case "$1" in
        relay|private-core|mcp|unbrowser-egress|unbrowser-mcp|fin-terminal|web)
            printf '%s\n' healthy
            ;;
        fin-terminal-demo)
            # rollback-only: retired demo may be restarted during restore
            printf '%s\n' healthy
            ;;
        scheduler|trial-agent)
            printf '%s\n' running
            ;;
        *)
            return 1
            ;;
    esac
}

# Keep upstream dependencies available before restarting their consumers.
# --no-deps prevents Compose from expanding this into a broad restart, while
# --force-recreate guarantees restart-dependent readiness checks observe a new
# container even when its rendered Compose configuration is unchanged.
# Note: fin-terminal-demo appears here only for rollback compatibility — a
# failed retirement deployment may restore the old snapshot and restart it.
for service in relay private-core unbrowser-egress web mcp unbrowser-mcp fin-terminal fin-terminal-demo scheduler trial-agent; do
    if ! selected "$service"; then
        continue
    fi
    echo "    Restarting $service..."
    docker compose up -d --no-deps --no-build --force-recreate "$service"
    wait_for_state "$service" "$(expected_state "$service")"
done
EOF
}

rollback_remote_release() {
    if [[ "$DEPLOY_BACKUP_READY" != "true" || "$DEPLOY_ROLLBACK_ATTEMPTED" == "true" ]]; then
        return
    fi
    DEPLOY_ROLLBACK_ATTEMPTED=true
    echo "==> Deployment failed; restoring the previous release..." >&2
    if ! remote_bash "$REMOTE_DIR" "$REMOTE_BACKUP_DIR" <<'EOF'
set -euo pipefail
remote_dir="$1"
backup_dir="$2"
test -f "$backup_dir/source.tgz"
test -f "$backup_dir/.env"
test -s "$backup_dir/runtime-images.tsv"
test -f "$backup_dir/deploy-current.state"
metadata_state="$(<"$backup_dir/deploy-current.state")"
case "$metadata_state" in
    present)
        test -f "$backup_dir/.deploy-current"
        test ! -L "$backup_dir/.deploy-current"
        ;;
    absent) ;;
    *)
        echo "ERROR: invalid rollback deployment-metadata state" >&2
        exit 1
        ;;
esac

# Validate every retained image before mutating the failed release again.
while IFS=$'\t' read -r service image_ref image_id rollback_ref; do
    [[ "$service" =~ ^[a-z0-9][a-z0-9-]*$ ]]
    [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]]
    [[ "$image_ref" =~ ^[A-Za-z0-9._/:@-]+$ \
        && "$image_ref" != *@* && "$image_ref" != sha256:* ]]
    [[ "$rollback_ref" =~ ^unchained-deploy-rollback:[0-9a-f]{24}-[a-z0-9-]+$ ]]
    retained_id="$(docker image inspect --format '{{.Id}}' "$rollback_ref")"
    [[ "$retained_id" == "$image_id" ]]
done < "$backup_dir/runtime-images.tsv"

rm -rf "$remote_dir/unchained" "$remote_dir/research_desk_vendor" "$remote_dir/rhythm"
rm -f "$remote_dir/Dockerfile" "$remote_dir/Dockerfile.unbrowser-mcp" \
      "$remote_dir/docker-compose.yml" "$remote_dir/docker-compose.public-terminal.yml" \
      "$remote_dir/Caddyfile" "$remote_dir/.env" \
      "$remote_dir/deploy/terminal_runtime_reconciler.py" \
      "$remote_dir/deploy/terminal-runtime-reconciler.service"
tar -C "$remote_dir" -xzf "$backup_dir/source.tgz"
cp -p -- "$backup_dir/.env" "$remote_dir/.env"
chmod 600 "$remote_dir/.env"
if [[ "$metadata_state" == "present" ]]; then
    metadata_tmp="$(mktemp "$remote_dir/.deploy-current.rollback.XXXXXX")"
    cat -- "$backup_dir/.deploy-current" > "$metadata_tmp"
    chmod 644 "$metadata_tmp"
    mv -f -- "$metadata_tmp" "$remote_dir/.deploy-current"
    cmp -s "$backup_dir/.deploy-current" "$remote_dir/.deploy-current"
else
    rm -f -- "$remote_dir/.deploy-current"
    test ! -e "$remote_dir/.deploy-current"
fi
cd "$remote_dir"
mapfile -t runtime_services < <(docker compose config --services | grep -v '^caddy$')
[[ "${#runtime_services[@]}" -gt 0 ]]
declare -A restored_services=()
while IFS=$'\t' read -r service image_ref image_id rollback_ref; do
    docker image tag "$rollback_ref" "$image_ref"
    restored_id="$(docker image inspect --format '{{.Id}}' "$image_ref")"
    [[ "$restored_id" == "$image_id" ]]
    restored_services["$service"]=1
done < "$backup_dir/runtime-images.tsv"
[[ "${#restored_services[@]}" -eq "${#runtime_services[@]}" ]]
for service in "${runtime_services[@]}"; do
    [[ "${restored_services[$service]:-}" == 1 ]]
done
EOF
    then
        echo "ERROR: automatic rollback failed; inspect $REMOTE_BACKUP_DIR on the host." >&2
        return 1
    fi
    local rollback_runtime_services
    rollback_runtime_services="$(remote_bash "$REMOTE_DIR" <<'EOF'
set -euo pipefail
cd "$1"
docker compose config --services | grep -v '^caddy$' | tr '\n' ' '
EOF
)"
    if [[ -z "$rollback_runtime_services" ]] \
        || ! restart_services_serially "$rollback_runtime_services"; then
        echo "ERROR: rollback restored source but could not restart all services serially." >&2
        return 1
    fi
    if ! remote_bash "$REMOTE_DIR" <<'EOF'
set -euo pipefail
cd "$1"
# Recreate Caddy from the restored Compose definition so rolled-back
# environment values and network attachments cannot linger. Preserve opt-in
# public-terminal containers by including their overlay when it can be rendered;
# otherwise omit --remove-orphans rather than deleting an independently rolled
# pilot during an unrelated default-stack rollback.
compose_args=(-f docker-compose.yml)
remove_orphans=(--remove-orphans)
if [[ -f docker-compose.public-terminal.yml ]]; then
    if docker compose -f docker-compose.yml -f docker-compose.public-terminal.yml \
        config --quiet >/dev/null 2>&1; then
        compose_args+=(-f docker-compose.public-terminal.yml)
    else
        echo "    Public-terminal overlay is not renderable; preserving orphan containers." >&2
        remove_orphans=()
    fi
fi
docker compose "${compose_args[@]}" up -d --no-deps --no-build \
    --force-recreate "${remove_orphans[@]}" caddy
for attempt in $(seq 1 24); do
    container="$(docker compose ps -q caddy)"
    state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || true)"
    if [[ "$state" == "running" || "$state" == "healthy" ]]; then
        exit 0
    fi
    if [[ "$state" == "unhealthy" || "$state" == "exited" || "$state" == "dead" ]]; then
        docker compose logs --tail 40 caddy >&2 || true
        exit 1
    fi
    sleep 2
done
echo "Timed out waiting for caddy to become running during rollback" >&2
docker compose logs --tail 40 caddy >&2 || true
exit 1
EOF
    then
        echo "ERROR: rollback restarted services but could not restore Caddy." >&2
        return 1
    fi
    release_remote_rollback_images \
        || echo "    (could not release retained rollback image tags; keeping them for recovery)" >&2
    echo "    Previous source release restored; verify services before retrying." >&2
}

verify_production_health() {
    local services="$1"
    remote_bash "$REMOTE_DIR" "$services" "$DEPLOY_HEALTH_HOST" "$FIN_TERMINAL_PUBLIC_HOST" <<'EOF'
set -euo pipefail
remote_dir="$1"
services="$2"
health_host="$3"
public_host="$4"
cd "$remote_dir"

selected() {
    [[ " $services " == *" $1 "* ]]
}

container_state() {
    local service="$1" container
    container="$(docker compose ps -q "$service")"
    [[ -n "$container" ]] || return 1
    docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container"
}

wait_for_state() {
    local service="$1" expected="$2" attempt state
    for attempt in $(seq 1 24); do
        state="$(container_state "$service" 2>/dev/null || true)"
        if [[ "$state" == "$expected" ]]; then
            return 0
        fi
        if [[ "$state" == "unhealthy" || "$state" == "exited" || "$state" == "dead" ]]; then
            docker compose logs --tail 40 "$service" >&2 || true
            return 1
        fi
        sleep 2
    done
    echo "Timed out waiting for $service to become $expected (last state: ${state:-missing})" >&2
    docker compose logs --tail 40 "$service" >&2 || true
    return 1
}

# These lists are intentionally explicit because health-checked services must
# reach "healthy", while process-only services below must reach "running".
# The Compose service-list contract above fails deployment when either policy
# needs to be updated for a newly added service.
for service in relay private-core mcp unbrowser-egress unbrowser-mcp fin-terminal web; do
    if ! selected "$service"; then
        continue
    fi
    wait_for_state "$service" healthy
done
for service in caddy scheduler trial-agent; do
    wait_for_state "$service" running
done
if selected trial-agent; then
    for attempt in $(seq 1 30); do
        if docker compose logs --since 2m trial-agent 2>&1 | grep -q 'Authenticated\. Model:'; then
            break
        fi
        if [[ "$attempt" == "30" ]]; then
            echo "trial-agent did not authenticate after restart" >&2
            docker compose logs --tail 80 trial-agent >&2 || true
            exit 1
        fi
        sleep 2
    done
fi

for attempt in $(seq 1 20); do
    if curl --fail --silent --show-error --connect-timeout 3 --max-time 10 \
        --resolve "$health_host:443:127.0.0.1" \
        "https://$health_host/health" >/dev/null \
        && curl --fail --silent --show-error --connect-timeout 3 --max-time 10 \
        --resolve "$health_host:443:127.0.0.1" \
        "https://$health_host/" >/dev/null; then
        public_site_ready=true
        break
    fi
    sleep 2
done
if [[ "${public_site_ready:-false}" != "true" ]]; then
    echo "public Caddy health checks failed" >&2
    docker compose logs --tail 80 caddy >&2 || true
    exit 1
fi

# The legacy authenticated-terminal URL must redirect to the canonical
# subdomain route. This catches a healthy but stale Caddy process that is still
# serving the previous route after a failed reload.
for attempt in $(seq 1 20); do
    legacy_terminal_check="$(curl --silent --show-error --connect-timeout 3 --max-time 10 \
        --output /dev/null --write-out '%{http_code} %{redirect_url}' \
        --resolve "$health_host:443:127.0.0.1" \
        "https://$health_host/unbrowser/fin-terminal/" || true)"
    if [[ "$legacy_terminal_check" == "308 https://$public_host/fin-terminal/" ]]; then
        break
    fi
    sleep 2
done
if [[ "$legacy_terminal_check" != "308 https://$public_host/fin-terminal/" ]]; then
    echo "legacy authenticated fin-terminal redirect health check failed (result: ${legacy_terminal_check:-request-failed})" >&2
    docker compose logs --tail 80 caddy web fin-terminal >&2 || true
    exit 1
fi

# The public Unbrowser host must serve its landing page and authenticated
# terminal with no session, proving its dedicated Caddy site and TLS
# certificate are live after reload.
for attempt in $(seq 1 20); do
    if curl --fail --silent --show-error --connect-timeout 3 --max-time 10 \
        --resolve "$public_host:443:127.0.0.1" \
        "https://$public_host/" \
        | grep -Fq "unbrowser by Unchained - MCP Browser for LLM Agents"; then
        unbrowser_page_ready=true
        break
    fi
    sleep 2
done
if [[ "${unbrowser_page_ready:-false}" != "true" ]]; then
    echo "public Unbrowser landing-page health check failed" >&2
    docker compose logs --tail 80 caddy web >&2 || true
    exit 1
fi

# A logged-out request to the persistent terminal must reach forward_auth;
# it must never silently become the anonymous kiosk session.
for attempt in $(seq 1 20); do
    terminal_status="$(curl --silent --show-error --connect-timeout 3 --max-time 10 \
        --output /dev/null --write-out '%{http_code}' \
        --resolve "$public_host:443:127.0.0.1" \
        "https://$public_host/fin-terminal/" || true)"
    if [[ "$terminal_status" == "401" ]]; then
        break
    fi
    sleep 2
done
if [[ "$terminal_status" != "401" ]]; then
    echo "authenticated subdomain fin-terminal route health check failed (status: ${terminal_status:-request-failed})" >&2
    docker compose logs --tail 80 caddy web fin-terminal >&2 || true
    exit 1
fi

terminal_base_check="$(curl --silent --show-error --connect-timeout 3 --max-time 10 \
    --output /dev/null --write-out '%{http_code} %{redirect_url}' \
    --resolve "$public_host:443:127.0.0.1" \
    "https://$public_host/fin-terminal" || true)"
if [[ "$terminal_base_check" != "308 https://$public_host/fin-terminal/" ]]; then
    echo "authenticated terminal base redirect health check failed (result: ${terminal_base_check:-request-failed})" >&2
    docker compose logs --tail 80 caddy >&2 || true
    exit 1
fi

# The static fin-terminal replay demo is retired. Verify all its former
# URLs return direct HTTP 404 (no redirect, no-store cache control).
# Each host/path pair uses the URL's own host for --resolve so apex and
# subdomain URLs each hit their correct Caddy site block.
retired_routes=(
    "$public_host|/fin-terminal-demo/"
    "$public_host|/fin-terminal-demo"
    "$public_host|/fin-terminal-demo/ws"
    "$health_host|/unbrowser/fin-terminal-demo/"
    "$health_host|/unbrowser/fin-terminal-demo"
    "$health_host|/unbrowser/fin-terminal/demo/"
    "$health_host|/unbrowser/fin-terminal/demo"
)
for route in "${retired_routes[@]}"; do
    IFS='|' read -r host path <<< "$route"
    retired_check="$(curl --silent --show-error --connect-timeout 3 --max-time 10 \
        --output /dev/null --write-out '%{http_code}' \
        --resolve "$host:443:127.0.0.1" \
        "https://$host$path" || true)"
    if [[ "$retired_check" != "404" ]]; then
        echo "retired fin-terminal-demo URL https://$host$path returned ${retired_check:-request-failed} (expected 404)" >&2
        docker compose logs --tail 80 caddy >&2 || true
        exit 1
    fi
done

exit 0
EOF
}

write_deploy_metadata() {
    remote_bash "$REMOTE_DIR" "$DEPLOY_REVISION" "$DEPLOY_ID" <<'EOF'
set -euo pipefail
remote_dir="$1"
revision="$2"
deploy_id="$3"
tmp="$remote_dir/.deploy-current.tmp"
printf 'revision=%s\ndeploy_id=%s\ndeployed_at=%s\n' \
    "$revision" "$deploy_id" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp"
mv "$tmp" "$remote_dir/.deploy-current"
EOF
}

prune_remote_deploy_backups() {
    remote_bash "$REMOTE_DIR" "$REMOTE_BACKUP_DIR" <<'EOF'
set -euo pipefail
remote_dir="$1"
current_backup="$2"
backup_root="$remote_dir/.deploy-backups"
[[ -d "$backup_root" ]] || exit 0
# Keep recent snapshots for diagnosis and manual recovery, but prevent source
# archives from consuming the small production root disk indefinitely.
find "$backup_root" -mindepth 1 -maxdepth 1 -type d \
    ! -path "$current_backup" -mtime +14 -exec rm -rf {} +
EOF
}

cleanup_deploy() {
    local status="${1:-0}"
    trap - EXIT
    set +e
    cleanup_remote_config_stage || echo "ERROR: could not remove staged Caddy preflight files." >&2
    if [[ "$status" -ne 0 && "$DEPLOY_SUCCEEDED" != "true" && "$DEPLOY_MUTATED" == "true" && "$DEPLOY_BACKUP_READY" == "true" && "$DEPLOY_ROLLBACK_ON_FAILURE" == "1" ]]; then
        rollback_remote_release
    elif [[ "$status" -ne 0 && "$DEPLOY_MUTATED" != "true" && "$DEPLOY_BACKUP_READY" == "true" ]]; then
        release_remote_rollback_images \
            || echo "ERROR: could not release retained images after pre-mutation failure." >&2
    fi
    restore_private_core_worktree
    release_remote_deploy_lock
    rm -f "$REMOTE_CHECKSUMS_FILE" "$LOCAL_CHECKSUMS_FILE" "$DEPLOYED_PATHS_FILE"
    return "$status"
}

trap 'cleanup_deploy $?' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

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
    for f in "${HOST_RUNTIME_FILES[@]}"; do
        printf '%s\n' "deploy/$f"
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
    DEPLOY_OVERLAY_APPLIED=true
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

echo "==> Acquiring exclusive deployment lock..."
acquire_remote_deploy_lock
echo "    Lock acquired."
echo "==> Verifying public terminal pilot is disabled..."
assert_public_pilot_disabled_for_deploy
echo "    Public terminal pilot is disabled."
echo "==> Snapshotting current release for automatic rollback..."
snapshot_remote_release

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

# Stage the prospective top-level release before mutating the live source
# directory. The staged .env receives any generated terminal credential, allowing
# the Caddy candidate to be validated with the exact future Compose image and
# environment while a malformed file cannot trigger rollback/recreation.
echo "==> Staging prospective configuration..."
create_remote_config_stage
echo "==> Provisioning staged Turnstile values..."
install_staged_public_turnstile_values
echo "==> Validating staged fin-terminal production secrets..."
ensure_staged_fin_terminal_secrets
echo "==> Validating staged Caddyfile..."
validate_staged_caddy_config

echo "==> Promoting validated configuration..."
DEPLOY_MUTATED=true
promote_staged_config
cleanup_remote_config_stage

echo "==> Uploading deploy helpers..."
upload_deploy_helpers
echo "==> Uploading host runtime controller files..."
upload_host_runtime_files

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
expected=$(printf '%s\n' caddy fin-terminal mcp private-core relay scheduler trial-agent unbrowser-egress unbrowser-mcp web)
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
CADDY_RECREATE_REQUIRED=false
if $FIN_TERMINAL_SECRETS_CHANGED; then
    # Existing Caddy and terminal containers retain their old environment.
    # Recreate all default terminal trust-boundary participants so generated
    # persistent, replay, or public-edge credentials are never one-sided.
    echo "==> Fin-terminal credentials changed; recreating Caddy and terminal services."
    add_services "caddy fin-terminal"
    CADDY_RECREATE_REQUIRED=true
fi
if $FORCE_FULL_DEPLOY; then
    SERVICES_TO_REBUILD="$ALL_SERVICES"
    # --full skips file-level classification, so compose/network changes can't
    # be ruled out. Reload first later, then recreate if needed.
    CADDY_RECREATE_REQUIRED=true
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

            # Run the static ownership classifier first. Compose changes are
            # resolved separately against the pre-upload Compose snapshot.
            CLASSIFIER_OUTPUT=""
            if ! CLASSIFIER_OUTPUT=$(printf '%s\n' "$CHANGED_FILES" \
                | python3 "$SCRIPT_DIR/deploy/classify_changes.py"); then
                echo "    (classifier failed — falling back to full deploy)" >&2
                SERVICES_TO_REBUILD="$ALL_SERVICES"
                CADDY_RECREATE_REQUIRED=true
            elif echo "$CLASSIFIER_OUTPUT" | grep -qx ALL; then
                # Caddy uses a public image, so a Dockerfile/dependency change
                # needs every application service rebuilt but not Caddy itself.
                # A separately changed Caddyfile is added below for reload.
                echo "    Classifier: all application services need rebuild"
                SERVICES_TO_REBUILD="$ALL_RUNTIME_SERVICES"
            else
                CLASSIFIER_SERVICES=$(printf '%s\n' "$CLASSIFIER_OUTPUT" \
                    | grep -Ev '^(COMPOSE|ALL)?$' || true)
                if [[ -n "$CLASSIFIER_SERVICES" ]]; then
                    add_services "$CLASSIFIER_SERVICES"
                fi
            fi

            # Caddyfile is mounted live. Reload it only when it actually
            # changed, even if another file forced a broad app rebuild.
            if printf '%s\n' "$CHANGED_FILES" | grep -qx 'Caddyfile'; then
                add_services caddy
            fi

            if printf '%s\n' "$CHANGED_FILES" \
                | grep -qx 'docker-compose.public-terminal.yml'; then
                echo "    Public-terminal overlay staged; opt-in profile services left unchanged."
            fi

            if printf '%s\n' "$CHANGED_FILES" | grep -qx 'docker-compose.yml'; then
                echo "    Comparing resolved Compose service config..."
                if ! COMPOSE_DIFF_OUTPUT=$(compare_compose_services); then
                    echo "    (Compose comparison failed — falling back to full deploy)" >&2
                    SERVICES_TO_REBUILD="$ALL_SERVICES"
                    CADDY_RECREATE_REQUIRED=true
                elif echo "$COMPOSE_DIFF_OUTPUT" | grep -qx ALL; then
                    echo "    Compose topology changed: full deploy required"
                    SERVICES_TO_REBUILD="$ALL_SERVICES"
                    CADDY_RECREATE_REQUIRED=true
                elif [[ -n "$COMPOSE_DIFF_OUTPUT" ]]; then
                    echo "    Compose changed services: $(echo "$COMPOSE_DIFF_OUTPUT" | tr '\n' ' ' | sed 's/ *$//')"
                    add_services "$COMPOSE_DIFF_OUTPUT"
                    if echo "$COMPOSE_DIFF_OUTPUT" | grep -qx caddy; then
                        CADDY_RECREATE_REQUIRED=true
                    fi
                else
                    echo "    Compose change has no effective service runtime impact."
                fi
            fi

            if [[ -z "$SERVICES_TO_REBUILD" ]]; then
                echo "    Classifier: no services need rebuild (docs/test changes only)"
            else
                echo "    Classifier: rebuilding [$SERVICES_TO_REBUILD]"
            fi
        fi
    else
        echo "    (local checksum failed — falling back to full deploy)"
        SERVICES_TO_REBUILD="$ALL_SERVICES"
        # Unknown change set: be conservative after attempting graceful reload.
        CADDY_RECREATE_REQUIRED=true
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

# Recreate one service at a time in dependency order. Each service reaches its
# expected Docker health state before a consumer is restarted, so a Compose
# change cannot briefly take every Caddy upstream down at once.
RESTART_SERVICES=$(echo "$SERVICES_TO_REBUILD" | tr ' ' '\n' | grep -v '^$' | grep -v '^caddy$' | tr '\n' ' ' | sed 's/ *$//' || true)

if [[ -n "$RESTART_SERVICES" ]]; then
    echo "==> Recreating containers serially: $RESTART_SERVICES"
    restart_services_serially "$RESTART_SERVICES"
else
    echo "==> No service containers to recreate."
fi

# Caddy: prefer graceful reload over restart. caddy reload re-reads the
# Caddyfile and updates upstream resolutions in-place with zero downtime.
# This block runs only for an effective Caddy runtime change or Caddyfile edit.
if echo " $SERVICES_TO_REBUILD " | grep -q ' caddy '; then
    echo "==> Reloading Caddy (graceful when possible)..."
    remote_bash "$REMOTE_DIR" "$CADDY_RECREATE_REQUIRED" <<'EOF'
set -euo pipefail
cd "$1"
recreate_required="$2"
old_container="$(docker compose ps -q caddy 2>/dev/null || true)"
old_state=""
if [[ -n "$old_container" ]]; then
    old_state="$(docker inspect --format '{{.State.Status}}' "$old_container" 2>/dev/null || true)"
fi
if [[ "$old_state" != "running" ]]; then
    recreate_required=true
fi

if [[ "$recreate_required" == "true" ]]; then
    desired_ref="$(docker compose config --format json \
        | python3 -c 'import json, sys; print(json.load(sys.stdin)["services"]["caddy"]["image"])')"
    [[ -n "$desired_ref" ]]
    echo "    Pulling and validating desired Caddy image: $desired_ref"
    docker compose pull caddy </dev/null
    desired_image_id="$(docker image inspect --format '{{.Id}}' "$desired_ref")"
    [[ -n "$desired_image_id" ]]
    docker compose run --rm --no-deps --entrypoint caddy caddy \
        validate --config /etc/caddy/Caddyfile </dev/null
fi

if [[ "$old_state" == "running" ]]; then
    # compose exec is interactive even with -T and otherwise consumes the
    # unread remainder of this `bash -s` script from SSH stdin.
    if ! docker compose exec -T caddy caddy reload \
        --config /etc/caddy/Caddyfile </dev/null; then
        echo "ERROR: Caddy rejected the candidate config; keeping the running edge container." >&2
        exit 1
    fi
    if [[ "$recreate_required" != "true" ]]; then
        exit 0
    fi
    echo "    Caddy reload succeeded; recreating to apply compose/network changes..."
else
    echo "    Caddy is not running; recreating container..."
fi
docker compose up -d --no-deps --no-build --pull never --force-recreate caddy </dev/null

new_container="$(docker compose ps -q caddy)"
if [[ -z "$new_container" || "$new_container" == "$old_container" ]]; then
    echo "ERROR: Caddy force-recreate did not produce a new container." >&2
    exit 1
fi
if [[ -n "${desired_image_id:-}" ]]; then
    actual_image_id="$(docker inspect --format '{{.Image}}' "$new_container")"
    if [[ "$actual_image_id" != "$desired_image_id" ]]; then
        echo "ERROR: recreated Caddy container does not use the desired image." >&2
        exit 1
    fi
    echo "    Caddy recreate verified: ${old_container:-missing} -> $new_container ($desired_ref)"
fi

stable_checks=0
for attempt in $(seq 1 24); do
    container="$(docker compose ps -q caddy)"
    state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || true)"
    if [[ "$state" == "running" || "$state" == "healthy" ]]; then
        stable_checks=$((stable_checks + 1))
        if [[ "$stable_checks" -ge 3 ]]; then
            exit 0
        fi
    else
        stable_checks=0
    fi
    if [[ "$state" == "unhealthy" || "$state" == "exited" || "$state" == "dead" ]]; then
        docker compose logs --tail 40 caddy >&2 || true
        exit 1
    fi
    sleep 2
done
echo "Timed out waiting for caddy to become running" >&2
docker compose logs --tail 40 caddy >&2 || true
exit 1
EOF
fi

# The fin-terminal-demo service is retired. If the backup Compose has the demo
# but the new Compose does not, retire the old container and its dedicated
# network. Never down, wildcard delete, remove volumes, or touch profile services.
retire_fin_terminal_demo() {
    remote_bash "$REMOTE_DIR" "$REMOTE_BACKUP_DIR" \
        "$FIN_TERMINAL_PUBLIC_HOST" "$DEPLOY_HEALTH_HOST" <<'RETIRE_EOF'
set -euo pipefail
remote_dir="$1"
backup_dir="$2"
public_host="$3"
health_host="$4"

backup_compose="$backup_dir/docker-compose.yml"
new_compose="$remote_dir/docker-compose.yml"
backup_env="$backup_dir/.env"

test -f "$backup_compose"
test -f "$new_compose"
test -f "$backup_env"

# Only act when the backup had fin-terminal-demo but the new one does not.
# Preserve the production project identity and path resolution by pointing
# every Compose operation at the live remote directory.
if ! docker compose --project-directory "$remote_dir" \
        -f "$backup_compose" --env-file "$backup_env" \
        config --services | grep -qx fin-terminal-demo; then
    exit 0
fi
if docker compose --project-directory "$remote_dir" \
        -f "$new_compose" config --services 2>/dev/null \
        | grep -qx fin-terminal-demo; then
    exit 0
fi

echo "    Old release contains fin-terminal-demo; retiring the service..."

# Verify new Caddy is routing the retired URLs to 404 before touching the
# old container. This proves the retirement route is live.
# Each host/path pair uses the URL's own host for --resolve.
pre_retire_routes=(
    "$public_host|/fin-terminal-demo/"
    "$health_host|/unbrowser/fin-terminal-demo/"
)
for route in "${pre_retire_routes[@]}"; do
    IFS='|' read -r host url_path <<< "$route"
    code="$(curl --silent --show-error --connect-timeout 3 --max-time 10 \
        --output /dev/null --write-out '%{http_code}' \
        --resolve "$host:443:127.0.0.1" \
        "https://$host$url_path" || true)"
    if [[ "$code" != "404" ]]; then
        echo "ERROR: retired fin-terminal-demo URL https://$host$url_path returned $code (expected 404); aborting container retirement" >&2
        exit 1
    fi
done

# Find all old demo containers (running or stopped) using the backup Compose
# with its project directory pointing to the live remote dir and backup .env.
old_containers="$(docker compose --project-directory "$remote_dir" \
    -f "$backup_compose" --env-file "$backup_env" \
    ps -aq fin-terminal-demo 2>/dev/null || true)"
old_containers="$(echo "$old_containers" | grep -E '^[0-9a-f]{12,}$' || true)"
if [[ -z "$old_containers" ]]; then
    echo "    No old fin-terminal-demo container found (running or stopped)."
else
    count=$(echo "$old_containers" | wc -l | tr -d ' ')
    if [[ "$count" -gt 1 ]]; then
        echo "ERROR: found $count fin-terminal-demo containers (expected at most 1); aborting" >&2
        exit 1
    fi

    # Stop and remove the old demo container.
    echo "    Stopping and removing old fin-terminal-demo container..."
    docker compose --project-directory "$remote_dir" \
        -f "$backup_compose" --env-file "$backup_env" \
        stop fin-terminal-demo 2>/dev/null || true
    docker compose --project-directory "$remote_dir" \
        -f "$backup_compose" --env-file "$backup_env" \
        rm -f fin-terminal-demo 2>/dev/null || true

    # Prove the container is gone.
    remaining="$(docker compose --project-directory "$remote_dir" \
        -f "$backup_compose" --env-file "$backup_env" \
        ps -aq fin-terminal-demo 2>/dev/null | grep -E '^[0-9a-f]{12,}$' || true)"
    if [[ -n "$remaining" ]]; then
        echo "ERROR: fin-terminal-demo container still present after removal: $remaining" >&2
        exit 1
    fi
fi

# Derive the old demo network name from the backup Compose JSON so its
# exact label/scope can be verified before removal. If the backup Compose
# cannot be rendered or the network name cannot be confirmed, leave it
# harmless — never fail deployment over an already-empty legacy network.
demo_network=""
if compose_json="$(docker compose --project-directory "$remote_dir" \
        -f "$backup_compose" --env-file "$backup_env" \
        config --format json --no-path-resolution 2>/dev/null)"; then
    demo_network="$(echo "$compose_json" \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("networks",{}).get("fin_terminal_demo",{}).get("name",""))' 2>/dev/null || true)"
fi
if [[ -n "$demo_network" && "$demo_network" =~ ^[A-Za-z0-9_-]+$ ]]; then
    # Verify the network belongs to the expected Compose project and has the
    # correct network key label before any mutation.
    project_label="$(docker network inspect "$demo_network" \
        --format '{{index .Labels "com.docker.compose.project"}}' 2>/dev/null || true)"
    network_label="$(docker network inspect "$demo_network" \
        --format '{{index .Labels "com.docker.compose.network"}}' 2>/dev/null || true)"
    container_count="$(docker network inspect "$demo_network" \
        --format '{{len .Containers}}' 2>/dev/null || echo 0)"
    if [[ "$project_label" == "unchained" && "$network_label" == "fin_terminal_demo" ]]; then
        if [[ "$container_count" == "0" ]]; then
            if docker network rm "$demo_network" >/dev/null 2>&1; then
                echo "    Removed empty demo network $demo_network (project=$project_label network=$network_label)."
            else
                echo "    (could not remove proven-empty demo network $demo_network; leaving it harmless)"
            fi
        else
            echo "    (demo network $demo_network has $container_count container(s); leaving it harmless)"
        fi
    else
        echo "    (demo network $demo_network labels project=$project_label network=$network_label; skipping removal)"
    fi
elif [[ -z "$demo_network" ]]; then
    echo "    (could not determine demo network name from backup Compose; nothing to remove)"
else
    echo "    (demo network name is unsafe: $demo_network; skipping)"
fi
RETIRE_EOF
}

retire_fin_terminal_demo

echo "==> Verifying production health..."
verify_production_health "$SERVICES_TO_REBUILD"
write_deploy_metadata
# Metadata is the transaction commit point. From here onward only best-effort
# cleanup remains, so an interrupted cleanup must not roll back a healthy,
# revision-stamped release after its retained images have begun to be removed.
DEPLOY_SUCCEEDED=true
release_remote_rollback_images \
    || echo "    (could not release retained rollback image tags; keeping them for recovery)"
prune_remote_deploy_backups || echo "    (backup retention cleanup failed, ignoring)"
echo "    Health checks passed."

# Show status
echo ""
echo "==> Container status:"
remote_bash "$REMOTE_DIR" <<'EOF' || echo "    (container status unavailable after successful health verification)"
set -euo pipefail
remote_dir="$1"
docker compose -f "$remote_dir/docker-compose.yml" ps
EOF

echo ""
echo "==> Relay logs (last 5):"
remote_bash "$REMOTE_DIR" <<'EOF' || echo "    (relay logs unavailable after successful health verification)"
set -euo pipefail
remote_dir="$1"
docker compose -f "$remote_dir/docker-compose.yml" logs relay --tail 5
EOF

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
