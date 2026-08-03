#!/bin/bash
# Production public-terminal pilot control — run on EC2 via verified SSH stdin.
#
# Usage (local caller): ssh ... "bash -s -- ACTION EXPECTED_SHA"
#   ACTION       : activate | disable | status
#   EXPECTED_SHA : exact 40-character lowercase host deployment revision
#
# Never transport Turnstile / OpenRouter values. The remote script reads the
# protected host .env directly during the transaction.

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------
ACTION="${1:-}"
EXPECTED_SHA="${2:-}"

readonly ACTION
readonly EXPECTED_SHA

case "$ACTION" in
    activate|disable|status) ;;
    *)
        echo "ERROR: ACTION must be one of activate, disable, or status" >&2
        exit 1
        ;;
esac

if [[ ! "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "ERROR: EXPECTED_SHA must be a 40-character lowercase hex revision" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Fixed constants
# ---------------------------------------------------------------------------
# In production the remote directory is always /home/$USER/unchained. Allow a
# test-only override via REMOTE_DIR env variable (the production workflow never
# sets this).
readonly REMOTE_DIR="${REMOTE_DIR:-/home/${USER:-ec2-user}/unchained}"

# Hosts — separated for correct routing.
readonly PRIMARY_HOST="unchainedsky.com"
readonly PUBLIC_HOST="unbrowser.unchainedsky.com"
readonly PILOT_PATH="/fin-terminal-live-pilot"
readonly PILOT_URL="https://${PUBLIC_HOST}${PILOT_PATH}/"
readonly MAIN_REPO_URL="https://github.com/protostatis/unchained-infra.git"

# The four profiled pilot services — always these exact names.
readonly PILOT_SERVICES=(
    fin-terminal-public-redis
    fin-terminal-public-unbrowser-mcp
    fin-terminal-public-seat-01
    fin-terminal-public-gateway
)
# Stop/remove order (reverse dependency: gateway → seat → MCP → redis).
readonly PILOT_STOP_ORDER=(
    fin-terminal-public-gateway
    fin-terminal-public-seat-01
    fin-terminal-public-unbrowser-mcp
    fin-terminal-public-redis
)
readonly COMPOSE_ARGS=(
    -f docker-compose.yml
    -f docker-compose.public-terminal.yml
    --profile fin-terminal-public-pilot
)
readonly LOCK_FILE="$REMOTE_DIR/.deploy.lock"
readonly DEPLOY_CURRENT="$REMOTE_DIR/.deploy-current"
readonly ENV_FILE="$REMOTE_DIR/.env"
readonly ENV_FLAG="FIN_TERMINAL_PUBLIC_ENABLED"
# Compose project name used by the base deployment.
readonly COMPOSE_PROJECT="unchained"

# Secure work dir for staging and backups (created per-run under REMOTE_DIR).
SECURE_WORKDIR=""
ROLLBACK_SNAPSHOT=""
STAGED_ENV=""
LOCK_FD=""
ROLLBACK_ARMED=false

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

cleanup_lock() {
    if [[ -n "$LOCK_FD" ]]; then
        exec {LOCK_FD}>&- 2>/dev/null || true
    fi
    LOCK_FD=""
}

acquire_lock() {
    mkdir -p "$(dirname "$LOCK_FILE")"
    exec {LOCK_FD}>>"$LOCK_FILE"
    if ! flock -n "$LOCK_FD"; then
        echo "ERROR: deployment lock is already held" >&2
        exit 75
    fi
}

release_lock() {
    cleanup_lock
}

# ---------------------------------------------------------------------------
# Secure temp workdir (under REMOTE_DIR, mode 0700, cleaned on exit/trap)
# ---------------------------------------------------------------------------
secure_workdir_init() {
    SECURE_WORKDIR="$(mktemp -d "$REMOTE_DIR/.pilot-XXXXXX")"
    chmod 700 "$SECURE_WORKDIR"
    STAGED_ENV="$SECURE_WORKDIR/staged.env"
}

secure_workdir_cleanup() {
    if [[ -n "$SECURE_WORKDIR" ]] && [[ -d "$SECURE_WORKDIR" ]]; then
        rm -rf "$SECURE_WORKDIR"
    fi
    SECURE_WORKDIR=""
    STAGED_ENV=""
    ROLLBACK_SNAPSHOT=""
}

common_exit_cleanup() {
    secure_workdir_cleanup
    cleanup_lock
}

# ---------------------------------------------------------------------------
# Per-host curl helpers
# ---------------------------------------------------------------------------
curl_host_status() {
    local host="$1" url="$2"
    local status
    status="$(curl --silent --show-error --connect-timeout 3 --max-time 10 \
        --output /dev/null --write-out '%{http_code}' \
        --resolve "${host}:443:127.0.0.1" "$url" 2>/dev/null || true)"
    [[ "$status" =~ ^[0-9]{3}$ ]] || status="000"
    printf '%s\n' "$status"
}

curl_host_body() {
    local host="$1" url="$2"
    curl --fail --silent --show-error --connect-timeout 3 --max-time 10 \
        --resolve "${host}:443:127.0.0.1" "$url" 2>/dev/null || true
}

assert_http_status() {
    local host="$1" url="$2" expected="$3" label="$4"
    local actual attempts
    for attempts in $(seq 1 15); do
        actual="$(curl_host_status "$host" "$url")"
        if [[ "$actual" == "$expected" ]]; then
            return 0
        fi
        sleep 1
    done
    echo "ERROR: $label expected $expected, got $actual" >&2
    return 1
}

# Read the current value of FIN_TERMINAL_PUBLIC_ENABLED from .env.
# Returns exactly "true" or "false". Exits on parse failure.
read_public_enabled() {
    python3 - "$ENV_FILE" "$ENV_FLAG" <<'PYEOF'
import os, stat, sys

path, key = sys.argv[1:]
fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode):
        raise ValueError("not a regular file")
    if stat.S_IMODE(opened.st_mode) != 0o600:
        raise ValueError(".env mode must be 0600")
    if opened.st_uid != os.geteuid():
        raise ValueError(".env must be owned by the activation user")
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        fd = -1
        content = handle.read(1024 * 1024 + 1)
finally:
    if fd >= 0:
        os.close(fd)
if len(content.encode("utf-8")) > 1024 * 1024:
    raise ValueError(".env too large")

prefix = key + "="
values = []
for line in content.splitlines():
    if line.startswith(prefix):
        value = line[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values.append(value)
if len(values) != 1 or values[0] not in {"true", "false"}:
    raise SystemExit(f"ERROR: {key} must have exactly one true/false definition")
print(values[0])
PYEOF
}

# ---------------------------------------------------------------------------
# Atomic .env updater — embedded Python with full safety (review item A).
# ---------------------------------------------------------------------------
update_env_flag() {
    local env_path="$1" key="$2" value="$3"
    python3 - "$env_path" "$key" "$value" <<'PYEOF'
import os, stat, sys, tempfile

ENV_MAX_BYTES = 1024 * 1024

def read_env(path_str):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path_str, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError('not a regular file')
        data = b''
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
            if len(data) > ENV_MAX_BYTES:
                raise ValueError('.env too large')
        os.close(fd)
        fd = -1
    finally:
        if fd >= 0:
            os.close(fd)
    return data.decode('utf-8'), st

def validate_no_duplicate(lines, key):
    prefix = key + '='
    count = 0
    for line in lines:
        if line.startswith(prefix):
            count += 1
            if count > 1:
                raise ValueError(f'Duplicate {key} definition in .env')
    return count > 0

def apply_flag(lines, key, value):
    prefix = key + '='
    found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped == '' or stripped.startswith('#'):
            new_lines.append(line)
            continue
        if line.startswith(prefix):
            if found:
                raise ValueError(f'Duplicate {key}')
            found = True
            new_lines.append(f'{key}={value}\n')
        else:
            new_lines.append(line)
    if not found:
        if new_lines and not new_lines[-1].endswith(('\n', '\r')):
            new_lines[-1] += '\n'
        new_lines.append(f'{key}={value}\n')
    validate_no_duplicate(new_lines, key)
    # Verify final content.
    for l in new_lines:
        if l.startswith(prefix):
            raw = l[len(prefix):].strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
                raw = raw[1:-1]
            if raw != value:
                raise ValueError(f'{key} value mismatch after apply')
    return new_lines

def atomic_write(path_str, lines, expected_st):
    # Recheck inode/dev before mutating.
    cur_st = os.stat(path_str, follow_symlinks=False)
    if cur_st.st_dev != expected_st.st_dev or cur_st.st_ino != expected_st.st_ino:
        raise ValueError('.env changed before write')
    if not stat.S_ISREG(cur_st.st_mode):
        raise ValueError('.env not regular file')

    content = ''.join(lines)
    directory = os.path.dirname(path_str) or '.'
    dir_fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    tmp_path = None
    try:
        # mkstemp in same directory, mode 0600.
        fd_tmp = -1
        try:
            fd_tmp, tmp_path = tempfile.mkstemp(
                prefix='.env.', dir=directory)
            os.fchmod(fd_tmp, 0o600)
            written = 0
            data = content.encode('utf-8')
            while written < len(data):
                n = os.write(fd_tmp, data[written:])
                if n <= 0:
                    raise OSError('short write')
                written += n
            os.fsync(fd_tmp)
            os.close(fd_tmp)
            fd_tmp = -1
        finally:
            if fd_tmp >= 0:
                os.close(fd_tmp)

        # os.replace is atomic on same filesystem.
        os.replace(tmp_path, path_str)
        tmp_path = None
        # fsync the directory to persist the rename.
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass

    # Final no-follow verification. The replacement was already mode 0600.
    final_st = os.stat(path_str, follow_symlinks=False)
    if not stat.S_ISREG(final_st.st_mode):
        raise ValueError('.env not regular after write')
    if stat.S_IMODE(final_st.st_mode) != 0o600:
        raise ValueError('.env mode is not 0600 after write')

def main():
    env_path = sys.argv[1]
    key = sys.argv[2]
    value = sys.argv[3]
    content, st = read_env(env_path)
    lines = content.splitlines(True)
    validate_no_duplicate(lines, key)
    new_lines = apply_flag(lines, key, value)
    atomic_write(env_path, new_lines, st)

main()
PYEOF
}

# ---------------------------------------------------------------------------
# Env snapshot / restore (for rollback)
# ---------------------------------------------------------------------------
snapshot_env() {
    local dest="$1"
    cp -p "$ENV_FILE" "$dest"
    chmod 600 "$dest"
}

restore_env_from_snapshot() {
    local src="$1"
    # Use Python to atomically restore the full file.
    python3 - "$ENV_FILE" "$src" <<'PYEOF'
import os, stat, sys, tempfile

def atomic_restore(env_path, src_path):
    source_fd = os.open(src_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        src_st = os.fstat(source_fd)
        if not stat.S_ISREG(src_st.st_mode):
            raise ValueError('source not regular')
        content = b''
        while True:
            chunk = os.read(source_fd, 65536)
            if not chunk:
                break
            content += chunk
            if len(content) > 1024 * 1024:
                raise ValueError('source .env too large')
    finally:
        os.close(source_fd)

    tgt_st = os.stat(env_path, follow_symlinks=False)
    if not stat.S_ISREG(tgt_st.st_mode):
        raise ValueError('target not regular')

    directory = os.path.dirname(env_path) or '.'
    dir_fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    tmp_path = None
    try:
        fd_tmp, tmp_path = tempfile.mkstemp(
            prefix='.env.', dir=directory)
        try:
            os.fchmod(fd_tmp, 0o600)
            written = 0
            while written < len(content):
                n = os.write(fd_tmp, content[written:])
                if n <= 0:
                    raise OSError('short write')
                written += n
            os.fsync(fd_tmp)
            os.close(fd_tmp)
            fd_tmp = -1
        finally:
            if fd_tmp >= 0:
                os.close(fd_tmp)
        current = os.stat(env_path, follow_symlinks=False)
        if current.st_dev != tgt_st.st_dev or current.st_ino != tgt_st.st_ino:
            raise ValueError('target .env changed before restore')
        os.replace(tmp_path, env_path)
        tmp_path = None
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
    final = os.stat(env_path, follow_symlinks=False)
    if not stat.S_ISREG(final.st_mode) or stat.S_IMODE(final.st_mode) != 0o600:
        raise ValueError('restored .env is unsafe')

atomic_restore(sys.argv[1], sys.argv[2])
PYEOF
}

# ---------------------------------------------------------------------------
# Compose helpers
# ---------------------------------------------------------------------------
compose_cmd() {
    docker compose --project-name "$COMPOSE_PROJECT" "${COMPOSE_ARGS[@]}" "$@"
}

# Resolve container ID for a Compose service (uses ps -q, not literal name).
resolve_container_id() {
    local service="$1"
    compose_cmd ps -q "$service" 2>/dev/null || true
}

container_state() {
    local service="$1" container
    container="$(compose_cmd ps -q "$service" 2>/dev/null || true)"
    [[ -n "$container" ]] || { printf '%s' "absent"; return 0; }
    docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
        "$container" 2>/dev/null || printf '%s' "unknown"
}

wait_healthy() {
    local service="$1" attempts state
    for attempts in $(seq 1 40); do
        state="$(container_state "$service")"
        if [[ "$state" == "healthy" ]]; then
            return 0
        fi
        if [[ "$state" == "unhealthy" || "$state" == "exited" || "$state" == "dead" ]]; then
            compose_cmd logs --tail 30 "$service" >&2 || true
            return 1
        fi
        sleep 2
    done
    echo "ERROR: timed out waiting for $service healthy (last state: $state)" >&2
    compose_cmd logs --tail 30 "$service" >&2 || true
    return 1
}

pilot_any_container_present() {
    local svc
    for svc in "${PILOT_SERVICES[@]}"; do
        local container
        container="$(compose_cmd ps -aq "$svc" 2>/dev/null || true)"
        if [[ -n "$container" ]]; then
            return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------------------
# Caddy helpers
# ---------------------------------------------------------------------------
caddy_validate() {
    docker compose --project-name "$COMPOSE_PROJECT" \
        --env-file "$STAGED_ENV" -f docker-compose.yml \
        run --rm --no-deps --pull never \
        --entrypoint caddy caddy \
        validate --config /etc/caddy/Caddyfile --adapter caddyfile \
        >/dev/null 2>&1
}

caddy_force_recreate() {
    docker compose --project-name "$COMPOSE_PROJECT" -f docker-compose.yml up -d \
        --no-deps --no-build --pull never --force-recreate caddy \
        >/dev/null 2>&1
}

wait_caddy_running() {
    local attempts container state
    for attempts in $(seq 1 20); do
        container="$(docker compose --project-name "$COMPOSE_PROJECT" \
            -f docker-compose.yml ps -q caddy 2>/dev/null || true)"
        if [[ -z "$container" ]]; then
            sleep 1
            continue
        fi
        state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
            "$container" 2>/dev/null || true)"
        if [[ "$state" == "running" || "$state" == "healthy" ]]; then
            return 0
        fi
        sleep 1
    done
    return 1
}

stop_caddy_fail_closed() {
    echo "FATAL: stopping Caddy because the disabled edge state could not be verified" >&2
    if ! docker compose --project-name "$COMPOSE_PROJECT" \
            -f docker-compose.yml stop caddy >/dev/null 2>&1; then
        echo "FATAL: graceful Caddy stop failed; attempting forced container removal" >&2
        local caddy_id
        caddy_id="$(docker compose --project-name "$COMPOSE_PROJECT" \
            -f docker-compose.yml ps -aq caddy 2>/dev/null || true)"
        if [[ "$caddy_id" =~ ^[0-9a-f]{12,64}$ ]]; then
            # Removal, unlike `docker kill`, cannot be undone by restart policy.
            docker rm -f "$caddy_id" >/dev/null 2>&1 || true
        fi
    fi

    local remaining_id remaining_running
    remaining_id="$(docker compose --project-name "$COMPOSE_PROJECT" \
        -f docker-compose.yml ps -aq caddy 2>/dev/null || true)"
    if [[ "$remaining_id" =~ ^[0-9a-f]{12,64}$ ]]; then
        remaining_running="$(docker inspect --format '{{.State.Running}}' \
            "$remaining_id" 2>/dev/null || true)"
        if [[ "$remaining_running" == "true" ]]; then
            docker rm -f "$remaining_id" >/dev/null 2>&1 || true
        fi
    fi

    remaining_id="$(docker compose --project-name "$COMPOSE_PROJECT" \
        -f docker-compose.yml ps -q caddy 2>/dev/null || true)"
    if [[ -n "$remaining_id" ]]; then
        echo "FATAL: Caddy may still be running; immediate operator intervention is required" >&2
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Pre-flight guards
# ---------------------------------------------------------------------------
cd "$REMOTE_DIR"

if [[ ! -f "docker-compose.yml" ]]; then
    echo "ERROR: docker-compose.yml not found in $REMOTE_DIR" >&2
    exit 1
fi
if [[ ! -f "docker-compose.public-terminal.yml" ]]; then
    echo "ERROR: docker-compose.public-terminal.yml not found in $REMOTE_DIR" >&2
    exit 1
fi
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: production .env not found" >&2
    exit 1
fi
if [[ -L "$ENV_FILE" ]]; then
    echo "ERROR: production .env is a symlink" >&2
    exit 1
fi

# Verify the deployed revision only after acquiring the shared deployment lock.
# This closes the check/lock race with the normal production deploy workflow.
verify_deployed_revision() {
    if [[ ! -f "$DEPLOY_CURRENT" ]] || [[ -L "$DEPLOY_CURRENT" ]]; then
        echo "ERROR: .deploy-current not found or unsafe; deploy the main stack first" >&2
        return 1
    fi
    local deployed_revision
    deployed_revision="$(awk -F= '/^revision=/ { print $2; exit }' "$DEPLOY_CURRENT")"
    if [[ "$deployed_revision" != "$EXPECTED_SHA" ]]; then
        echo "ERROR: deployed revision ($deployed_revision) does not match expected ($EXPECTED_SHA)" >&2
        return 1
    fi
}

verify_current_main_revision() {
    [[ "$ACTION" == "activate" ]] || return 0
    local remote_ref latest_main
    if ! remote_ref="$(git ls-remote --exit-code "$MAIN_REPO_URL" \
            refs/heads/main 2>/dev/null)"; then
        echo "ERROR: could not verify the current protected main revision" >&2
        return 1
    fi
    read -r latest_main _ <<<"$remote_ref"
    if [[ ! "$latest_main" =~ ^[0-9a-f]{40}$ ]] \
        || [[ "$latest_main" != "$EXPECTED_SHA" ]]; then
        echo "ERROR: activation revision is no longer current main" >&2
        return 1
    fi
}

# Exact set-difference validation using config --format json (review item E).
validate_overlay_services() {
    local merged_services base_services diff_services
    if ! merged_services="$(compose_cmd config --format json 2>/dev/null \
            | python3 -c "import json,sys; cfg=json.load(sys.stdin); print('\n'.join(sorted(cfg.get('services',{}).keys())))" 2>/dev/null)"; then
        echo "ERROR: could not render merged public-terminal Compose configuration" >&2
        return 1
    fi
    if ! base_services="$(docker compose --project-name "$COMPOSE_PROJECT" \
            -f docker-compose.yml config --format json 2>/dev/null \
            | python3 -c "import json,sys; cfg=json.load(sys.stdin); print('\n'.join(sorted(cfg.get('services',{}).keys())))" 2>/dev/null)"; then
        echo "ERROR: could not render base Compose configuration" >&2
        return 1
    fi

    # Compute set difference.
    diff_services="$(comm -13 \
        <(printf '%s\n' "$base_services") \
        <(printf '%s\n' "$merged_services"))"
    local expected_list
    expected_list="$(printf '%s\n' "${PILOT_SERVICES[@]}" | sort)"

    if [[ "$diff_services" != "$expected_list" ]]; then
        echo "ERROR: overlay does not contribute exactly these profile services:" >&2
        echo "  expected: ${PILOT_SERVICES[*]}" >&2
        echo "  got: $(printf '%s\n' "$diff_services" | tr '\n' ' ')" >&2
        return 1
    fi
    echo "    Overlay services: exact match."
    return 0
}

# ---------------------------------------------------------------------------
# Rollback trap — fail-closed for activate (review item K)
# ---------------------------------------------------------------------------
arm_activate_rollback() {
    ROLLBACK_ARMED=true
    trap 'activate_rollback_handler' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'exit 129' HUP
}

activate_rollback_handler() {
    local exit_code=$?
    set +e
    trap - EXIT INT TERM HUP
    ROLLBACK_ARMED=false

    if [[ "$exit_code" -eq 0 ]]; then
        secure_workdir_cleanup
        return 0
    fi

    echo "==> [ROLLBACK] Activate failed (exit $exit_code); restoring fail-closed state..." >&2

    # Step 1: Restore exact preactivation .env snapshot atomically (mode 0600).
    if [[ -n "$ROLLBACK_SNAPSHOT" ]] && [[ -f "$ROLLBACK_SNAPSHOT" ]]; then
        printf 'Rollback: restoring preactivation .env snapshot...\n' >&2
        restore_env_from_snapshot "$ROLLBACK_SNAPSHOT" || {
            echo "FATAL: could not restore .env from snapshot" >&2
            stop_caddy_fail_closed
            secure_workdir_cleanup
            exit 1
        }
    fi

    # Step 2: Stage .env for Caddy validation.
    cp -p "$ENV_FILE" "$STAGED_ENV" 2>/dev/null || true
    chmod 600 "$STAGED_ENV" 2>/dev/null || true

    # Step 3: Validate and force-recreate Caddy.
    if ! caddy_validate; then
        echo "FATAL: Caddy validation failed during rollback; stopping Caddy" >&2
        stop_caddy_fail_closed
        secure_workdir_cleanup
        exit 1
    fi
    if ! caddy_force_recreate; then
        echo "FATAL: Caddy recreate failed during rollback" >&2
        stop_caddy_fail_closed
        secure_workdir_cleanup
        exit 1
    fi
    if ! wait_caddy_running; then
        echo "FATAL: Caddy did not reach running during rollback" >&2
        stop_caddy_fail_closed
        secure_workdir_cleanup
        exit 1
    fi

    # Step 4: Confirm pilot 404.
    if ! assert_http_status "$PUBLIC_HOST" "$PILOT_URL" "404" "rollback pilot 404"; then
        echo "FATAL: cannot prove pilot 404 after rollback; stopping Caddy" >&2
        stop_caddy_fail_closed
        secure_workdir_cleanup
        exit 1
    fi

    # Step 5: Stop/remove pilot services in reverse order (use ps -aq).
    printf 'Rollback: stopping/removing pilot services...\n' >&2
    for svc in "${PILOT_STOP_ORDER[@]}"; do
        compose_cmd stop "$svc" 2>/dev/null || true
    done
    for svc in "${PILOT_STOP_ORDER[@]}"; do
        compose_cmd rm -f "$svc" 2>/dev/null || true
    done

    # Confirm absent.
    if pilot_any_container_present; then
        echo "FATAL: pilot containers still present after rollback stop" >&2
        secure_workdir_cleanup
        exit 1
    fi

    # Clean up secure temp.
    secure_workdir_cleanup
    echo "==> [ROLLBACK] Fail-closed state restored." >&2
    exit "$exit_code"
}

# ---------------------------------------------------------------------------
# Validate overlay safety: seat count, pilot-services-only port/host checks
# ---------------------------------------------------------------------------
validate_overlay_safety() {
    echo "==> Validating overlay safety..."

    # Exactly one seat.
    local seat_count
    if ! seat_count="$(compose_cmd config --format json 2>/dev/null \
            | python3 -c "import json,sys; cfg=json.load(sys.stdin); services=list(cfg.get('services',{}).keys()); print(sum(1 for s in services if s.startswith('fin-terminal-public-seat-')))" 2>/dev/null)"; then
        echo "ERROR: could not inspect seat count in merged Compose configuration" >&2
        return 1
    fi
    if [[ "$seat_count" -ne 1 ]]; then
        echo "ERROR: expected exactly 1 seat, found $seat_count" >&2
        return 1
    fi

    # Filter checks to pilot services only.
    local pilot_json
    if ! pilot_json="$(compose_cmd config --format json 2>/dev/null \
        | python3 -c "
import json, sys
cfg = json.load(sys.stdin)
pilot = ['fin-terminal-public-redis','fin-terminal-public-unbrowser-mcp','fin-terminal-public-seat-01','fin-terminal-public-gateway']
out = {}
for name in pilot:
    if name in cfg.get('services',{}):
        svc = cfg['services'][name]
        out[name] = {
            'ports': svc.get('ports',[]),
            'network_mode': svc.get('network_mode',''),
            'privileged': svc.get('privileged',False),
            'pid': svc.get('pid',''),
            'ipc': svc.get('ipc',''),
            'cap_add': svc.get('cap_add',[]),
            'devices': svc.get('devices',[]),
            'read_only': svc.get('read_only',False),
            'security_opt': svc.get('security_opt',[]),
            'volumes': svc.get('volumes',[]),
        }
print(json.dumps(out))
" 2>/dev/null)"; then
        echo "ERROR: could not inspect pilot services in merged Compose configuration" >&2
        return 1
    fi

    # Check for published ports.
    local has_published
    if ! has_published="$(python3 -c "
import json, sys
data = json.load(sys.stdin)
if len(data) != 4:
    raise SystemExit(1)
for svc in data.values():
    if svc.get('ports'):
        print('PUBLISHED')
        sys.exit(0)
" <<<"$pilot_json" 2>/dev/null)"; then
        echo "ERROR: pilot service port validation could not complete" >&2
        return 1
    fi
    if [[ -n "$has_published" ]]; then
        echo "ERROR: pilot services have published host ports" >&2
        return 1
    fi

    # Check for host networking.
    local has_host
    if ! has_host="$(python3 -c "
import json, sys
data = json.load(sys.stdin)
for svc in data.values():
    if svc.get('network_mode') == 'host':
        print('HOST')
        sys.exit(0)
" <<<"$pilot_json" 2>/dev/null)"; then
        echo "ERROR: pilot host-network validation could not complete" >&2
        return 1
    fi
    if [[ -n "$has_host" ]]; then
        echo "ERROR: pilot services use host networking" >&2
        return 1
    fi

    # Reject high-risk runtime privileges and host bind mounts even if the
    # deployed Compose files were modified without updating release metadata.
    local hardening_result
    if ! hardening_result="$(python3 -c "
import json, sys
data = json.load(sys.stdin)
for name, svc in data.items():
    unsafe = (
        svc.get('privileged') is True
        or svc.get('pid') == 'host'
        or svc.get('ipc') == 'host'
        or bool(svc.get('cap_add'))
        or bool(svc.get('devices'))
        or svc.get('read_only') is not True
        or 'no-new-privileges:true' not in svc.get('security_opt', [])
        or any(volume.get('type') == 'bind' for volume in svc.get('volumes', []))
    )
    if unsafe:
        raise SystemExit(1)
print('HARDENED')
" <<<"$pilot_json" 2>/dev/null)"; then
        echo "ERROR: pilot services fail the reviewed runtime-hardening contract" >&2
        return 1
    fi
    if [[ "$hardening_result" != "HARDENED" ]]; then
        echo "ERROR: pilot runtime-hardening validation returned an unexpected result" >&2
        return 1
    fi

    echo "    Overlay safety: OK (1 seat, no published ports/host networking, hardened runtimes)."
    return 0
}

# ---------------------------------------------------------------------------
# Unbrowser-egress health
# ---------------------------------------------------------------------------
check_unbrowser_egress() {
    local state
    state="$(docker compose --project-name "$COMPOSE_PROJECT" \
        -f docker-compose.yml ps -q unbrowser-egress 2>/dev/null || true)"
    if [[ -z "$state" ]]; then
        echo "ERROR: unbrowser-egress is not running" >&2
        return 1
    fi
    state="$(container_state unbrowser-egress)"
    if [[ "$state" != "healthy" ]]; then
        echo "ERROR: unbrowser-egress is not healthy (state: $state)" >&2
        return 1
    fi
    echo "    unbrowser-egress: healthy."
    return 0
}

# ---------------------------------------------------------------------------
# Resource checks
# ---------------------------------------------------------------------------
check_resources() {
    local disk_pct mem_pct
    disk_pct="$(df --output=pcent "$REMOTE_DIR" 2>/dev/null | tail -1 | tr -d ' %' || echo 0)"
    if [[ "$disk_pct" -gt 85 ]]; then
        echo "ERROR: disk usage at ${disk_pct}% (threshold: 85%)" >&2
        return 1
    fi
    local mem_free_kb mem_total_kb
    mem_free_kb="$(awk '/MemAvailable/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
    mem_total_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 1)"
    if [[ "$mem_total_kb" -gt 0 ]]; then
        mem_pct=$(( (mem_total_kb - mem_free_kb) * 100 / mem_total_kb ))
        if [[ "$mem_pct" -gt 90 ]]; then
            echo "ERROR: memory usage at ${mem_pct}% (threshold: 90%)" >&2
            return 1
        fi
    fi
    echo "    Resources: sufficient (disk ${disk_pct}%, memory ${mem_pct:-?}%)."
    return 0
}

# ---------------------------------------------------------------------------
# Clean partial/leftover pilot containers (ps -aq)
# ---------------------------------------------------------------------------
clean_partial_pilot() {
    local any=false svc
    for svc in "${PILOT_STOP_ORDER[@]}"; do
        local container
        container="$(compose_cmd ps -aq "$svc" 2>/dev/null || true)"
        if [[ -n "$container" ]]; then
            echo "    Removing leftover container: $svc"
            compose_cmd stop "$svc" 2>/dev/null || true
            compose_cmd rm -f "$svc" 2>/dev/null || true
            any=true
        fi
    done
    if $any; then
        echo "    Cleaned partial pilot containers."
    else
        echo "    No partial pilot containers found."
    fi
}

# ---------------------------------------------------------------------------
# Check retired demo URLs 404 (public host + legacy primary host aliases)
# ---------------------------------------------------------------------------
check_retired_demo_404() {
    local status
    echo "    Checking retired demo URLs return 404..."
    local paths=(
        "$PUBLIC_HOST:/fin-terminal-demo/"
        "$PUBLIC_HOST:/fin-terminal-demo"
        "$PUBLIC_HOST:/fin-terminal-demo/ws"
        "$PRIMARY_HOST:/unbrowser/fin-terminal-demo/"
        "$PRIMARY_HOST:/unbrowser/fin-terminal-demo"
        "$PRIMARY_HOST:/unbrowser/fin-terminal/demo/"
        "$PRIMARY_HOST:/unbrowser/fin-terminal/demo"
    )
    for entry in "${paths[@]}"; do
        local host="${entry%%:*}"
        local path="${entry#*:}"
        status="$(curl_host_status "$host" "https://${host}${path}")"
        if [[ "$status" != "404" ]]; then
            echo "ERROR: retired demo path https://${host}${path} returned $status (expected 404)" >&2
            return 1
        fi
    done
    echo "    Retired demo URLs: all 404."
    return 0
}

# ---------------------------------------------------------------------------
# Check no stale demo container (review item G: use Docker labels)
# ---------------------------------------------------------------------------
check_no_stale_demo() {
    local containers
    containers="$(docker ps -aq --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
        --filter "label=com.docker.compose.service=fin-terminal-demo" 2>/dev/null || true)"
    if [[ -n "$containers" ]]; then
        echo "ERROR: stale fin-terminal-demo container(s) found by Docker labels" >&2
        return 1
    fi
    echo "    Stale demo container: absent (label check)."
    return 0
}

# ---------------------------------------------------------------------------
# Credentials stability (review item H: run against temp copy)
# ---------------------------------------------------------------------------
check_credentials_stable() {
    local temp_copy result
    temp_copy="$(mktemp "$SECURE_WORKDIR/.env.credcheck.XXXXXX")"
    cp -p "$ENV_FILE" "$temp_copy"
    chmod 600 "$temp_copy"

    if ! result="$(python3 "$REMOTE_DIR/.deploy-tools/ensure_fin_terminal_secrets.py" \
            --ensure-status "$temp_copy" 2>/dev/null)"; then
        rm -f "$temp_copy"
        echo "ERROR: credentials helper validation failed" >&2
        return 1
    fi
    rm -f "$temp_copy"

    case "$result" in
        fin_terminal_credentials_changed=false)
            echo "    Credentials helper: reports no generation needed."
            ;;
        *)
            echo "ERROR: credentials helper did not confirm stable credentials" >&2
            return 1
            ;;
    esac
    return 0
}

# ---------------------------------------------------------------------------
# Activate gates
# ---------------------------------------------------------------------------
run_activate_gates() {
    echo "==> Activate gate checks..."
    local enabled
    enabled="$(read_public_enabled)"
    if [[ "$enabled" != "false" ]]; then
        echo "ERROR: $ENV_FLAG is '$enabled', must be 'false' before activation" >&2
        return 1
    fi
    echo "    $ENV_FLAG: false."
    check_no_stale_demo || return 1
    check_credentials_stable || return 1
    check_retired_demo_404 || return 1

    echo "    Checking pilot URL 404 before activation..."
    if ! assert_http_status "$PUBLIC_HOST" "$PILOT_URL" "404" "pre-activation pilot 404"; then
        echo "ERROR: pilot URL is not 404 before activation" >&2
        return 1
    fi
    echo "    Pilot URL: currently 404."

    validate_overlay_services || return 1
    validate_overlay_safety || return 1
    check_unbrowser_egress || return 1
    check_resources || return 1

    echo "==> All activate gates passed."
    return 0
}

# ---------------------------------------------------------------------------
# Runtime service-set, health, host-port, and network-isolation verification
# ---------------------------------------------------------------------------
validate_runtime_pilot() {
    # Verify exactly four healthy containers with unambiguous resolved IDs.
    local svc cid count=0
    for svc in "${PILOT_SERVICES[@]}"; do
        cid="$(resolve_container_id "$svc")"
        if [[ ! "$cid" =~ ^[0-9a-f]{12,64}$ ]]; then
            echo "ERROR: $svc did not resolve to exactly one Docker container ID" >&2
            return 1
        fi
        if [[ "$(container_state "$svc")" != "healthy" ]]; then
            echo "ERROR: $svc is not healthy during runtime verification" >&2
            return 1
        fi
        count=$((count + 1))
    done
    if [[ "$count" -ne 4 ]]; then
        echo "ERROR: expected 4 pilot containers, found $count" >&2
        return 1
    fi
    echo "    Pilot containers: 4/4 present."

    # Runtime PortBindings check (not exposed null).
    local port_bindings
    port_bindings="$(for svc in "${PILOT_SERVICES[@]}"; do
        cid="$(resolve_container_id "$svc")"
        [[ -n "$cid" ]] || continue
        docker inspect --format '{{range $hp, $bindings := .NetworkSettings.Ports}}{{$hp}}:{{len $bindings}}{{"\n"}}{{end}}' "$cid" 2>/dev/null
    done | grep -v ':0$' || true)"
    if [[ -n "$port_bindings" ]]; then
        echo "ERROR: pilot containers have active port bindings" >&2
        printf '%s\n' "$port_bindings" >&2
        return 1
    fi
    echo "    Host ports: none published."

    # Verify no unexpected public-profile service/container exists.
    local runtime_public_services expected_public_services
    runtime_public_services="$(docker ps -a \
        --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
        --format '{{.Label "com.docker.compose.service"}}' 2>/dev/null \
        | grep '^fin-terminal-public-' | sort -u || true)"
    expected_public_services="$(printf '%s\n' "${PILOT_SERVICES[@]}" | sort)"
    if [[ "$runtime_public_services" != "$expected_public_services" ]]; then
        echo "ERROR: runtime public-profile service set is not the reviewed four-service set" >&2
        return 1
    fi

    # Verify exact runtime network attachments for every pilot service.
    local actual_networks expected_networks
    for svc in "${PILOT_SERVICES[@]}"; do
        cid="$(resolve_container_id "$svc")"
        actual_networks="$(docker inspect \
            --format '{{json .NetworkSettings.Networks}}' "$cid" 2>/dev/null \
            | python3 -c 'import json,sys; data=json.load(sys.stdin); print("\n".join(sorted(data)))')"
        case "$svc" in
            fin-terminal-public-redis)
                expected_networks="${COMPOSE_PROJECT}_fin_terminal_public_state"
                ;;
            fin-terminal-public-unbrowser-mcp)
                expected_networks="$(printf '%s\n' \
                    "${COMPOSE_PROJECT}_fin_terminal_public_mcp" \
                    "${COMPOSE_PROJECT}_unbrowser_egress_proxy" | sort)"
                ;;
            fin-terminal-public-seat-01)
                expected_networks="$(printf '%s\n' \
                    "${COMPOSE_PROJECT}_fin_terminal_public" \
                    "${COMPOSE_PROJECT}_fin_terminal_public_egress" \
                    "${COMPOSE_PROJECT}_fin_terminal_public_mcp" | sort)"
                ;;
            fin-terminal-public-gateway)
                expected_networks="$(printf '%s\n' \
                    "${COMPOSE_PROJECT}_fin_terminal_public" \
                    "${COMPOSE_PROJECT}_fin_terminal_public_egress" \
                    "${COMPOSE_PROJECT}_fin_terminal_public_state" | sort)"
                ;;
            *)
                echo "ERROR: unexpected pilot service during network verification" >&2
                return 1
                ;;
        esac
        if [[ "$actual_networks" != "$expected_networks" ]]; then
            echo "ERROR: $svc runtime network attachments differ from reviewed isolation" >&2
            return 1
        fi
    done
    echo "    Runtime service set and network isolation: verified."

    return 0
}

# ---------------------------------------------------------------------------
# Build and start pilot services
# ---------------------------------------------------------------------------
build_and_start_pilot() {
    echo "==> Building and starting pilot services..."
    compose_cmd pull fin-terminal-public-redis || return 1
    compose_cmd build \
        fin-terminal-public-unbrowser-mcp \
        fin-terminal-public-seat-01 \
        fin-terminal-public-gateway || return 1

    echo "    Starting Redis..."
    compose_cmd up -d --no-build fin-terminal-public-redis || return 1
    wait_healthy fin-terminal-public-redis || return 1
    echo "    Redis: healthy."

    echo "    Starting MCP..."
    compose_cmd up -d --no-deps --no-build fin-terminal-public-unbrowser-mcp || return 1
    wait_healthy fin-terminal-public-unbrowser-mcp || return 1
    echo "    MCP: healthy."

    echo "    Starting seat-01..."
    compose_cmd up -d --no-deps --no-build fin-terminal-public-seat-01 || return 1
    wait_healthy fin-terminal-public-seat-01 || return 1
    echo "    Seat-01: healthy."

    echo "    Starting gateway..."
    compose_cmd up -d --no-deps --no-build fin-terminal-public-gateway || return 1
    wait_healthy fin-terminal-public-gateway || return 1
    echo "    Gateway: healthy."

    validate_runtime_pilot || return 1

    echo "==> Pilot services healthy and ready."
    return 0
}

# ---------------------------------------------------------------------------
# MCP protocol check (review item C)
# ---------------------------------------------------------------------------
mcp_protocol_check() {
    echo "    Running MCP protocol check..."
    local mcp_cid
    mcp_cid="$(resolve_container_id fin-terminal-public-unbrowser-mcp)"
    if [[ -z "$mcp_cid" ]]; then
        echo "ERROR: could not resolve MCP container ID" >&2
        return 1
    fi

    # Never print session identifiers, provider data, or raw protocol output.
    local result
    result="$(docker exec "$mcp_cid" python3 -c '
import http.client, json, sys

PROTOCOL = "2025-06-18"

def decode_response(data, content_type):
    if not data:
        return None
    if "text/event-stream" in content_type:
        for line in data.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise ValueError("SSE response contained no data event")
    return json.loads(data)

def post(payload, session_id=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["MCP-Session-Id"] = session_id
        headers["MCP-Protocol-Version"] = PROTOCOL
    connection = http.client.HTTPConnection("localhost", 8767, timeout=20)
    connection.request("POST", "/mcp", json.dumps(payload), headers)
    response = connection.getresponse()
    status = response.status
    returned_session = response.getheader("MCP-Session-Id", "")
    content_type = response.getheader("Content-Type", "")
    data = response.read().decode("utf-8")
    connection.close()
    return status, returned_session, decode_response(data, content_type)

def tool_http_error_status(message):
    if not isinstance(message, dict):
        return None
    content = message.get("result", {}).get("content", [])
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        status = (
            payload.get("blockmap", {})
            .get("density", {})
            .get("http_error_status")
        )
        if isinstance(status, int):
            return status
    return None

session_id = ""
cleanup_ok = False
try:
    status, session_id, initialized = post({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "pilot-check", "version": "1.0"},
        },
    })
    if status != 200 or not session_id or not isinstance(initialized, dict) or "result" not in initialized:
        raise ValueError("initialize failed")

    status, _, _ = post({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }, session_id)
    if status not in {200, 202, 204}:
        raise ValueError("initialized notification failed")

    status, _, tools = post({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }, session_id)
    names = [tool.get("name") for tool in tools.get("result", {}).get("tools", [])]
    if status != 200 or "navigate" not in names:
        raise ValueError("navigate tool missing")

    status, _, navigation = post({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "navigate", "arguments": {"url": "https://example.com/"}},
    }, session_id)
    result = navigation.get("result", {})
    if status != 200 or result.get("isError") is True or not result.get("content"):
        raise ValueError("public navigation failed")

    for request_id, target in enumerate((
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
    ), start=4):
        status, _, rejection = post({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": "navigate",
                "arguments": {"url": target},
            },
        }, session_id)
        # The navigate tool successfully returns a parsed blockmap for upstream
        # HTTP errors, so the egress denial is represented as an embedded 403
        # rather than a JSON-RPC/tool error.
        blocked_status = tool_http_error_status(rejection)
        if status != 200 or blocked_status != 403:
            raise ValueError("private target was not rejected")
finally:
    if session_id:
        try:
            connection = http.client.HTTPConnection("localhost", 8767, timeout=5)
            connection.request("DELETE", "/mcp", headers={
                "MCP-Session-Id": session_id,
                "MCP-Protocol-Version": PROTOCOL,
            })
            response = connection.getresponse()
            response.read()
            cleanup_ok = response.status in {200, 202, 204}
            connection.close()
        except Exception:
            cleanup_ok = False

if not cleanup_ok:
    raise SystemExit(1)
print("MCP_OK")
' 2>/dev/null)" || true

    if [[ "$result" != *"MCP_OK"* ]]; then
        echo "ERROR: MCP protocol check failed (scrubbed)" >&2
        printf '    (protocol output scrubbed for security)\n' >&2
        return 1
    fi
    echo "    MCP protocol: OK (navigate + private rejection + cleanup)."
    return 0
}

# ---------------------------------------------------------------------------
# Gateway /api/ready (review item D: resolve container ID, status "ready")
# ---------------------------------------------------------------------------
gateway_internal_ready() {
    echo "    Checking gateway /api/ready internally..."
    local gw_cid
    gw_cid="$(resolve_container_id fin-terminal-public-gateway)"
    if [[ -z "$gw_cid" ]]; then
        echo "ERROR: could not resolve gateway container ID" >&2
        return 1
    fi

    docker exec "$gw_cid" node -e '
const http = require("http");
const req = http.get("http://127.0.0.1:8788/api/ready", (res) => {
    if (res.statusCode < 200 || res.statusCode >= 300) { process.exit(1); }
    let data = "";
    res.on("data", c => data += c);
    res.on("end", () => {
        try {
            const obj = JSON.parse(data);
            if (obj && obj.status === "ready") { process.exit(0); }
        } catch(e) {}
        process.exit(1);
    });
});
req.on("error", () => process.exit(1));
req.setTimeout(5000, () => process.exit(1));
' 2>/dev/null || {
        echo "ERROR: gateway /api/ready failed internally" >&2
        return 1
    }
    echo "    Gateway /api/ready: ready."
    return 0
}

# Verify the already-enabled browser surface without changing Caddy or .env.
# This is used only for an idempotent re-run of the activate action.
verify_live_edge_surface() {
    assert_http_status "$PUBLIC_HOST" "$PILOT_URL" "200" "pilot slash" || return 1

    local slash_body
    slash_body="$(curl_host_body "$PUBLIC_HOST" "$PILOT_URL")"
    if ! grep -Fq 'name="x-build-mode" content="public-live"' <<<"$slash_body" \
        || ! grep -Fq '/fin-terminal-live-pilot/assets/' <<<"$slash_body"; then
        echo "ERROR: enabled pilot is missing its reviewed public-live frontend marker" >&2
        return 1
    fi

    local response_headers
    response_headers="$(curl --silent --show-error --connect-timeout 3 --max-time 10 \
        --dump-header - --output /dev/null \
        --resolve "${PUBLIC_HOST}:443:127.0.0.1" \
        "$PILOT_URL" 2>/dev/null || true)"
    if ! grep -Eiq '^content-security-policy:' <<<"$response_headers"; then
        echo "ERROR: enabled pilot response is missing Content-Security-Policy" >&2
        return 1
    fi

    assert_http_status "$PRIMARY_HOST" "https://${PRIMARY_HOST}/health" "200" "primary health" || return 1
    assert_http_status "$PUBLIC_HOST" "https://${PUBLIC_HOST}/" "200" "public root" || return 1
    assert_http_status "$PUBLIC_HOST" "https://${PUBLIC_HOST}/fin-terminal/" "401" "signed terminal" || return 1
    check_retired_demo_404 || return 1
    return 0
}

# ---------------------------------------------------------------------------
# Promote Caddy edge (review items B, I)
# ---------------------------------------------------------------------------
promote_edge() {
    echo "==> Promoting Caddy edge..."

    # Recheck protected main under the still-held host lock immediately before
    # any edge mutation. A main update during the potentially long image build
    # therefore rolls the new containers back while the route remains 404.
    verify_current_main_revision || return 1

    # Step 1: Snapshot preactivation .env in secure workdir.
    ROLLBACK_SNAPSHOT="$SECURE_WORKDIR/preactivate.env"
    snapshot_env "$ROLLBACK_SNAPSHOT"
    echo "    Pre-activation .env snapshot saved."

    # Step 2: Create staged .env with flag=true.
    cp -p "$ENV_FILE" "$STAGED_ENV"
    chmod 600 "$STAGED_ENV"
    update_env_flag "$STAGED_ENV" "$ENV_FLAG" "true" || {
        echo "ERROR: could not create staged .env with $ENV_FLAG=true" >&2
        return 1
    }
    echo "    Staged .env with $ENV_FLAG=true created."

    # Step 3: Validate Compose + Caddy against candidate (using --env-file, never swaps live).
    if ! docker compose --env-file "$STAGED_ENV" \
        -f docker-compose.yml -f docker-compose.public-terminal.yml \
        config --quiet >/dev/null 2>&1; then
        echo "ERROR: Compose config validation failed against staged .env" >&2
        return 1
    fi
    echo "    Compose validation: OK."

    if ! caddy_validate; then
        echo "ERROR: Caddy validation failed against staged .env" >&2
        return 1
    fi
    echo "    Caddy validation: OK."

    # Step 4: Atomically promote flag to true.
    update_env_flag "$ENV_FILE" "$ENV_FLAG" "true" || {
        echo "ERROR: atomic promote of $ENV_FLAG=true failed" >&2
        return 1
    }
    echo "    $ENV_FLAG: atomically set to true."

    # Step 5: Force-recreate only Caddy.
    if ! caddy_force_recreate; then
        echo "ERROR: Caddy force-recreate failed" >&2
        return 1
    fi
    if ! wait_caddy_running; then
        echo "ERROR: Caddy did not reach running state" >&2
        return 1
    fi
    echo "    Caddy: force-recreated and running."

    # Step 6: External release-blocking HTTP checks (review item I).
    echo "==> External release-blocking HTTP checks..."

    # Base 308 redirect.
    local redirect_check
    redirect_check="$(curl --silent --show-error --connect-timeout 3 --max-time 10 \
        --output /dev/null --write-out '%{http_code} %{redirect_url}' \
        --resolve "${PUBLIC_HOST}:443:127.0.0.1" \
        "https://${PUBLIC_HOST}${PILOT_PATH}" || true)"
    if [[ "$redirect_check" != "308 ${PILOT_URL}" ]]; then
        echo "ERROR: pilot base redirect (result: ${redirect_check:-request-failed})" >&2
        return 1
    fi
    echo "    Pilot base 308 redirect: OK."

    # Slash HTML 200: x-build-mode public-live + canonical asset prefix + CSP.
    if ! assert_http_status "$PUBLIC_HOST" "$PILOT_URL" "200" "pilot slash"; then
        return 1
    fi
    local slash_body
    slash_body="$(curl_host_body "$PUBLIC_HOST" "$PILOT_URL")"
    if [[ -z "$slash_body" ]]; then
        echo "ERROR: pilot slash returned empty body" >&2
        return 1
    fi
    if ! grep -Fq 'name="x-build-mode" content="public-live"' <<<"$slash_body" \
        || ! grep -Fq '/fin-terminal-live-pilot/assets/' <<<"$slash_body"; then
        echo "ERROR: pilot slash missing x-build-mode public-live marker" >&2
        return 1
    fi
    # CSP header present.
    local response_headers
    response_headers="$(curl --silent --show-error --connect-timeout 3 --max-time 10 \
        --dump-header - --output /dev/null \
        --resolve "${PUBLIC_HOST}:443:127.0.0.1" \
        "$PILOT_URL" 2>/dev/null || true)"
    if ! grep -Eiq '^content-security-policy:' <<<"$response_headers"; then
        echo "ERROR: pilot response is missing Content-Security-Policy" >&2
        return 1
    fi
    echo "    Pilot slash: 200 with public-live marker + CSP."

    # Primary health 200.
    if ! assert_http_status "$PRIMARY_HOST" "https://${PRIMARY_HOST}/health" "200" "primary health"; then
        return 1
    fi
    echo "    Primary health: 200."

    # Public root 200.
    if ! assert_http_status "$PUBLIC_HOST" "https://${PUBLIC_HOST}/" "200" "public root"; then
        return 1
    fi
    echo "    Public root: 200."

    # Signed terminal 401.
    if ! assert_http_status "$PUBLIC_HOST" "https://${PUBLIC_HOST}/fin-terminal/" "401" "signed terminal"; then
        return 1
    fi
    echo "    Signed terminal: 401."

    # Retired demo paths 404 (public host + legacy primary aliases).
    check_retired_demo_404 || return 1

    # Admission negative checks. A single in-memory client retains the opaque
    # visitor token; it is never placed in argv, a file, or output.
    echo "    Admission negative checks..."
    local admission_result
    admission_result="$(python3 - "$PUBLIC_HOST" "$PILOT_PATH" <<'PYEOF'
import http.client, json, socket, ssl, sys

host, base_path = sys.argv[1:]
origin = f"https://{host}"

class LocalHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        raw = socket.create_connection(("127.0.0.1", 443), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)

def request(method, path, body=None, headers=None):
    context = ssl._create_unverified_context()
    connection = LocalHTTPSConnection(host, timeout=10, context=context)
    payload = None if body is None else json.dumps(body)
    request_headers = dict(headers or {})
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    connection.request(method, path, payload, request_headers)
    response = connection.getresponse()
    status = response.status
    data = response.read(65_537)
    connection.close()
    if len(data) > 65_536:
        raise SystemExit(1)
    return status, data

admission_path = f"{base_path}/api/public/admission"
config_path = f"{base_path}/api/public/config"

status, _ = request("POST", admission_path, {"turnstileToken": ""}, {
    "Origin": "https://evil.example.com",
})
if status != 403:
    raise SystemExit(1)

status, _ = request("POST", admission_path, {"turnstileToken": ""}, {
    "Origin": origin,
})
if status != 401:
    raise SystemExit(1)

status, config_data = request("GET", config_path)
if status != 200:
    raise SystemExit(1)
public_config = json.loads(config_data)
if public_config.get("turnstileRequired") is not True:
    raise SystemExit(1)
if not isinstance(public_config.get("turnstileSiteKey"), str) or not public_config["turnstileSiteKey"]:
    raise SystemExit(1)
visitor = public_config.get("visitorToken")
if not isinstance(visitor, str) or not visitor:
    raise SystemExit(1)
visitor_headers = {
    "Origin": origin,
    "X-Public-Visitor-Token": visitor,
}

status, _ = request("POST", admission_path, {"turnstileToken": ""}, visitor_headers)
if status != 400:
    raise SystemExit(1)

status, _ = request("POST", admission_path, {"turnstileToken": "short"}, visitor_headers)
if status != 403:
    raise SystemExit(1)

print("ADMISSION_NEGATIVE_OK")
PYEOF
)" || true
    if [[ "$admission_result" != "ADMISSION_NEGATIVE_OK" ]]; then
        echo "ERROR: admission negative checks failed (details scrubbed)" >&2
        return 1
    fi
    echo "    Admission checks: all passed."

    echo "==> Edge promotion complete. All HTTP checks passed."
    return 0
}

# ---------------------------------------------------------------------------
# Action: status
# ---------------------------------------------------------------------------
cmd_status() {
    echo "==> Public Terminal Pilot Status"

    if [[ -f "$ENV_FILE" ]]; then
        echo "    .env: present"
    else
        echo "    .env: MISSING"
        return 1
    fi

    local enabled
    enabled="$(read_public_enabled)"
    echo "    $ENV_FLAG: $enabled"

    local svc state
    for svc in "${PILOT_SERVICES[@]}"; do
        state="$(container_state "$svc")"
        echo "    $svc: $state"
    done

    local edge_status
    edge_status="$(curl_host_status "$PUBLIC_HOST" "$PILOT_URL")"
    echo "    Edge ${PILOT_URL}: $edge_status"

    local caddy_state
    caddy_state="$(container_state caddy)"
    echo "    caddy: $caddy_state"

    local egress_state
    egress_state="$(container_state unbrowser-egress)"
    echo "    unbrowser-egress: $egress_state"

    local deployed_rev
    deployed_rev="$(awk -F= '/^revision=/ { print $2; exit }' "$DEPLOY_CURRENT" 2>/dev/null || echo "unknown")"
    echo "    deployed-revision: $deployed_rev"
}

# ---------------------------------------------------------------------------
# Action: activate
# ---------------------------------------------------------------------------
cmd_activate() {
    echo "==> Activating public-terminal pilot..."
    echo "    Expected revision: $EXPECTED_SHA"

    local enabled
    enabled="$(read_public_enabled)"
    if [[ "$enabled" == "true" ]]; then
        if validate_runtime_pilot \
            && mcp_protocol_check \
            && gateway_internal_ready \
            && verify_live_edge_surface; then
            echo "==> Pilot already active and healthy (idempotent success)."
            return 0
        fi
        echo "==> Pilot in degraded state; cleaning up fail-closed..."
        cmd_disable
        echo "==> Pilot disabled. Re-run activate when ready."
        return 1
    fi

    # Secure workdir.
    secure_workdir_init

    run_activate_gates || exit 1
    clean_partial_pilot
    arm_activate_rollback
    build_and_start_pilot || exit 1
    mcp_protocol_check || exit 1
    gateway_internal_ready || exit 1
    promote_edge || exit 1

    # Disarm rollback — success.
    trap - EXIT INT TERM HUP
    ROLLBACK_ARMED=false
    secure_workdir_cleanup
    echo "==> Public-terminal pilot activated successfully."
    echo "    Human Turnstile/session test should follow outside this script."
}

# ---------------------------------------------------------------------------
# Action: disable
# ---------------------------------------------------------------------------
cmd_disable() {
    echo "==> Disabling public-terminal pilot..."
    secure_workdir_init

    local enabled
    if ! enabled="$(read_public_enabled)"; then
        enabled="invalid"
    fi

    # Step 1: Atomically set flag to false.
    if [[ "$enabled" != "false" ]]; then
        echo "    Setting $ENV_FLAG=false..."
        update_env_flag "$ENV_FILE" "$ENV_FLAG" "false" || {
            echo "ERROR: could not set $ENV_FLAG=false" >&2
            stop_caddy_fail_closed
            exit 1
        }
        echo "    $ENV_FLAG: atomically set to false."
    else
        echo "    $ENV_FLAG: already false."
    fi

    # Stage env for Caddy validation.
    cp -p "$ENV_FILE" "$STAGED_ENV"
    chmod 600 "$STAGED_ENV"

    # Step 2: Validate and recreate Caddy.
    if ! caddy_validate; then
        echo "ERROR: Caddy validation failed" >&2
        stop_caddy_fail_closed
        exit 1
    fi
    if ! caddy_force_recreate; then
        echo "ERROR: Caddy force-recreate failed" >&2
        stop_caddy_fail_closed
        exit 1
    fi
    if ! wait_caddy_running; then
        echo "ERROR: Caddy did not reach running state" >&2
        stop_caddy_fail_closed
        exit 1
    fi
    echo "    Caddy: force-recreated and running."

    # Step 3: Confirm pilot 404.
    if ! assert_http_status "$PUBLIC_HOST" "$PILOT_URL" "404" "pilot 404"; then
        echo "ERROR: pilot URL is not 404 after disable" >&2
        stop_caddy_fail_closed
        exit 1
    fi
    echo "    Pilot URL: 404 confirmed."

    # Step 4: Stop/remove pilot services (ps -aq) in reverse order.
    if pilot_any_container_present; then
        echo "    Stopping pilot services..."
        for svc in "${PILOT_STOP_ORDER[@]}"; do
            compose_cmd stop "$svc" 2>/dev/null || true
        done
        echo "    Removing pilot containers..."
        for svc in "${PILOT_STOP_ORDER[@]}"; do
            compose_cmd rm -f "$svc" 2>/dev/null || true
        done
        echo "    Pilot containers: removed."
    else
        echo "    Pilot containers: already absent (idempotent)."
    fi
    if pilot_any_container_present; then
        echo "ERROR: pilot containers remain after disable" >&2
        exit 1
    fi

    # Step 5: Verify normal routes.
    echo "==> Verifying normal routes..."
    assert_http_status "$PRIMARY_HOST" "https://${PRIMARY_HOST}/health" "200" "health" || { secure_workdir_cleanup; exit 1; }
    echo "    Health: 200."
    assert_http_status "$PUBLIC_HOST" "https://${PUBLIC_HOST}/" "200" "root" || { secure_workdir_cleanup; exit 1; }
    echo "    Root: 200."
    assert_http_status "$PUBLIC_HOST" "https://${PUBLIC_HOST}/fin-terminal/" "401" "signed terminal" || { secure_workdir_cleanup; exit 1; }
    echo "    Signed terminal: 401."
    check_retired_demo_404 || { secure_workdir_cleanup; exit 1; }

    secure_workdir_cleanup
    echo "==> Public-terminal pilot disabled."
    echo "    Redis volume retained for diagnosis."
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
main() {
    trap 'common_exit_cleanup' EXIT
    acquire_lock
    verify_deployed_revision
    verify_current_main_revision

    case "$ACTION" in
        status)   cmd_status ;;
        activate) cmd_activate ;;
        disable)  cmd_disable ;;
    esac

    local exit_code=$?
    release_lock
    secure_workdir_cleanup
    trap - EXIT
    exit "$exit_code"
}

main
