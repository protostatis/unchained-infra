#!/bin/bash
# Production public-terminal pilot control — run on EC2 via verified SSH stdin.
#
# Usage (local caller):
#   ssh ... "bash -s -- ACTION EXPECTED_SHA RECONCILER_SHA UNIT_SHA"
#   ACTION       : activate-runtime | verify-runtime | disable | status
#   EXPECTED_SHA : exact 40-character lowercase host deployment revision
#   *_SHA        : exact SHA-256 of the host runtime artifacts from main
#
# Never transport Turnstile / OpenRouter values. The remote script reads the
# protected host .env directly during the transaction.

set -Eeuo pipefail

# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------
ACTION="${1:-}"
EXPECTED_SHA="${2:-}"
EXPECTED_RECONCILER_SHA="${3:-}"
EXPECTED_RECONCILER_UNIT_SHA="${4:-}"

readonly ACTION
readonly EXPECTED_SHA
readonly EXPECTED_RECONCILER_SHA
readonly EXPECTED_RECONCILER_UNIT_SHA

case "$ACTION" in
    activate-runtime|verify-runtime|disable|status|rollback) ;;
    *)
        echo "ERROR: ACTION must be one of activate-runtime, verify-runtime, disable, status, or rollback" >&2
        exit 1
        ;;
esac

if [[ ! "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "ERROR: EXPECTED_SHA must be a 40-character lowercase hex revision" >&2
    exit 1
fi

if [[ "$ACTION" == "activate-runtime" || "$ACTION" == "verify-runtime" ]]; then
    if [[ ! "$EXPECTED_RECONCILER_SHA" =~ ^[0-9a-f]{64}$ ]] \
        || [[ ! "$EXPECTED_RECONCILER_UNIT_SHA" =~ ^[0-9a-f]{64}$ ]]; then
        echo "ERROR: runtime artifact hashes must be 64-character lowercase hex values" >&2
        exit 1
    fi
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

# The six reviewed worker seats and nine profiled pilot services.
readonly PILOT_SEATS=(
    fin-terminal-public-seat-01
    fin-terminal-public-seat-02
    fin-terminal-public-seat-03
    fin-terminal-public-seat-04
    fin-terminal-public-seat-05
    fin-terminal-public-seat-06
)
readonly PILOT_SERVICES=(
    fin-terminal-public-redis
    fin-terminal-public-unbrowser-mcp
    "${PILOT_SEATS[@]}"
    fin-terminal-public-gateway
)
readonly PILOT_SEAT_COUNT="${#PILOT_SEATS[@]}"
readonly PILOT_SERVICE_COUNT="${#PILOT_SERVICES[@]}"
# Docker's default address pools allocate a large subnet per bridge and are
# finite. Keep the 18 per-seat bridges in one reviewed /24 using explicit /29s.
readonly PILOT_EPHEMERAL_NETWORK_SPECS=(
    fin_terminal_public_mcp_01=10.253.0.0/29
    fin_terminal_public_mcp_02=10.253.0.8/29
    fin_terminal_public_mcp_03=10.253.0.16/29
    fin_terminal_public_mcp_04=10.253.0.24/29
    fin_terminal_public_mcp_05=10.253.0.32/29
    fin_terminal_public_mcp_06=10.253.0.40/29
    fin_terminal_public_egress_01=10.253.0.48/29
    fin_terminal_public_egress_02=10.253.0.56/29
    fin_terminal_public_egress_03=10.253.0.64/29
    fin_terminal_public_egress_04=10.253.0.72/29
    fin_terminal_public_egress_05=10.253.0.80/29
    fin_terminal_public_egress_06=10.253.0.88/29
    fin_terminal_public_seat_01=10.253.0.96/29
    fin_terminal_public_seat_02=10.253.0.104/29
    fin_terminal_public_seat_03=10.253.0.112/29
    fin_terminal_public_seat_04=10.253.0.120/29
    fin_terminal_public_seat_05=10.253.0.128/29
    fin_terminal_public_seat_06=10.253.0.136/29
)
# The one-seat overlay used one shared MCP bridge. It is no longer referenced,
# but a disabled older revision may have left the empty Compose-owned network.
readonly PILOT_LEGACY_NETWORK_KEYS=(fin_terminal_public_mcp)
# Stop/remove order (reverse dependency: gateway → seats → MCP → redis).
readonly PILOT_STOP_ORDER=(
    fin-terminal-public-gateway
    "${PILOT_SEATS[@]}"
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
readonly RUNTIME_FEATURE_FLAG="TERMINAL_RUNTIME_FEATURE_ENABLED"
readonly RUNTIME_MANAGEMENT_TOKEN_KEY="TERMINAL_RUNTIME_MANAGEMENT_TOKEN"
readonly RECONCILER_ENV_FILE="$REMOTE_DIR/.env.reconciler"
readonly RECONCILER_SOURCE="$REMOTE_DIR/deploy/terminal_runtime_reconciler.py"
readonly RECONCILER_UNIT_SOURCE="$REMOTE_DIR/deploy/terminal-runtime-reconciler.service"
readonly RECONCILER_UNIT_TARGET="/etc/systemd/system/terminal-runtime-reconciler.service"
readonly RUNTIME_METADATA_FILE="$REMOTE_DIR/.terminal-runtime-current"
# Compose project name used by the base deployment.
readonly COMPOSE_PROJECT="unchained"

# Secure work dir for staging and backups (created per-run under REMOTE_DIR).
SECURE_WORKDIR=""
ROLLBACK_SNAPSHOT=""
STAGED_ENV=""
LOCK_FD=""
ROLLBACK_ARMED=false
REDIS_STATE_BACKUP=""
REDIS_STATE_BACKUP_PRESENCE=""
REDIS_STATE_BACKUP_READY=false
REDIS_STATE_EXISTED=false
REDIS_STATE_MIGRATED=false
RUNTIME_REDIS_BACKUP_DIR=""
RUNTIME_REDIS_BACKUP_READY=false
RUNTIME_REDIS_RESET=false
DYNAMIC_SETUP_STARTED=false
RUNTIME_TOKEN_FILE=""

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
    local attempts=1
    if [[ "$ACTION" == "verify-runtime" ]]; then attempts=20; fi
    local attempt
    for attempt in $(seq 1 "$attempts"); do
        if flock -n "$LOCK_FD"; then
            return 0
        fi
        sleep 0.5
    done
    echo "ERROR: deployment lock is already held" >&2
    exit 75
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
    REDIS_STATE_BACKUP=""
    REDIS_STATE_BACKUP_PRESENCE=""
    REDIS_STATE_BACKUP_READY=false
    REDIS_STATE_EXISTED=false
    REDIS_STATE_MIGRATED=false
    RUNTIME_REDIS_BACKUP_DIR=""
    RUNTIME_REDIS_BACKUP_READY=false
    RUNTIME_REDIS_RESET=false
    RUNTIME_TOKEN_FILE=""
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

# Read the reconciler/dynamic-mode flag from .env. Returns exactly "true" or
# "false". The dynamic runtime reconciler (terminal-runtime-reconciler) owns
# seat start/stop when this is true; the pilot workflow then allows stopped
# seats and requires only the warm pool, while feature-disabled mode keeps the
# legacy requirement of six ready seats.
read_dynamic_mode_enabled() {
    python3 - "$ENV_FILE" <<'PYEOF'
import os, stat, sys

path = sys.argv[1]
fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode):
        raise ValueError("not a regular file")
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        fd = -1
        content = handle.read(1024 * 1024 + 1)
finally:
    if fd >= 0:
        os.close(fd)

key = "TERMINAL_RUNTIME_FEATURE_ENABLED"
prefix = key + "="
value = "false"
for line in content.splitlines():
    if line.startswith(prefix):
        candidate = line[len(prefix):].strip()
        if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in {"'", '"'}:
            candidate = candidate[1:-1]
        value = candidate
print(value if value in {"true", "false"} else "false")
PYEOF
}

# True when the host-side reconciler owns seat lifecycle (dynamic mode).
dynamic_mode_enabled() {
    [[ "$(read_dynamic_mode_enabled)" == "true" ]]
}

# ---------------------------------------------------------------------------
# SQLite online backup (additive-migration safety gate)
# ---------------------------------------------------------------------------
sqlite_online_backup() {
    local web_cid backup_path
    web_cid="$(resolve_container_id web 2>/dev/null || true)"
    backup_path="$SECURE_WORKDIR/auth.db.backup"
    if [[ -z "$web_cid" ]]; then
        echo "ERROR: cannot take SQLite online backup — web container not found" >&2
        return 1
    fi
    # Online backup via python3 sqlite3 .backup (safe under WAL; no downtime).
    if ! docker exec "$web_cid" python3 -c '
import sqlite3, sys
src = sqlite3.connect("/data/auth.db", timeout=30)
dst = sqlite3.connect("/tmp/auth.db.backup", timeout=30)
try:
    src.backup(dst)
finally:
    dst.close(); src.close()
'; then
        echo "ERROR: SQLite online backup inside web failed" >&2
        return 1
    fi
    if ! docker cp "$web_cid:/tmp/auth.db.backup" "$backup_path" >/dev/null 2>&1; then
        echo "ERROR: could not copy SQLite backup out of web" >&2
        return 1
    fi
    docker exec "$web_cid" sh -c 'rm -f /tmp/auth.db.backup' >/dev/null 2>&1 || true
    echo "    SQLite online backup: saved ($(stat -c%s "$backup_path" 2>/dev/null || echo 0) bytes)."
    return 0
}

# ---------------------------------------------------------------------------
# Companion Redis (DB 1) backup / restore / clean — workspace state keys
# ---------------------------------------------------------------------------
workspace_redis_dump() {
    local redis_cid="$1" out_dir="$2"
    docker exec "$redis_cid" redis-cli -n 1 --scan > "$out_dir/keys.txt" 2>/dev/null || {
        echo "ERROR: could not scan workspace Redis DB 1" >&2
        return 1
    }
    local key
    while IFS= read -r key; do
        [[ -z "$key" ]] && continue
        if [[ ! "$key" =~ ^[A-Za-z0-9:_-]{1,200}$ ]]; then
            echo "ERROR: workspace Redis contains an unsafe key name" >&2
            return 1
        fi
        if ! docker exec "$redis_cid" redis-cli -n 1 --raw DUMP "$key" > "$out_dir/key.bin" 2>/dev/null; then
            echo "ERROR: could not dump workspace Redis key" >&2
            return 1
        fi
        local ttl
        ttl="$(docker exec "$redis_cid" redis-cli -n 1 --raw TTL "$key" 2>/dev/null || echo -1)"
        if [[ "$ttl" == "-1" ]]; then ttl="0"; fi
        if [[ "$ttl" == "-2" ]]; then continue; fi
        docker exec "$redis_cid" redis-cli -n 1 --raw PTTL "$key" 2>/dev/null \
            > "$out_dir/ttl-$key.txt" 2>/dev/null || true
        printf '%s\n' "$key" >> "$out_dir/dumped-keys.txt"
        mv "$out_dir/key.bin" "$out_dir/dump-$key.bin" 2>/dev/null || true
    done < "$out_dir/keys.txt"
    return 0
}

backup_workspace_redis() {
    local redis_cid out_dir
    redis_cid="$(resolve_container_id fin-terminal-public-redis 2>/dev/null || true)"
    if [[ -z "$redis_cid" ]]; then
        echo "    Companion Redis: container not running; nothing to back up."
        return 0
    fi
    out_dir="$SECURE_WORKDIR/workspace-redis"
    mkdir -p "$out_dir" && chmod 700 "$out_dir"
    : > "$out_dir/dumped-keys.txt"
    workspace_redis_dump "$redis_cid" "$out_dir" || return 1
    echo "    Companion Redis (DB 1): backed up $(wc -l < "$out_dir/dumped-keys.txt") key(s)."
    return 0
}

restore_workspace_redis_backup() {
    local redis_cid out_dir key
    redis_cid="$(resolve_container_id fin-terminal-public-redis 2>/dev/null || true)"
    out_dir="$SECURE_WORKDIR/workspace-redis"
    [[ -n "$redis_cid" ]] || return 0
    [[ -f "$out_dir/dumped-keys.txt" ]] || return 0
    docker exec "$redis_cid" redis-cli -n 1 FLUSHDB >/dev/null 2>&1 || true
    while IFS= read -r key; do
        [[ -z "$key" ]] && continue
        if [[ ! "$key" =~ ^[A-Za-z0-9:_-]{1,200}$ ]]; then
            echo "ERROR: workspace Redis backup contains an unsafe key name" >&2
            return 1
        fi
        if [[ -f "$out_dir/dump-$key.bin" ]]; then
            docker exec -i "$redis_cid" redis-cli -n 1 -x RESTORE "$key" 0 \
                < "$out_dir/dump-$key.bin" >/dev/null 2>&1 || true
            local ttl_file="$out_dir/ttl-$key.txt"
            if [[ -f "$ttl_file" ]]; then
                local pttl
                pttl="$(<"$ttl_file")"
                if [[ "$pttl" =~ ^[0-9]+$ ]] && (( pttl > 0 )); then
                    docker exec "$redis_cid" redis-cli -n 1 PEXPIRE "$key" "$pttl" >/dev/null 2>&1 || true
                fi
            fi
        fi
    done < "$out_dir/dumped-keys.txt"
    echo "    Companion Redis (DB 1): snapshot restored."
    return 0
}

cleanup_workspace_redis() {
    local redis_cid
    redis_cid="$(resolve_container_id fin-terminal-public-redis 2>/dev/null || true)"
    [[ -n "$redis_cid" ]] || return 0
    docker exec "$redis_cid" redis-cli -n 1 FLUSHDB >/dev/null 2>&1
    echo "    Companion Redis (DB 1): workspace keys cleaned."
    return 0
}

# Capacity drains and global research permits are persisted beside admission
# state in Redis DB 0. They are process-lifecycle state, not durable accounting:
# every fresh activation must start with no old drain fences or permits. Back up
# the two fixed keys before deleting them so activation rollback can restore the
# exact prior values.
backup_runtime_redis_state() {
    local redis_cid result
    redis_cid="$(resolve_container_id fin-terminal-public-redis 2>/dev/null || true)"
    [[ -n "$redis_cid" ]] || {
        echo "ERROR: Redis is unavailable for runtime-state backup" >&2
        return 1
    }
    RUNTIME_REDIS_BACKUP_DIR="$SECURE_WORKDIR/runtime-redis"
    mkdir -m 700 "$RUNTIME_REDIS_BACKUP_DIR"
    result="$(python3 - "$redis_cid" "$RUNTIME_REDIS_BACKUP_DIR" <<'PYEOF'
import os, stat, subprocess, sys

container_id, backup_dir = sys.argv[1:]
keys = {
    "capacity": "fin-terminal-public:v1:capacity",
    "research-permits": "fin-terminal-public:v1:research-permits",
}

def redis(*args, stdin=None):
    completed = subprocess.run(
        ["docker", "exec", "-i", container_id, "redis-cli", *args],
        input=stdin,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Redis command failed")
    return completed.stdout

def write_file(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        written = 0
        while written < len(data):
            count = os.write(fd, data[written:])
            if count <= 0:
                raise OSError("short runtime Redis backup write")
            written += count
        os.fsync(fd)
    finally:
        os.close(fd)
    st = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o600:
        raise RuntimeError("unsafe runtime Redis backup")

for label, key in keys.items():
    exists = redis("--raw", "EXISTS", key).decode("ascii").strip()
    if exists not in {"0", "1"}:
        raise RuntimeError("Redis existence check failed")
    write_file(os.path.join(backup_dir, f"{label}.presence"), ("present\n" if exists == "1" else "absent\n").encode("ascii"))
    if exists == "1":
        dump = redis("--raw", "DUMP", key)
        # redis-cli --raw appends one display newline after the binary reply.
        if not dump.endswith(b"\n"):
            raise RuntimeError("Redis DUMP response was not terminated")
        write_file(os.path.join(backup_dir, f"{label}.dump"), dump[:-1])
print("RUNTIME_REDIS_BACKUP_OK")
PYEOF
)" || true
    if [[ "$result" != "RUNTIME_REDIS_BACKUP_OK" ]]; then
        echo "ERROR: runtime Redis backup failed (details scrubbed)" >&2
        return 1
    fi
    RUNTIME_REDIS_BACKUP_READY=true
    echo "    Runtime Redis state: exact capacity/permit backup saved."
}

delete_runtime_redis_state() {
    local redis_cid result
    redis_cid="$(resolve_container_id fin-terminal-public-redis 2>/dev/null || true)"
    [[ -n "$redis_cid" ]] || return 0
    result="$(docker exec "$redis_cid" redis-cli --raw DEL \
        'fin-terminal-public:v1:capacity' \
        'fin-terminal-public:v1:research-permits' 2>/dev/null || true)"
    [[ "$result" == "0" || "$result" == "1" || "$result" == "2" ]] || {
        echo "ERROR: could not delete runtime Redis capacity/permit state" >&2
        return 1
    }
    for key in fin-terminal-public:v1:capacity fin-terminal-public:v1:research-permits; do
        if [[ "$(docker exec "$redis_cid" redis-cli --raw EXISTS "$key" 2>/dev/null || true)" != "0" ]]; then
            echo "ERROR: runtime Redis key remains after deletion" >&2
            return 1
        fi
    done
    echo "    Runtime Redis state: capacity drains and permits cleared."
}

reset_runtime_redis_state_for_activation() {
    RUNTIME_REDIS_RESET=true
    delete_runtime_redis_state
}

restore_runtime_redis_state_backup() {
    $RUNTIME_REDIS_RESET || return 0
    if ! $RUNTIME_REDIS_BACKUP_READY || [[ -z "$RUNTIME_REDIS_BACKUP_DIR" ]]; then
        echo "ERROR: runtime Redis state changed without a restorable backup" >&2
        return 1
    fi
    local redis_cid result
    redis_cid="$(resolve_container_id fin-terminal-public-redis 2>/dev/null || true)"
    [[ -n "$redis_cid" ]] || {
        echo "ERROR: Redis is unavailable for runtime-state restore" >&2
        return 1
    }
    result="$(python3 - "$redis_cid" "$RUNTIME_REDIS_BACKUP_DIR" <<'PYEOF'
import os, subprocess, sys

container_id, backup_dir = sys.argv[1:]
keys = {
    "capacity": "fin-terminal-public:v1:capacity",
    "research-permits": "fin-terminal-public:v1:research-permits",
}

def redis(*args, stdin=None):
    completed = subprocess.run(
        ["docker", "exec", "-i", container_id, "redis-cli", *args],
        input=stdin,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Redis command failed")
    return completed.stdout.decode("ascii", errors="strict").strip()

for label, key in keys.items():
    presence_path = os.path.join(backup_dir, f"{label}.presence")
    with open(presence_path, "r", encoding="ascii") as handle:
        presence = handle.read().strip()
    if presence not in {"present", "absent"}:
        raise RuntimeError("invalid runtime Redis presence marker")
    redis("DEL", key)
    if presence == "present":
        dump_path = os.path.join(backup_dir, f"{label}.dump")
        with open(dump_path, "rb") as handle:
            dump = handle.read()
        if redis("-x", "RESTORE", key, "0", stdin=dump) != "OK":
            raise RuntimeError("runtime Redis restore failed")
    expected = "1" if presence == "present" else "0"
    if redis("--raw", "EXISTS", key) != expected:
        raise RuntimeError("runtime Redis restore verification failed")
print("RUNTIME_REDIS_RESTORE_OK")
PYEOF
)" || true
    if [[ "$result" != "RUNTIME_REDIS_RESTORE_OK" ]]; then
        echo "ERROR: runtime Redis restore failed (details scrubbed)" >&2
        return 1
    fi
    RUNTIME_REDIS_RESET=false
    RUNTIME_REDIS_BACKUP_READY=false
    echo "    Runtime Redis state: pre-activation capacity/permit values restored."
}

# ---------------------------------------------------------------------------
# Atomic .env updater — embedded Python with full safety (review item A).
# Values beginning with @ are read from that owner-only regular file. This is
# used for the management token so the secret never appears in argv or logs.
# ---------------------------------------------------------------------------
update_env_flag() {
    local env_path="$1" key="$2" value="$3"
    python3 - "$env_path" "$key" "$value" <<'PYEOF'
import os, re, stat, sys, tempfile

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
    if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
        raise ValueError('invalid environment key')
    value_arg = sys.argv[3]
    if value_arg.startswith('@'):
        value_path = value_arg[1:]
        value_fd = os.open(value_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            value_st = os.fstat(value_fd)
            if not stat.S_ISREG(value_st.st_mode):
                raise ValueError('value source is not a regular file')
            if stat.S_IMODE(value_st.st_mode) != 0o600 or value_st.st_uid != os.geteuid():
                raise ValueError('value source must be owner-only')
            value_bytes = os.read(value_fd, 4097)
        finally:
            os.close(value_fd)
        if len(value_bytes) > 4096:
            raise ValueError('environment value too large')
        value = value_bytes.decode('utf-8')
    else:
        value = value_arg
    if '\x00' in value or '\n' in value or '\r' in value:
        raise ValueError('environment value contains a line break')
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
# Dynamic runtime configuration and systemd lifecycle
# ---------------------------------------------------------------------------
verify_runtime_artifacts() {
    local reconciler_sha unit_sha
    for path in "$RECONCILER_SOURCE" "$RECONCILER_UNIT_SOURCE"; do
        if [[ ! -f "$path" || -L "$path" ]]; then
            echo "ERROR: deployed runtime artifact is missing or unsafe: $path" >&2
            return 1
        fi
    done
    reconciler_sha="$(sha256sum "$RECONCILER_SOURCE" | cut -d' ' -f1)"
    unit_sha="$(sha256sum "$RECONCILER_UNIT_SOURCE" | cut -d' ' -f1)"
    if [[ "$reconciler_sha" != "$EXPECTED_RECONCILER_SHA" ]] \
        || [[ "$unit_sha" != "$EXPECTED_RECONCILER_UNIT_SHA" ]]; then
        echo "ERROR: deployed runtime artifacts do not match the approved main revision" >&2
        return 1
    fi
    if ! python3 - "$RECONCILER_SOURCE" <<'PYEOF'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
compile(path.read_bytes(), str(path), "exec")
PYEOF
    then
        echo "ERROR: deployed reconciler does not compile" >&2
        return 1
    fi
    if ! grep -Fq 'User=ec2-user' "$RECONCILER_UNIT_SOURCE" \
        || ! grep -Fq 'EnvironmentFile=/home/ec2-user/unchained/.env.reconciler' "$RECONCILER_UNIT_SOURCE" \
        || ! grep -Fq 'ExecStart=/usr/bin/python3 /home/ec2-user/unchained/deploy/terminal_runtime_reconciler.py' "$RECONCILER_UNIT_SOURCE"; then
        echo "ERROR: reconciler unit does not match the reviewed production identity/path" >&2
        return 1
    fi
    echo "    Runtime artifacts: exact approved hashes verified."
}

runtime_install_preflight() {
    if [[ "${USER:-}" != "ec2-user" ]] || [[ "$REMOTE_DIR" != "/home/ec2-user/unchained" ]]; then
        echo "ERROR: dynamic runtime installation is restricted to the reviewed ec2-user path" >&2
        return 1
    fi
    for command_name in sudo systemctl systemd-analyze sha256sum; do
        command -v "$command_name" >/dev/null 2>&1 || {
            echo "ERROR: required host command is unavailable: $command_name" >&2
            return 1
        }
    done
    sudo -n true >/dev/null 2>&1 || {
        echo "ERROR: passwordless sudo is required for reviewed systemd installation" >&2
        return 1
    }
    verify_runtime_artifacts || return 1
    if [[ "$(read_dynamic_mode_enabled)" != "false" ]]; then
        echo "ERROR: $RUNTIME_FEATURE_FLAG must be false before a fresh runtime activation" >&2
        return 1
    fi
    if [[ -e "$RECONCILER_ENV_FILE" || -L "$RECONCILER_ENV_FILE" \
        || -e "$RUNTIME_METADATA_FILE" || -L "$RUNTIME_METADATA_FILE" \
        || -e "$RECONCILER_UNIT_TARGET" || -L "$RECONCILER_UNIT_TARGET" ]]; then
        echo "ERROR: stale runtime configuration is present; refusing an ambiguous install" >&2
        return 1
    fi
    sudo systemctl daemon-reload >/dev/null
    if sudo systemctl is-active --quiet terminal-runtime-reconciler 2>/dev/null \
        || sudo systemctl cat terminal-runtime-reconciler >/dev/null 2>&1; then
        echo "ERROR: terminal-runtime-reconciler is already loaded; disable it before activation" >&2
        return 1
    fi
    echo "    Runtime install preflight: clean host state verified."
}

write_reconciler_env() {
    local token_path="$1"
    python3 - "$RECONCILER_ENV_FILE" "$token_path" "$REMOTE_DIR" <<'PYEOF'
import os, re, stat, sys, tempfile

target, token_path, compose_dir = sys.argv[1:]
if os.path.lexists(target):
    raise ValueError("reconciler env already exists")
fd = os.open(token_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o600:
        raise ValueError("unsafe token source")
    if st.st_uid != os.geteuid():
        raise ValueError("token source owner mismatch")
    token = os.read(fd, 129).decode("ascii")
finally:
    os.close(fd)
if not re.fullmatch(r"[0-9a-f]{64}", token):
    raise ValueError("management token must be 256-bit lowercase hex")
content = (
    "TERMINAL_RUNTIME_FEATURE_ENABLED=true\n"
    f"TERMINAL_RUNTIME_MANAGEMENT_TOKEN={token}\n"
    "TERMINAL_RUNTIME_MANAGEMENT_PORT=8789\n"
    "TERMINAL_RUNTIME_COMPOSE_PROJECT=unchained\n"
    f"TERMINAL_RUNTIME_COMPOSE_DIR={compose_dir}\n"
    "TERMINAL_RUNTIME_RECONCILE_INTERVAL=15\n"
    "TERMINAL_RUNTIME_IDLE_SCALE_DOWN=300\n"
    "TERMINAL_RUNTIME_MAX_START_CONCUR=2\n"
    "TERMINAL_RUNTIME_HOST_MEM_RESERVE_MB=512\n"
    "TERMINAL_RUNTIME_HOST_MEM_HEADROOM_PCT=15\n"
    "TERMINAL_RUNTIME_HOST_DISK_MAX_PCT=85\n"
)
directory = os.path.dirname(target)
dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
tmp_path = None
try:
    fd_tmp, tmp_path = tempfile.mkstemp(prefix=".env.reconciler.", dir=directory)
    try:
        os.fchmod(fd_tmp, 0o600)
        data = content.encode("utf-8")
        written = 0
        while written < len(data):
            count = os.write(fd_tmp, data[written:])
            if count <= 0:
                raise OSError("short write")
            written += count
        os.fsync(fd_tmp)
    finally:
        os.close(fd_tmp)
    if os.path.lexists(target):
        raise ValueError("reconciler env appeared before install")
    os.replace(tmp_path, target)
    tmp_path = None
    os.fsync(dir_fd)
finally:
    os.close(dir_fd)
    if tmp_path is not None:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
final = os.stat(target, follow_symlinks=False)
if not stat.S_ISREG(final.st_mode) or stat.S_IMODE(final.st_mode) != 0o600:
    raise ValueError("unsafe reconciler env after install")
PYEOF
}

write_runtime_metadata() {
    local started_at="$1"
    python3 - "$RUNTIME_METADATA_FILE" "$EXPECTED_SHA" \
        "$EXPECTED_RECONCILER_SHA" "$EXPECTED_RECONCILER_UNIT_SHA" "$started_at" <<'PYEOF'
import os, re, stat, sys, tempfile

target, revision, reconciler_sha, unit_sha, started_at = sys.argv[1:]
if os.path.lexists(target):
    raise ValueError("runtime metadata already exists")
if not re.fullmatch(r"[0-9a-f]{40}", revision):
    raise ValueError("invalid revision")
if not re.fullmatch(r"[0-9a-f]{64}", reconciler_sha) or not re.fullmatch(r"[0-9a-f]{64}", unit_sha):
    raise ValueError("invalid artifact hash")
if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", started_at):
    raise ValueError("invalid activation timestamp")
data = (
    f"revision={revision}\n"
    f"reconciler_sha256={reconciler_sha}\n"
    f"unit_sha256={unit_sha}\n"
    f"started_at={started_at}\n"
).encode("ascii")
directory = os.path.dirname(target)
dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
tmp_path = None
try:
    fd, tmp_path = tempfile.mkstemp(prefix=".terminal-runtime-current.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, target)
    tmp_path = None
    os.fsync(dir_fd)
finally:
    os.close(dir_fd)
    if tmp_path is not None:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
PYEOF
}

prepare_dynamic_runtime() {
    runtime_install_preflight || return 1
    DYNAMIC_SETUP_STARTED=true

    RUNTIME_TOKEN_FILE="$SECURE_WORKDIR/runtime-management-token"
    umask 077
    python3 -c 'import secrets; print(secrets.token_hex(32), end="")' > "$RUNTIME_TOKEN_FILE"
    chmod 600 "$RUNTIME_TOKEN_FILE"

    # Install the token first and flip the master feature flag last. The armed
    # activation rollback restores the exact pre-activation .env on any error.
    update_env_flag "$ENV_FILE" "$RUNTIME_MANAGEMENT_TOKEN_KEY" "@$RUNTIME_TOKEN_FILE" || return 1
    update_env_flag "$ENV_FILE" "$RUNTIME_FEATURE_FLAG" "true" || return 1
    write_reconciler_env "$RUNTIME_TOKEN_FILE" || return 1

    if ! compose_cmd config --quiet >/dev/null 2>&1; then
        echo "ERROR: dynamic runtime Compose configuration is invalid" >&2
        return 1
    fi

    sudo install -o root -g root -m 0644 "$RECONCILER_UNIT_SOURCE" "$RECONCILER_UNIT_TARGET" || return 1
    sudo systemd-analyze verify "$RECONCILER_UNIT_TARGET" >/dev/null 2>&1 || {
        echo "ERROR: systemd rejected the reconciler unit" >&2
        return 1
    }
    sudo systemctl daemon-reload >/dev/null || return 1
    sudo systemctl enable terminal-runtime-reconciler >/dev/null || return 1
    write_runtime_metadata "$(date -u +%Y-%m-%dT%H:%M:%SZ)" || return 1
    echo "    Dynamic runtime configuration: installed (token output suppressed)."
}

runtime_metadata_value() {
    local key="$1"
    [[ -f "$RUNTIME_METADATA_FILE" && ! -L "$RUNTIME_METADATA_FILE" ]] || return 1
    awk -F= -v wanted="$key" '$1 == wanted { print $2; found += 1 } END { if (found != 1) exit 1 }' \
        "$RUNTIME_METADATA_FILE"
}

runtime_install_matches_metadata() {
    local metadata_revision metadata_reconciler metadata_unit actual_reconciler actual_unit deployed_revision
    metadata_revision="$(runtime_metadata_value revision)" || return 1
    metadata_reconciler="$(runtime_metadata_value reconciler_sha256)" || return 1
    metadata_unit="$(runtime_metadata_value unit_sha256)" || return 1
    [[ "$metadata_revision" =~ ^[0-9a-f]{40}$ ]] || return 1
    [[ "$metadata_reconciler" =~ ^[0-9a-f]{64}$ ]] || return 1
    [[ "$metadata_unit" =~ ^[0-9a-f]{64}$ ]] || return 1
    [[ -f "$RECONCILER_SOURCE" && ! -L "$RECONCILER_SOURCE" ]] || return 1
    [[ -f "$RECONCILER_UNIT_TARGET" && ! -L "$RECONCILER_UNIT_TARGET" ]] || return 1
    actual_reconciler="$(sha256sum "$RECONCILER_SOURCE" | cut -d' ' -f1)"
    actual_unit="$(sudo sha256sum "$RECONCILER_UNIT_TARGET" | cut -d' ' -f1)"
    deployed_revision="$(awk -F= '/^revision=/ { print $2; exit }' "$DEPLOY_CURRENT")"
    [[ "$metadata_revision" == "$deployed_revision" \
        && "$actual_reconciler" == "$metadata_reconciler" \
        && "$actual_unit" == "$metadata_unit" ]]
}

verify_runtime_config_consistency() {
    python3 - "$ENV_FILE" "$RECONCILER_ENV_FILE" "$REMOTE_DIR" <<'PYEOF'
import os, re, stat, sys

env_path, reconciler_path, compose_dir = sys.argv[1:]

def read(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o600:
            raise ValueError("unsafe runtime environment file")
        if st.st_uid != os.geteuid():
            raise ValueError("runtime environment owner mismatch")
        data = os.read(fd, 1024 * 1024 + 1)
    finally:
        os.close(fd)
    if len(data) > 1024 * 1024:
        raise ValueError("runtime environment too large")
    values = {}
    for line in data.decode("utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in values:
            raise ValueError(f"duplicate runtime key: {key}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values

env = read(env_path)
reconciler = read(reconciler_path)
token = env.get("TERMINAL_RUNTIME_MANAGEMENT_TOKEN", "")
if not re.fullmatch(r"[0-9a-f]{64}", token):
    raise ValueError("invalid live management token")
if env.get("TERMINAL_RUNTIME_FEATURE_ENABLED") != "true":
    raise ValueError("live runtime feature is not enabled")
expected = {
    "TERMINAL_RUNTIME_FEATURE_ENABLED": "true",
    "TERMINAL_RUNTIME_MANAGEMENT_TOKEN": token,
    "TERMINAL_RUNTIME_MANAGEMENT_PORT": "8789",
    "TERMINAL_RUNTIME_COMPOSE_PROJECT": "unchained",
    "TERMINAL_RUNTIME_COMPOSE_DIR": compose_dir,
    "TERMINAL_RUNTIME_RECONCILE_INTERVAL": "15",
    "TERMINAL_RUNTIME_IDLE_SCALE_DOWN": "300",
    "TERMINAL_RUNTIME_MAX_START_CONCUR": "2",
    "TERMINAL_RUNTIME_HOST_MEM_RESERVE_MB": "512",
    "TERMINAL_RUNTIME_HOST_MEM_HEADROOM_PCT": "15",
    "TERMINAL_RUNTIME_HOST_DISK_MAX_PCT": "85",
}
for key, value in expected.items():
    if reconciler.get(key) != value:
        raise ValueError(f"reconciler runtime mismatch: {key}")
PYEOF
}

start_reconciler() {
    echo "==> Starting host runtime reconciler behind the closed edge..."
    sudo systemctl start terminal-runtime-reconciler || return 1
    local attempt active_state sub_state restarts
    for attempt in $(seq 1 15); do
        active_state="$(sudo systemctl show terminal-runtime-reconciler -p ActiveState --value 2>/dev/null || true)"
        sub_state="$(sudo systemctl show terminal-runtime-reconciler -p SubState --value 2>/dev/null || true)"
        restarts="$(sudo systemctl show terminal-runtime-reconciler -p NRestarts --value 2>/dev/null || true)"
        if [[ "$active_state" == "active" && "$sub_state" == "running" && "$restarts" == "0" ]]; then
            sleep 3
            if sudo systemctl is-active --quiet terminal-runtime-reconciler; then
                echo "    terminal-runtime-reconciler: active with no restarts."
                return 0
            fi
        fi
        sleep 1
    done
    sudo journalctl -u terminal-runtime-reconciler -n 30 --no-pager -o cat >&2 || true
    echo "ERROR: terminal-runtime-reconciler did not remain active" >&2
    return 1
}

# Redact secret-like material from reconciler diagnostics before output:
#   * values assigned to known token keys (any length) — management tokens,
#     visitor/edge tokens, or bare "token:" headers
#   * any standalone token of 32+ alphanumeric characters — management tokens
#     are validated 64-hex, InvocationID is 32-hex, container ids/digests etc.
# The awk pass walks each maximal alphanumeric run so adjacent tokens cannot
# dodge the redaction.
redact_diagnostic_output() {
    sed -E \
        -e 's/(TERMINAL_RUNTIME_MANAGEMENT_TOKEN[=:][[:space:]]*)[^[:space:]]+/\1<redacted>/g' \
        -e 's/([Mm]anagement[ _-]?[Tt]oken[=:][[:space:]]*)[^[:space:]]+/\1<redacted>/g' \
        -e 's/([A-Za-z0-9_-]*[Tt]oken[=:][[:space:]]*)[^[:space:]]+/\1<redacted>/g' \
        | awk '{
              line = $0
              out = ""
              while (match(line, /[A-Za-z0-9]{32,}/)) {
                  out = out substr(line, 1, RSTART - 1) "<redacted>"
                  line = substr(line, RSTART + RLENGTH)
              }
              print out line
          }'
}

# Bounded, redacted systemd reconciler diagnostics. Advisory only: a missing
# sudo/systemctl/journalctl, a removed unit, or any diagnostic failure never
# fails the caller. Never prints the unit file, environment-file contents, or
# process command lines — only the selected systemctl show fields and at most
# the last 80 journal lines, redacted.
reconciler_diagnostics() {
    if ! command -v sudo >/dev/null 2>&1 \
        || ! command -v systemctl >/dev/null 2>&1; then
        echo "    Reconciler diagnostics: sudo/systemctl unavailable"
        return 0
    fi
    # Non-interactive sudo only: a diagnostic must never block on a password
    # prompt inside status/verify paths.
    if ! sudo -n true >/dev/null 2>&1; then
        echo "    Reconciler diagnostics: passwordless sudo unavailable (skipped)"
        return 0
    fi
    echo "    Reconciler diagnostics:"
    if sudo -n systemctl cat terminal-runtime-reconciler >/dev/null 2>&1; then
        local show_fields
        show_fields="$(sudo -n systemctl show terminal-runtime-reconciler \
            -p LoadState -p ActiveState -p SubState -p NRestarts -p InvocationID \
            2>/dev/null || true)"
        if [[ -n "$show_fields" ]]; then
            printf '%s\n' "$show_fields" | redact_diagnostic_output | sed 's/^/      /' || true
        fi
    else
        echo "      unit not installed (removed or never created)"
    fi
    # journald retains evidence after fail-closed removes the unit, so query it
    # independently of the current unit-load state.
    if command -v journalctl >/dev/null 2>&1; then
        sudo -n journalctl -u terminal-runtime-reconciler -n 80 --no-pager -o cat 2>/dev/null \
            | redact_diagnostic_output | sed 's/^/      /' || true
    fi
    return 0
}

verify_reconciler_cycle() {
    local invocation_id logs cycle_state restarts
    # Synchronize on the live unit's systemd InvocationID (journald field
    # match) instead of a wall-clock/RFC3339 timestamp boundary: the activation
    # timestamp is not a reliable journald filter on this host. Reject any
    # value that is not exactly 32 lowercase hex (normalize only case).
    invocation_id="$(sudo systemctl show terminal-runtime-reconciler \
        -p InvocationID --value 2>/dev/null || true)"
    invocation_id="$(printf '%s' "$invocation_id" | tr '[:upper:]' '[:lower:]')"
    if [[ ! "$invocation_id" =~ ^[0-9a-f]{32}$ ]]; then
        echo "ERROR: reconciler InvocationID is invalid or unavailable" >&2
        reconciler_diagnostics >&2
        return 1
    fi
    # Query only the current invocation's journal lines. A journalctl command
    # failure must never be suppressed into an empty "successful" log.
    if ! logs="$(sudo journalctl -u terminal-runtime-reconciler -n 200 \
        "_SYSTEMD_INVOCATION_ID=$invocation_id" --no-pager -o cat 2>/dev/null)"; then
        echo "ERROR: could not read reconciler journal for the current invocation" >&2
        reconciler_diagnostics >&2
        return 1
    fi
    # A transient failed cycle may recover. Require the most recent relevant
    # journal event to be a successful reconcile cycle; retrying the workflow
    # can then observe recovery instead of being poisoned by an older error line.
    # lock-busy markers are expected while an activate/disable holds the deploy
    # lock and are neither success nor fatal.
    if ! cycle_state="$(awk '
/Cycle outcome: success/ { last_success = NR }
/Configuration error|Reconcile cycle error|Reconcile snapshot failed|Traceback/ { last_error = NR }
END {
    if (last_success == 0 || last_error > last_success) exit 1
    print "healthy"
}
' <<<"$logs")" || [[ "$cycle_state" != "healthy" ]]; then
        echo "ERROR: no latest successful post-unlock reconciler cycle was observed" >&2
        reconciler_diagnostics >&2
        return 1
    fi
    if ! sudo systemctl is-active --quiet terminal-runtime-reconciler; then
        echo "ERROR: reconciler is not active after its successful cycle" >&2
        return 1
    fi
    restarts="$(sudo systemctl show terminal-runtime-reconciler -p NRestarts --value 2>/dev/null || true)"
    if [[ "$restarts" != "0" ]]; then
        echo "ERROR: reconciler restarted after activation" >&2
        return 1
    fi
    echo "    Reconciler post-unlock cycle: verified."
}

stop_reconciler() {
    if command -v systemctl >/dev/null 2>&1; then
        sudo systemctl disable --now terminal-runtime-reconciler >/dev/null 2>&1 || {
            if sudo systemctl is-active --quiet terminal-runtime-reconciler 2>/dev/null; then
                echo "ERROR: could not stop terminal-runtime-reconciler" >&2
                return 1
            fi
        }
    fi
    return 0
}

remove_managed_runtime_install() {
    local force_new="${1:-false}"
    stop_reconciler || return 1
    if [[ "$force_new" != "true" ]] && ! runtime_install_matches_metadata; then
        echo "ERROR: runtime install metadata does not authorize automatic removal" >&2
        return 1
    fi
    sudo rm -f "$RECONCILER_UNIT_TARGET" || return 1
    sudo systemctl daemon-reload >/dev/null || return 1
    sudo systemctl reset-failed terminal-runtime-reconciler >/dev/null 2>&1 || true
    rm -f "$RECONCILER_ENV_FILE" "$RUNTIME_METADATA_FILE"
    return 0
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
    [[ "$ACTION" == "activate-runtime" ]] || return 0
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
# Reversible Redis worker-set transition
# ---------------------------------------------------------------------------
# The pinned gateway intentionally refuses persisted state whose worker IDs do
# not exactly match configuration. While the gateway is stopped, transition
# only the worker-set portion between the rollback-compatible one-seat shape
# and the reviewed six-seat shape. Preserve the daily reservation counter and
# ended ticket history; terminate any stale live/queued tickets fail-closed.
wait_redis_gateway_lock_clear() {
    local redis_cid="$1" lock_value attempts
    for attempts in $(seq 1 20); do
        lock_value="$(docker exec "$redis_cid" redis-cli --raw \
            GET 'fin-terminal-public:v1:gateway-lock' 2>/dev/null || true)"
        if [[ -z "$lock_value" ]]; then
            return 0
        fi
        sleep 1
    done
    echo "ERROR: Redis gateway lease did not clear" >&2
    return 1
}

transition_redis_worker_set() {
    local target_count="$1" backup_path="${2:--}" redis_cid result
    redis_cid="$(resolve_container_id fin-terminal-public-redis)"
    if [[ -z "$redis_cid" ]]; then
        echo "ERROR: could not resolve Redis container for worker-set transition" >&2
        return 1
    fi
    wait_redis_gateway_lock_clear "$redis_cid" || return 1

    result="$(python3 - "$redis_cid" "$target_count" "$backup_path" 2>/dev/null <<'PYEOF'
import json
import os
import stat
import subprocess
import sys
import time

container_id, target_raw, backup_path = sys.argv[1:]
target_count = int(target_raw)
if target_count not in {1, 6}:
    raise SystemExit(1)

STATE_KEY = "fin-terminal-public:v1:state"
ONE = ["seat-01"]
SIX = [f"seat-{value:02d}" for value in range(1, 7)]
target_workers = ONE if target_count == 1 else SIX

def redis(*args, stdin=None):
    completed = subprocess.run(
        ["docker", "exec", "-i", container_id, "redis-cli", *args],
        input=stdin,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Redis command failed")
    # redis-cli appends one protocol newline. Remove exactly that newline so a
    # persisted value that itself ends in a newline can still be backed up and
    # restored byte-for-byte.
    return completed.stdout[:-1] if completed.stdout.endswith("\n") else completed.stdout

exists_raw = redis("--raw", "EXISTS", STATE_KEY)
if exists_raw not in {"0", "1"}:
    raise RuntimeError("Redis existence check failed")
existed = exists_raw == "1"
raw = redis("--raw", "GET", STATE_KEY) if existed else ""
if backup_path != "-":
    def write_backup(path, value):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            data = value.encode("utf-8")
            written = 0
            while written < len(data):
                count = os.write(fd, data[written:])
                if count <= 0:
                    raise OSError("short Redis backup write")
                written += count
            os.fsync(fd)
        finally:
            os.close(fd)
        backup_stat = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(backup_stat.st_mode) or stat.S_IMODE(backup_stat.st_mode) != 0o600:
            raise RuntimeError("unsafe Redis backup file")

    # The separate presence marker distinguishes an absent key from a present
    # empty value. Both files are durable before any Redis mutation occurs.
    write_backup(backup_path, raw)
    write_backup(backup_path + ".presence", "present\n" if existed else "absent\n")

if existed:
    state = json.loads(raw)
    if state.get("version") != 1:
        raise ValueError("unsupported persisted state version")
    sessions = state.get("sessions")
    queue = state.get("queue")
    workers = state.get("workers")
    if not isinstance(sessions, list) or not isinstance(queue, list) or not isinstance(workers, list):
        raise ValueError("invalid persisted state shape")
    current_workers = [worker.get("id") for worker in workers if isinstance(worker, dict)]
    if len(current_workers) != len(workers) or sorted(current_workers) not in [ONE, SIX]:
        raise ValueError("unexpected persisted worker set")
    reserved = state.get("dailyReservedMicroUsd")
    if not isinstance(reserved, int) or isinstance(reserved, bool) or reserved < 0:
        raise ValueError("invalid persisted reservation counter")
    now = int(time.time() * 1000)
    for session in sessions:
        if not isinstance(session, dict):
            raise ValueError("invalid persisted session")
        if session.get("state") != "ended":
            session["state"] = "ended"
            session["endReason"] = "worker-unavailable"
            session["endedAt"] = now
            session.pop("pendingConnectionVersion", None)
            session.pop("pendingConnectionReservedAt", None)
    state["queue"] = []
    state["workers"] = [{"id": worker_id} for worker_id in target_workers]
    updated = json.dumps(state, separators=(",", ":"), sort_keys=True)
    if redis("-x", "SET", STATE_KEY, stdin=updated).strip() != "OK":
        raise RuntimeError("Redis state write failed")
    if redis("--raw", "GET", STATE_KEY) != updated:
        raise RuntimeError("Redis state verification failed")

print("STATE_TRANSITION_OK:" + ("present" if existed else "absent"))
PYEOF
)" || {
        echo "ERROR: persisted worker-set transition failed (details scrubbed)" >&2
        return 1
    }
    case "$result" in
        STATE_TRANSITION_OK:present)
            [[ "$backup_path" == "-" ]] || REDIS_STATE_EXISTED=true
            ;;
        STATE_TRANSITION_OK:absent)
            [[ "$backup_path" == "-" ]] || REDIS_STATE_EXISTED=false
            ;;
        *)
            echo "ERROR: persisted worker-set transition returned an unexpected result" >&2
            return 1
            ;;
    esac
    echo "    Persisted admission worker set: transitioned to ${target_count}."
    return 0
}

prepare_six_worker_state() {
    REDIS_STATE_BACKUP="$SECURE_WORKDIR/redis-state.backup"
    REDIS_STATE_BACKUP_PRESENCE="${REDIS_STATE_BACKUP}.presence"
    # Companion workspace Redis (DB 1) keys are snapshotted before any state
    # mutation so activate/rollback/disable can restore or clean them exactly.
    backup_workspace_redis || return 1
    backup_runtime_redis_state || return 1
    REDIS_STATE_MIGRATED=true
    if ! transition_redis_worker_set 6 "$REDIS_STATE_BACKUP"; then
        # The transition cannot mutate Redis before writing the presence marker.
        # If the marker is absent there is therefore nothing to restore.
        if [[ ! -f "$REDIS_STATE_BACKUP_PRESENCE" ]]; then
            REDIS_STATE_MIGRATED=false
        elif ! load_redis_backup_presence; then
            # A marker without a valid backup is ambiguous after a failed SET;
            # leave rollback armed so it fails closed rather than guessing.
            REDIS_STATE_BACKUP_READY=false
        fi
        return 1
    fi
    load_redis_backup_presence || return 1
    reset_runtime_redis_state_for_activation || return 1
}

load_redis_backup_presence() {
    [[ -f "$REDIS_STATE_BACKUP" ]] || return 1
    [[ -f "$REDIS_STATE_BACKUP_PRESENCE" ]] || return 1
    local presence
    presence="$(<"$REDIS_STATE_BACKUP_PRESENCE")"
    case "$presence" in
        present) REDIS_STATE_EXISTED=true ;;
        absent)  REDIS_STATE_EXISTED=false ;;
        *)       return 1 ;;
    esac
    REDIS_STATE_BACKUP_READY=true
    return 0
}

restore_redis_state_backup() {
    $REDIS_STATE_MIGRATED || return 0
    if ! $REDIS_STATE_BACKUP_READY; then
        echo "ERROR: Redis state changed without a restorable snapshot" >&2
        return 1
    fi
    local redis_cid result
    redis_cid="$(resolve_container_id fin-terminal-public-redis)"
    if [[ -z "$redis_cid" ]]; then
        echo "ERROR: could not resolve Redis container for state restore" >&2
        return 1
    fi
    wait_redis_gateway_lock_clear "$redis_cid" || return 1
    if $REDIS_STATE_EXISTED; then
        result="$(docker exec -i "$redis_cid" redis-cli -x SET \
            'fin-terminal-public:v1:state' < "$REDIS_STATE_BACKUP" 2>/dev/null || true)"
        if [[ "$result" != "OK" ]]; then
            echo "ERROR: could not restore persisted admission state" >&2
            return 1
        fi
    else
        result="$(docker exec "$redis_cid" redis-cli DEL \
            'fin-terminal-public:v1:state' 2>/dev/null || true)"
        if [[ "$result" != "0" && "$result" != "1" ]]; then
            echo "ERROR: could not remove newly-created persisted admission state" >&2
            return 1
        fi
    fi
    echo "    Persisted admission state: pre-activation snapshot restored."
    REDIS_STATE_MIGRATED=false
    REDIS_STATE_BACKUP_READY=false
    REDIS_STATE_BACKUP_PRESENCE=""
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

    # Stop the host controller before changing its token/feature configuration.
    # A fresh runtime activation proves these files were absent beforehand, so
    # the armed transaction may remove them unambiguously.
    if $DYNAMIC_SETUP_STARTED; then
        if ! remove_managed_runtime_install true; then
            echo "FATAL: could not stop/remove the failed runtime controller" >&2
            stop_caddy_fail_closed
            secure_workdir_cleanup
            exit 1
        fi
    fi

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
    # Stop the only Redis writer first, then restore the exact pre-activation
    # state while Redis is still available.
    compose_cmd stop fin-terminal-public-gateway 2>/dev/null || true
    if ! restore_redis_state_backup; then
        echo "FATAL: could not restore persisted admission state during rollback" >&2
        secure_workdir_cleanup
        exit 1
    fi
    if ! restore_runtime_redis_state_backup; then
        echo "FATAL: could not restore capacity/permit Redis state during rollback" >&2
        secure_workdir_cleanup
        exit 1
    fi
    if ! restore_workspace_redis_backup; then
        echo "FATAL: could not restore companion workspace Redis during rollback" >&2
        secure_workdir_cleanup
        exit 1
    fi
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
    if ! remove_unused_pilot_networks; then
        echo "FATAL: unused pilot networks remain after rollback" >&2
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

    # Exactly the reviewed six seats.
    local seat_count
    if ! seat_count="$(compose_cmd config --format json 2>/dev/null \
            | python3 -c "import json,sys; cfg=json.load(sys.stdin); services=list(cfg.get('services',{}).keys()); print(sum(1 for s in services if s.startswith('fin-terminal-public-seat-')))" 2>/dev/null)"; then
        echo "ERROR: could not inspect seat count in merged Compose configuration" >&2
        return 1
    fi
    if [[ "$seat_count" -ne "$PILOT_SEAT_COUNT" ]]; then
        echo "ERROR: expected exactly $PILOT_SEAT_COUNT seats, found $seat_count" >&2
        return 1
    fi

    # Filter checks to pilot services only.
    local pilot_json
    if ! pilot_json="$(compose_cmd config --format json 2>/dev/null \
        | python3 -c "
import json, sys
cfg = json.load(sys.stdin)
pilot = [
    'fin-terminal-public-redis',
    'fin-terminal-public-unbrowser-mcp',
    'fin-terminal-public-seat-01',
    'fin-terminal-public-seat-02',
    'fin-terminal-public-seat-03',
    'fin-terminal-public-seat-04',
    'fin-terminal-public-seat-05',
    'fin-terminal-public-seat-06',
    'fin-terminal-public-gateway',
]
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
if len(data) != 9:
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

    # Validate the exact six-seat endpoint and network-isolation contract before
    # any image is built or container is started.
    local six_seat_contract
    if ! six_seat_contract="$(compose_cmd config --format json 2>/dev/null \
        | python3 -c "
import json, sys

cfg = json.load(sys.stdin)
services = cfg.get('services', {})
network_config = cfg.get('networks', {})
seat_numbers = [f'{value:02d}' for value in range(1, 7)]
seat_services = [f'fin-terminal-public-seat-{value}' for value in seat_numbers]
gateway = services.get('fin-terminal-public-gateway', {})
mcp = services.get('fin-terminal-public-unbrowser-mcp', {})
environment = gateway.get('environment', {})
if str(environment.get('PUBLIC_MAX_SESSIONS')) != '6':
    raise SystemExit(1)
expected_endpoints = [
    f'seat-{value}=http://fin-terminal-public-seat-{value}:8787'
    for value in seat_numbers
]
actual_endpoints = str(environment.get('PUBLIC_WORKER_ENDPOINTS', '')).split(',')
if actual_endpoints != expected_endpoints or len(set(actual_endpoints)) != 6:
    raise SystemExit(1)

def networks(service):
    value = service.get('networks', {})
    return set(value if isinstance(value, list) else value.keys())

expected_gateway = {
    'fin_terminal_public',
    'fin_terminal_public_state',
    'fin_terminal_public_egress',
    *{f'fin_terminal_public_seat_{value}' for value in seat_numbers},
}
if networks(gateway) != expected_gateway:
    raise SystemExit(1)
expected_mcp = {
    'unbrowser_egress_proxy',
    *{f'fin_terminal_public_mcp_{value}' for value in seat_numbers},
}
if networks(mcp) != expected_mcp:
    raise SystemExit(1)
for value, name in zip(seat_numbers, seat_services):
    expected = {
        f'fin_terminal_public_seat_{value}',
        f'fin_terminal_public_egress_{value}',
        f'fin_terminal_public_mcp_{value}',
    }
    if networks(services.get(name, {})) != expected:
        raise SystemExit(1)

expected_subnets = {}
for index, value in enumerate(seat_numbers):
    expected_subnets[f'fin_terminal_public_mcp_{value}'] = f'10.253.0.{index * 8}/29'
    expected_subnets[f'fin_terminal_public_egress_{value}'] = f'10.253.0.{48 + index * 8}/29'
    expected_subnets[f'fin_terminal_public_seat_{value}'] = f'10.253.0.{96 + index * 8}/29'
for name, expected_subnet in expected_subnets.items():
    configs = network_config.get(name, {}).get('ipam', {}).get('config', [])
    actual_subnets = [str(config.get('subnet', '')) for config in configs]
    if actual_subnets != [expected_subnet]:
        raise SystemExit(1)
print('SIX_SEAT_CONTRACT_OK')
" 2>/dev/null)"; then
        echo "ERROR: six-seat endpoint/network isolation validation could not complete" >&2
        return 1
    fi
    if [[ "$six_seat_contract" != "SIX_SEAT_CONTRACT_OK" ]]; then
        echo "ERROR: six-seat endpoint/network isolation contract does not match" >&2
        return 1
    fi

    echo "    Overlay safety: OK (6 isolated seats, no published ports/host networking, hardened runtimes)."
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

check_post_start_capacity() {
    local mem_available_kb mem_total_kb required_kb required_percent_kb
    mem_available_kb="$(awk '/MemAvailable/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
    mem_total_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
    required_kb=$((512 * 1024))
    required_percent_kb=$((mem_total_kb * 15 / 100))
    if [[ "$required_percent_kb" -gt "$required_kb" ]]; then
        required_kb="$required_percent_kb"
    fi
    if [[ "$mem_available_kb" -lt "$required_kb" ]]; then
        echo "ERROR: six-seat startup left less than the required memory reserve" >&2
        return 1
    fi

    local svc cid lifecycle
    for svc in "${PILOT_SERVICES[@]}"; do
        cid="$(resolve_container_id "$svc")"
        lifecycle="$(docker inspect --format '{{.State.OOMKilled}} {{.RestartCount}}' "$cid" 2>/dev/null || true)"
        if [[ "$lifecycle" != "false 0" ]]; then
            echo "ERROR: $svc OOMed or restarted during six-seat startup" >&2
            return 1
        fi
    done
    echo "    Six-seat startup capacity: memory reserve retained; no OOMs/restarts."
    return 0
}

# ---------------------------------------------------------------------------
# Clean partial/leftover pilot containers (ps -aq)
# ---------------------------------------------------------------------------
remove_unused_pilot_networks() {
    local keys=() spec key network_name metadata
    local project_label network_label container_count
    for spec in "${PILOT_EPHEMERAL_NETWORK_SPECS[@]}"; do
        keys+=("${spec%%=*}")
    done
    keys+=("${PILOT_LEGACY_NETWORK_KEYS[@]}")

    local removed=0
    for key in "${keys[@]}"; do
        network_name="${COMPOSE_PROJECT}_${key}"
        if ! docker network inspect "$network_name" >/dev/null 2>&1; then
            continue
        fi
        metadata="$(docker network inspect --format \
            '{{index .Labels "com.docker.compose.project"}}|{{index .Labels "com.docker.compose.network"}}|{{len .Containers}}' \
            "$network_name" 2>/dev/null || true)"
        IFS='|' read -r project_label network_label container_count <<<"$metadata"
        if [[ "$project_label" != "$COMPOSE_PROJECT" || "$network_label" != "$key" ]]; then
            echo "ERROR: refusing to remove network without exact Compose ownership labels: $network_name" >&2
            return 1
        fi
        if [[ "$container_count" != "0" ]]; then
            echo "ERROR: refusing to remove in-use pilot network: $network_name" >&2
            return 1
        fi
        docker network rm "$network_name" >/dev/null || {
            echo "ERROR: could not remove unused pilot network: $network_name" >&2
            return 1
        }
        removed=$((removed + 1))
    done
    echo "    Unused per-seat/legacy pilot networks removed: $removed."
    return 0
}

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
    remove_unused_pilot_networks || return 1
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

    # SQLite online backup before any additive schema migration can run. The
    # workspace control plane adds fin_terminal_* tables/columns on startup;
    # the deploy never migrates without a restorable pre-migration snapshot.
    if ! sqlite_online_backup; then
        echo "ERROR: pre-migration SQLite online backup failed" >&2
        return 1
    fi

    echo "==> All activate gates passed."
    return 0
}

# ---------------------------------------------------------------------------
# Runtime service-set, health, host-port, and network-isolation verification
# ---------------------------------------------------------------------------
validate_runtime_pilot() {
    # In dynamic mode (reconciler enabled) seats may be legitimately stopped
    # while the reconciler scales the warm pool; the shared services and any
    # running seat must still be healthy and the service set exact.
    local dynamic=false
    if dynamic_mode_enabled; then dynamic=true; fi

    # Account for the exact nine-service definition with unambiguous resolved
    # IDs. In dynamic mode a seat is validly absent only when no stopped/stale
    # container for that service remains.
    local svc cid any_cid count=0 running_seats=0
    for svc in "${PILOT_SERVICES[@]}"; do
        cid="$(resolve_container_id "$svc")"
        if [[ -z "$cid" ]] && $dynamic && [[ "$svc" == fin-terminal-public-seat-0[1-6] ]]; then
            any_cid="$(compose_cmd ps -aq "$svc" 2>/dev/null || true)"
            if [[ -n "$any_cid" ]]; then
                echo "ERROR: $svc has a non-running stale container" >&2
                return 1
            fi
            # Reconciler may have drained this seat; absent is valid in dynamic mode.
            count=$((count + 1))
            continue
        fi
        if [[ ! "$cid" =~ ^[0-9a-f]{12,64}$ ]]; then
            echo "ERROR: $svc did not resolve to exactly one Docker container ID" >&2
            return 1
        fi
        if [[ "$(container_state "$svc")" != "healthy" ]]; then
            echo "ERROR: $svc is not healthy during runtime verification" >&2
            return 1
        fi
        if [[ "$svc" == fin-terminal-public-seat-0[1-6] ]]; then
            running_seats=$((running_seats + 1))
        fi
        count=$((count + 1))
    done
    if [[ "$count" -ne "$PILOT_SERVICE_COUNT" ]]; then
        echo "ERROR: expected $PILOT_SERVICE_COUNT pilot containers, found $count" >&2
        return 1
    fi
    echo "    Pilot services: ${PILOT_SERVICE_COUNT}/${PILOT_SERVICE_COUNT} accounted for"
    if $dynamic; then
        if [[ "$running_seats" -lt 1 ]]; then
            echo "ERROR: dynamic runtime has no warm worker" >&2
            return 1
        fi
        echo "    Running warm workers: $running_seats"
        echo "    (dynamic mode: stopped seats are valid; reconciler owns lifecycle)"
    fi

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
    local runtime_public_services expected_public_services unexpected_public_services
    runtime_public_services="$(docker ps -a \
        --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
        --format '{{.Label "com.docker.compose.service"}}' 2>/dev/null \
        | grep '^fin-terminal-public-' | sort -u || true)"
    expected_public_services="$(printf '%s\n' "${PILOT_SERVICES[@]}" | sort)"
    unexpected_public_services="$(comm -23 \
        <(printf '%s\n' "$runtime_public_services") \
        <(printf '%s\n' "$expected_public_services"))"
    if [[ -n "$unexpected_public_services" ]]; then
        echo "ERROR: unexpected public-profile service/container exists" >&2
        return 1
    fi
    if $dynamic; then
        for svc in fin-terminal-public-redis fin-terminal-public-unbrowser-mcp fin-terminal-public-gateway; do
            if ! grep -Fxq "$svc" <<<"$runtime_public_services"; then
                echo "ERROR: required shared runtime service is absent: $svc" >&2
                return 1
            fi
        done
    elif [[ "$runtime_public_services" != "$expected_public_services" ]]; then
        echo "ERROR: runtime public-profile service set is not the reviewed nine-service set" >&2
        return 1
    fi

    # Verify exact runtime network attachments for every pilot service.
    local actual_networks expected_networks
    for svc in "${PILOT_SERVICES[@]}"; do
        cid="$(resolve_container_id "$svc")"
        if [[ -z "$cid" ]] && $dynamic && [[ "$svc" == fin-terminal-public-seat-0[1-6] ]]; then
            continue
        fi
        actual_networks="$(docker inspect \
            --format '{{json .NetworkSettings.Networks}}' "$cid" 2>/dev/null \
            | python3 -c 'import json,sys; data=json.load(sys.stdin); print("\n".join(sorted(data)))')"
        case "$svc" in
            fin-terminal-public-redis)
                expected_networks="${COMPOSE_PROJECT}_fin_terminal_public_state"
                ;;
            fin-terminal-public-unbrowser-mcp)
                expected_networks="$({
                    printf '%s\n' "${COMPOSE_PROJECT}_unbrowser_egress_proxy"
                    local seat_number
                    for seat_number in 01 02 03 04 05 06; do
                        printf '%s\n' "${COMPOSE_PROJECT}_fin_terminal_public_mcp_${seat_number}"
                    done
                } | sort)"
                ;;
            fin-terminal-public-seat-0[1-6])
                local seat_number="${svc##*-seat-}"
                expected_networks="$(printf '%s\n' \
                    "${COMPOSE_PROJECT}_fin_terminal_public_seat_${seat_number}" \
                    "${COMPOSE_PROJECT}_fin_terminal_public_egress_${seat_number}" \
                    "${COMPOSE_PROJECT}_fin_terminal_public_mcp_${seat_number}" | sort)"
                ;;
            fin-terminal-public-gateway)
                expected_networks="$({
                    printf '%s\n' \
                        "${COMPOSE_PROJECT}_fin_terminal_public" \
                        "${COMPOSE_PROJECT}_fin_terminal_public_egress" \
                        "${COMPOSE_PROJECT}_fin_terminal_public_state"
                    local seat_number
                    for seat_number in 01 02 03 04 05 06; do
                        printf '%s\n' "${COMPOSE_PROJECT}_fin_terminal_public_seat_${seat_number}"
                    done
                } | sort)"
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

    # Existing empty Compose networks are not automatically recreated when a
    # subnet declaration changes. Verify every live per-seat bridge uses the
    # exact compact address allocation reviewed in the overlay.
    local spec network_key expected_subnet network_name actual_subnet
    for spec in "${PILOT_EPHEMERAL_NETWORK_SPECS[@]}"; do
        network_key="${spec%%=*}"
        expected_subnet="${spec#*=}"
        network_name="${COMPOSE_PROJECT}_${network_key}"
        actual_subnet="$(docker network inspect --format \
            '{{range .IPAM.Config}}{{.Subnet}}{{end}}' "$network_name" 2>/dev/null || true)"
        if [[ "$actual_subnet" != "$expected_subnet" ]]; then
            echo "ERROR: $network_key runtime subnet differs from reviewed allocation" >&2
            return 1
        fi
    done
    echo "    Per-seat bridge subnets: exact compact allocation verified."

    # Defense in depth: each worker must be unable to resolve every other
    # worker or Redis. Exact network membership above is the primary contract;
    # these live negative probes catch Docker/DNS behavior that differs from the
    # rendered configuration.
    local source target targets=()
    for source in "${PILOT_SEATS[@]}"; do
        targets=(fin-terminal-public-redis)
        for target in "${PILOT_SEATS[@]}"; do
            [[ "$target" == "$source" ]] || targets+=("$target")
        done
        cid="$(resolve_container_id "$source")"
        if [[ -z "$cid" ]] && $dynamic; then
            continue
        fi
        if ! docker exec "$cid" node -e '
const dns = require("dns").promises;
const targets = process.argv.slice(1);
const timeout = (ms) => new Promise((_, reject) => setTimeout(() => reject(Object.assign(new Error("timeout"), {code: "ETIME"})), ms));
Promise.all(targets.map(async (target) => {
  try {
    await Promise.race([dns.lookup(target), timeout(2000)]);
    process.exitCode = 1;
  } catch (error) {
    if (!["ENOTFOUND", "EAI_AGAIN", "ETIME"].includes(error && error.code)) process.exitCode = 1;
  }
})).then(() => process.exit(process.exitCode || 0)).catch(() => process.exit(1));
' "${targets[@]}" 2>/dev/null; then
            echo "ERROR: $source can resolve another seat or Redis" >&2
            return 1
        fi
    done
    echo "    Cross-seat and worker-to-state negative connectivity: verified."

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
        "${PILOT_SEATS[@]}" \
        fin-terminal-public-gateway || return 1

    echo "    Starting Redis..."
    compose_cmd up -d --no-build fin-terminal-public-redis || return 1
    wait_healthy fin-terminal-public-redis || return 1
    echo "    Redis: healthy."
    prepare_six_worker_state || return 1

    echo "    Starting MCP..."
    compose_cmd up -d --no-deps --no-build fin-terminal-public-unbrowser-mcp || return 1
    wait_healthy fin-terminal-public-unbrowser-mcp || return 1
    echo "    MCP: healthy."

    local seat
    for seat in "${PILOT_SEATS[@]}"; do
        echo "    Starting ${seat##fin-terminal-public-}..."
        compose_cmd up -d --no-deps --no-build "$seat" || return 1
        wait_healthy "$seat" || return 1
        echo "    ${seat##fin-terminal-public-}: healthy."
    done

    echo "    Starting gateway..."
    compose_cmd up -d --no-deps --no-build fin-terminal-public-gateway || return 1
    wait_healthy fin-terminal-public-gateway || return 1
    echo "    Gateway: healthy."

    validate_runtime_pilot || return 1
    check_post_start_capacity || return 1

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

    # Prove the shared MCP process can isolate six simultaneous logical
    # sessions. Never print session identifiers or protocol bodies.
    local concurrent_result
    concurrent_result="$(docker exec -i "$mcp_cid" python3 - 2>/dev/null <<'PYMCP'
from concurrent.futures import ThreadPoolExecutor
import http.client
import json

PROTOCOL = "2025-06-18"

def decode(data, content_type):
    if "text/event-stream" in content_type:
        for line in data.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise ValueError("SSE response contained no data event")
    return json.loads(data) if data else None

def post(payload, session_id=""):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["MCP-Session-Id"] = session_id
        headers["MCP-Protocol-Version"] = PROTOCOL
    connection = http.client.HTTPConnection("localhost", 8767, timeout=30)
    connection.request("POST", "/mcp", json.dumps(payload), headers)
    response = connection.getresponse()
    data = response.read().decode("utf-8")
    result = (
        response.status,
        response.getheader("MCP-Session-Id", ""),
        decode(data, response.getheader("Content-Type", "")),
    )
    connection.close()
    return result

def close_session(session_id):
    connection = http.client.HTTPConnection("localhost", 8767, timeout=5)
    connection.request("DELETE", "/mcp", headers={
        "MCP-Session-Id": session_id,
        "MCP-Protocol-Version": PROTOCOL,
    })
    response = connection.getresponse()
    response.read()
    connection.close()
    return response.status in {200, 202, 204}

def check_session(index):
    session_id = ""
    try:
        status, session_id, initialized = post({
            "jsonrpc": "2.0",
            "id": index * 10 + 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "six-seat-check", "version": "1.0"},
            },
        })
        if status != 200 or not session_id or "result" not in initialized:
            raise ValueError("initialize failed")
        status, _, _ = post({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }, session_id)
        if status not in {200, 202, 204}:
            raise ValueError("initialized notification failed")
        status, _, tools = post({
            "jsonrpc": "2.0",
            "id": index * 10 + 2,
            "method": "tools/list",
            "params": {},
        }, session_id)
        names = [tool.get("name") for tool in tools.get("result", {}).get("tools", [])]
        if status != 200 or "navigate" not in names:
            raise ValueError("navigate tool missing")
        status, _, navigation = post({
            "jsonrpc": "2.0",
            "id": index * 10 + 3,
            "method": "tools/call",
            "params": {"name": "navigate", "arguments": {"url": "https://example.com/"}},
        }, session_id)
        result = navigation.get("result", {})
        if status != 200 or result.get("isError") is True or not result.get("content"):
            raise ValueError("public navigation failed")
        return session_id
    finally:
        if session_id and not close_session(session_id):
            raise ValueError("session cleanup failed")

with ThreadPoolExecutor(max_workers=6) as executor:
    session_ids = list(executor.map(check_session, range(1, 7)))
if len(set(session_ids)) != 6:
    raise SystemExit(1)
print("MCP_SIX_OK")
PYMCP
    )" || true
    if [[ "$concurrent_result" != "MCP_SIX_OK" ]]; then
        echo "ERROR: six-session MCP concurrency check failed (scrubbed)" >&2
        return 1
    fi

    echo "    MCP protocol: OK (private rejection + six isolated sessions + cleanup)."
    return 0
}

# ---------------------------------------------------------------------------
# Gateway /api/ready (review item D: resolve container ID, status "ready")
# ---------------------------------------------------------------------------
gateway_internal_ready() {
    echo "    Checking gateway /api/ready internally..."
    local gw_cid attempts dynamic
    gw_cid="$(resolve_container_id fin-terminal-public-gateway)"
    if [[ -z "$gw_cid" ]]; then
        echo "ERROR: could not resolve gateway container ID" >&2
        return 1
    fi
    dynamic=false
    if dynamic_mode_enabled; then dynamic=true; fi

    for attempts in $(seq 1 30); do
        if docker exec "$gw_cid" node -e '
const dynamic = process.argv[1] === "true";
const http = require("http");
const endpoints = Array.from({length: 6}, (_, index) => {
  const seat = String(index + 1).padStart(2, "0");
  return `http://fin-terminal-public-seat-${seat}:8787/api/ready`;
});
const getJson = (url) => new Promise((resolve, reject) => {
  const req = http.get(url, (res) => {
    let data = "";
    res.on("data", chunk => data += chunk);
    res.on("end", () => {
      if (res.statusCode < 200 || res.statusCode >= 300) return reject(new Error("status"));
      try { resolve(JSON.parse(data)); } catch { reject(new Error("json")); }
    });
  });
  req.on("error", reject);
  req.setTimeout(5000, () => req.destroy(new Error("timeout")));
});
Promise.all([
  getJson("http://127.0.0.1:8788/api/ready"),
  Promise.allSettled(endpoints.map(getJson)),
])
  .then(([gateway, workerResults]) => {
    if (gateway.status !== "ready") process.exit(1);
    const workers = workerResults
      .filter(result => result.status === "fulfilled")
      .map(result => result.value);
    const healthy = workers.filter(w => w && w.publicWorker === true && typeof w.instanceId === "string" && w.instanceId.length >= 16);
    if (dynamic) {
      // Reconciler owns seat start/stop: accept any consistent warm pool with
      // the one-warm-spare invariant (never require six stopped seats).
      if (gateway.readyWorkers !== healthy.length || healthy.length < 1) process.exit(1);
      const generations = healthy.map(w => w.instanceId);
      if (new Set(generations).size !== healthy.length) process.exit(1);
      process.exit(0);
    }
    if (gateway.assignedWorkers !== 0 || gateway.queuedVisitors !== 0) process.exit(1);
    if (gateway.readyWorkers !== 6 || healthy.length !== 6) process.exit(1);
    const generations = healthy.map(w => w.instanceId);
    if (new Set(generations).size !== 6) process.exit(1);
    process.exit(0);
  })
  .catch(() => process.exit(1));
' "$dynamic" 2>/dev/null; then
            if [[ "$dynamic" == "true" ]]; then
                echo "    Gateway /api/ready: warm pool ready (dynamic mode)."
            else
                echo "    Gateway /api/ready: six unique workers ready."
            fi
            return 0
        fi
        sleep 2
    done
    echo "ERROR: gateway did not report a ready pool internally" >&2
    return 1
}

# ---------------------------------------------------------------------------
# Private research-permit gate verification
# ---------------------------------------------------------------------------
verify_research_gate_metrics() {
    local gw_cid result
    gw_cid="$(resolve_container_id fin-terminal-public-gateway)"
    [[ "$gw_cid" =~ ^[0-9a-f]{12,64}$ ]] || {
        echo "ERROR: gateway container is unavailable for permit verification" >&2
        return 1
    }
    result="$(docker exec -i "$gw_cid" node 2>/dev/null <<'NODE'
const http = require("http");
const token = process.env.TERMINAL_RUNTIME_MANAGEMENT_TOKEN || "";
if (process.env.TERMINAL_RUNTIME_FEATURE_ENABLED !== "true" || !/^[0-9a-f]{64}$/.test(token)) process.exit(1);
const request = (method, path, body, suppliedToken = token) => new Promise((resolve, reject) => {
  const payload = body === undefined ? "" : JSON.stringify(body);
  const req = http.request({
    hostname: "127.0.0.1",
    port: 8789,
    path,
    method,
    headers: {
      "X-Management-Token": suppliedToken,
      ...(payload ? {"Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload)} : {}),
    },
    timeout: 5000,
  }, (res) => {
    let data = "";
    res.on("data", chunk => data += chunk);
    res.on("end", () => {
      let parsed;
      try { parsed = data ? JSON.parse(data) : undefined; } catch {}
      resolve({status: res.statusCode, body: parsed});
    });
  });
  req.on("error", reject);
  req.on("timeout", () => req.destroy(new Error("timeout")));
  if (payload) req.write(payload);
  req.end();
});
(async () => {
  const unauthorized = await request("GET", "/api/management/research", undefined, "");
  const wrong = await request("GET", "/api/management/research", undefined, "0".repeat(64));
  const metrics = await request("GET", "/api/management/research");
  const snapshot = await request("POST", "/api/management/reconcile-snapshot", {});
  const research = metrics.body && metrics.body.research;
  if (unauthorized.status !== 401 || wrong.status !== 401 || metrics.status !== 200 || snapshot.status !== 200) process.exit(1);
  if (!research || research.maxConcurrent !== 2) process.exit(1);
  if (!Number.isInteger(research.acquired) || research.acquired < 0 || research.acquired > 2) process.exit(1);
  if (!Number.isInteger(research.queued) || research.queued < 0) process.exit(1);
  const expectedSeats = Array.from({length: 6}, (_, index) => `seat-${String(index + 1).padStart(2, "0")}`);
  const seats = snapshot.body && snapshot.body.seats;
  const actualSeats = seats && typeof seats === "object" ? Object.keys(seats).sort() : [];
  if (snapshot.body.version !== 1 || JSON.stringify(actualSeats) !== JSON.stringify(expectedSeats)) process.exit(1);
  if (!snapshot.body.plan || !Number.isInteger(snapshot.body.plan.desiredRunning)
      || snapshot.body.plan.desiredRunning < 1 || snapshot.body.plan.desiredRunning > 6) process.exit(1);
  process.stdout.write("RESEARCH_GATE_METRICS_OK");
})().catch(() => process.exit(1));
NODE
)" || true
    if [[ "$result" != "RESEARCH_GATE_METRICS_OK" ]]; then
        echo "ERROR: private research-permit metrics/auth verification failed" >&2
        return 1
    fi
    echo "    Research permit coordinator: authenticated maxConcurrent=2."
}

prove_research_gate_fifo() {
    local gw_cid result
    gw_cid="$(resolve_container_id fin-terminal-public-gateway)"
    [[ "$gw_cid" =~ ^[0-9a-f]{12,64}$ ]] || return 1
    result="$(docker exec -i "$gw_cid" node 2>/dev/null <<'NODE'
const http = require("http");
const token = process.env.TERMINAL_RUNTIME_MANAGEMENT_TOKEN || "";
const prefix = `activation-${Date.now()}-${Math.random().toString(16).slice(2)}`;
const ids = [`${prefix}-a`, `${prefix}-b`, `${prefix}-c`];
const request = (method, path, body) => new Promise((resolve, reject) => {
  const payload = body === undefined ? "" : JSON.stringify(body);
  const req = http.request({
    hostname: "127.0.0.1", port: 8789, path, method, timeout: 5000,
    headers: {
      "X-Management-Token": token,
      ...(payload ? {"Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload)} : {}),
    },
  }, (res) => {
    let data = "";
    res.on("data", chunk => data += chunk);
    res.on("end", () => {
      let parsed;
      try { parsed = data ? JSON.parse(data) : undefined; } catch {}
      resolve({status: res.statusCode, body: parsed});
    });
  });
  req.on("error", reject);
  req.on("timeout", () => req.destroy(new Error("timeout")));
  if (payload) req.write(payload);
  req.end();
});
const release = async (requestId) => {
  try { await request("POST", "/api/management/research-permits/release", {requestId}); } catch {}
};
(async () => {
  if (!/^[0-9a-f]{64}$/.test(token)) process.exit(1);
  const before = await request("GET", "/api/management/research");
  const initial = before.body && before.body.research;
  if (before.status !== 200 || !initial || initial.maxConcurrent !== 2 || initial.acquired !== 0 || initial.queued !== 0) process.exit(1);
  try {
    const outcomes = [];
    for (let index = 0; index < ids.length; index += 1) {
      outcomes.push(await request("POST", "/api/management/research-permits/acquire", {
        sessionId: `${prefix}-session-${index}`,
        workerGeneration: `${prefix}-generation-${index}`,
        requestId: ids[index],
      }));
    }
    if (outcomes.some(value => value.status !== 200)) throw new Error("acquire status");
    if (outcomes[0].body.status !== "acquired" || outcomes[1].body.status !== "acquired") throw new Error("first two not acquired");
    if (outcomes[2].body.status !== "queued" || outcomes[2].body.queuePosition !== 1) throw new Error("third not FIFO queued");
    await release(ids[0]);
    const promoted = await request("POST", "/api/management/research-permits/status", {requestId: ids[2]});
    if (promoted.status !== 200 || promoted.body.status !== "acquired") throw new Error("queued permit not promoted");
  } finally {
    await Promise.all(ids.map(release));
  }
  const after = await request("GET", "/api/management/research");
  const final = after.body && after.body.research;
  if (after.status !== 200 || !final || final.maxConcurrent !== 2 || final.acquired !== 0 || final.queued !== 0) process.exit(1);
  process.stdout.write("RESEARCH_GATE_FIFO_OK");
})().catch(async () => {
  await Promise.all(ids.map(release));
  process.exit(1);
});
NODE
)" || true
    if [[ "$result" != "RESEARCH_GATE_FIFO_OK" ]]; then
        echo "ERROR: research-permit max-two/FIFO proof failed (details scrubbed)" >&2
        return 1
    fi

    # Prove each currently running worker has the feature, token, and private
    # route needed by its real permit client. Stopped dynamic seats are skipped.
    local seat cid running_count=0
    for seat in "${PILOT_SEATS[@]}"; do
        cid="$(resolve_container_id "$seat")"
        [[ -n "$cid" ]] || continue
        running_count=$((running_count + 1))
        if ! docker exec "$cid" node -e '
const token = process.env.TERMINAL_RUNTIME_MANAGEMENT_TOKEN || "";
const base = process.env.TERMINAL_RUNTIME_MANAGEMENT_URL || "";
if (process.env.TERMINAL_RUNTIME_FEATURE_ENABLED !== "true" || !/^[0-9a-f]{64}$/.test(token)) process.exit(1);
if (base !== "http://fin-terminal-public-gateway:8789") process.exit(1);
fetch(`${base}/api/management/research-permits/status`, {
  method: "POST",
  headers: {"content-type": "application/json", "x-management-token": token},
  body: JSON.stringify({requestId: "activation-probe-not-found"}),
  signal: AbortSignal.timeout(5000),
}).then(async response => {
  if (!response.ok) process.exit(1);
  const body = await response.json();
  process.exit(body.status === "not-found" ? 0 : 1);
}).catch(() => process.exit(1));
' >/dev/null 2>&1; then
            echo "ERROR: $seat cannot reach its authenticated private permit path" >&2
            return 1
        fi
    done
    if [[ "$running_count" -lt 1 ]]; then
        echo "ERROR: no running worker was available for permit-path verification" >&2
        return 1
    fi
    echo "    Research permit gate: max-two FIFO and $running_count worker path(s) verified."
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

    # Step 1: Snapshot preactivation .env in secure workdir. Dynamic activation
    # takes this snapshot before installing its feature/token configuration, so
    # never overwrite an already-armed transaction snapshot here.
    if [[ -z "$ROLLBACK_SNAPSHOT" ]]; then
        ROLLBACK_SNAPSHOT="$SECURE_WORKDIR/preactivate.env"
        snapshot_env "$ROLLBACK_SNAPSHOT"
        echo "    Pre-activation .env snapshot saved."
    else
        echo "    Pre-activation .env snapshot already secured."
    fi

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

    local dynamic
    dynamic="$(read_dynamic_mode_enabled)"
    echo "    TERMINAL_RUNTIME_FEATURE_ENABLED: $dynamic"

    local reconciler_state="not-installed"
    if command -v systemctl >/dev/null 2>&1; then
        if systemctl cat terminal-runtime-reconciler >/dev/null 2>&1; then
            if systemctl is-active --quiet terminal-runtime-reconciler 2>/dev/null; then
                reconciler_state="active"
            else
                reconciler_state="inactive"
            fi
        fi
    fi
    echo "    terminal-runtime-reconciler: $reconciler_state"
    if [[ -f "$RUNTIME_METADATA_FILE" && ! -L "$RUNTIME_METADATA_FILE" ]]; then
        echo "    terminal-runtime-managed: yes"
    else
        echo "    terminal-runtime-managed: no"
    fi

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

    # Retained, bounded systemd journal evidence even after fail-closed removal.
    reconciler_diagnostics
}

# ---------------------------------------------------------------------------
# Action: activate-runtime
# ---------------------------------------------------------------------------
cmd_activate_runtime() {
    echo "==> Activating public-terminal runtime pilot..."
    echo "    Expected revision: $EXPECTED_SHA"

    local enabled
    enabled="$(read_public_enabled)"
    if [[ "$enabled" == "true" ]]; then
        if [[ "$(read_dynamic_mode_enabled)" == "true" ]] \
            && verify_runtime_artifacts \
            && runtime_install_matches_metadata \
            && verify_runtime_config_consistency \
            && sudo systemctl is-active --quiet terminal-runtime-reconciler \
            && validate_runtime_pilot \
            && gateway_internal_ready \
            && verify_research_gate_metrics \
            && verify_reconciler_cycle \
            && verify_live_edge_surface; then
            echo "==> Runtime pilot already active and healthy (idempotent success)."
            return 0
        fi
        echo "==> Runtime pilot is degraded; disabling the edge fail-closed..."
        cmd_disable
        echo "==> Pilot disabled. Re-run activate-runtime when ready."
        return 1
    fi

    secure_workdir_init
    run_activate_gates || exit 1
    clean_partial_pilot || exit 1

    # Secure the exact pre-runtime environment before adding the on-host token
    # or feature flag. The activation trap owns every mutation from here.
    ROLLBACK_SNAPSHOT="$SECURE_WORKDIR/preactivate.env"
    snapshot_env "$ROLLBACK_SNAPSHOT"
    arm_activate_rollback
    prepare_dynamic_runtime || exit 1
    build_and_start_pilot || exit 1
    mcp_protocol_check || exit 1
    gateway_internal_ready || exit 1
    prove_research_gate_fifo || exit 1
    start_reconciler || exit 1
    promote_edge || exit 1

    # Disarm rollback — success.
    trap - EXIT INT TERM HUP
    ROLLBACK_ARMED=false
    DYNAMIC_SETUP_STARTED=false
    secure_workdir_cleanup
    echo "==> Public-terminal runtime pilot activated successfully."
    echo "    Post-unlock reconciler verification must pass before workflow success."
    echo "    Human Turnstile/session test should follow outside this script."
}

# ---------------------------------------------------------------------------
# Action: verify-runtime — post-unlock release gate run by the workflow
# ---------------------------------------------------------------------------
cmd_verify_runtime() {
    echo "==> Verifying active public-terminal runtime..."
    if [[ "$(read_public_enabled)" != "true" ]] \
        || [[ "$(read_dynamic_mode_enabled)" != "true" ]]; then
        echo "ERROR: runtime pilot flags are not both enabled" >&2
        return 1
    fi
    verify_runtime_artifacts || return 1
    runtime_install_matches_metadata || {
        echo "ERROR: managed runtime install does not match deployment metadata" >&2
        return 1
    }
    verify_runtime_config_consistency || {
        echo "ERROR: runtime token/feature configuration is inconsistent" >&2
        return 1
    }
    sudo systemctl is-enabled --quiet terminal-runtime-reconciler || {
        echo "ERROR: terminal-runtime-reconciler is not enabled" >&2
        return 1
    }
    sudo systemctl is-active --quiet terminal-runtime-reconciler || {
        echo "ERROR: terminal-runtime-reconciler is not active" >&2
        return 1
    }
    verify_reconciler_cycle || return 1
    validate_runtime_pilot || return 1
    gateway_internal_ready || return 1
    verify_research_gate_metrics || return 1
    verify_live_edge_surface || return 1
    echo "==> Runtime pilot verification passed."
}

# ---------------------------------------------------------------------------
# Action: disable
# ---------------------------------------------------------------------------
cmd_disable() {
    echo "==> Disabling public-terminal pilot..."
    secure_workdir_init

    local enabled dynamic_before managed_runtime=false runtime_cleanup_error=false
    if ! enabled="$(read_public_enabled)"; then
        enabled="invalid"
    fi
    dynamic_before="$(read_dynamic_mode_enabled)"
    if [[ -f "$RUNTIME_METADATA_FILE" && ! -L "$RUNTIME_METADATA_FILE" ]]; then
        managed_runtime=true
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

    # The edge is closed before the host lifecycle controller is stopped. Use
    # disable --now so a reboot cannot resurrect it against absent services.
    if [[ "$dynamic_before" == "true" ]] || $managed_runtime \
        || systemctl is-active --quiet terminal-runtime-reconciler 2>/dev/null; then
        echo "    Stopping host runtime reconciler..."
        stop_reconciler || {
            echo "ERROR: pilot is 404 but the runtime reconciler could not be stopped" >&2
            exit 1
        }
        echo "    terminal-runtime-reconciler: stopped and disabled."
    fi

    # Step 4: Stop the gateway first, transition persisted state back to the
    # rollback-compatible one-seat shape, then remove the remaining services.
    if pilot_any_container_present; then
        echo "    Stopping pilot services..."
        compose_cmd stop fin-terminal-public-gateway 2>/dev/null || true
        local redis_cid
        redis_cid="$(resolve_container_id fin-terminal-public-redis)"
        if [[ -n "$redis_cid" ]]; then
            transition_redis_worker_set 1 - || {
                echo "ERROR: could not restore rollback-compatible admission state" >&2
                exit 1
            }
            # Clean workspace/checkpoint companion keys while Redis is still
            # running; the retained volume must not carry them across pilots.
            cleanup_workspace_redis || {
                echo "ERROR: could not clean companion workspace Redis state" >&2
                exit 1
            }
            delete_runtime_redis_state || {
                echo "ERROR: could not clean runtime capacity/permit Redis state" >&2
                exit 1
            }
        fi
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
    remove_unused_pilot_networks || {
        echo "ERROR: unused pilot networks remain after disable" >&2
        exit 1
    }

    # Remove only an install whose owner-only metadata and hashes prove that it
    # was created by this activation path. Unknown systemd files remain stopped
    # for operator inspection instead of being guessed away.
    if $managed_runtime; then
        if ! remove_managed_runtime_install false; then
            runtime_cleanup_error=true
            echo "ERROR: managed runtime files could not be removed automatically" >&2
        else
            echo "    Managed runtime configuration: removed."
        fi
    fi

    # Return the production .env to the default-off baseline only after the
    # reconciler and every token-consuming container have stopped.
    if [[ "$dynamic_before" != "false" ]]; then
        update_env_flag "$ENV_FILE" "$RUNTIME_FEATURE_FLAG" "false" || {
            echo "ERROR: could not reset $RUNTIME_FEATURE_FLAG=false" >&2
            exit 1
        }
    fi
    update_env_flag "$ENV_FILE" "$RUNTIME_MANAGEMENT_TOKEN_KEY" "" || {
        echo "ERROR: could not clear the runtime management token" >&2
        exit 1
    }

    # Step 5: Verify normal routes.
    echo "==> Verifying normal routes..."
    assert_http_status "$PRIMARY_HOST" "https://${PRIMARY_HOST}/health" "200" "health" || { secure_workdir_cleanup; exit 1; }
    echo "    Health: 200."
    assert_http_status "$PUBLIC_HOST" "https://${PUBLIC_HOST}/" "200" "root" || { secure_workdir_cleanup; exit 1; }
    echo "    Root: 200."
    assert_http_status "$PUBLIC_HOST" "https://${PUBLIC_HOST}/fin-terminal/" "401" "signed terminal" || { secure_workdir_cleanup; exit 1; }
    echo "    Signed terminal: 401."
    check_retired_demo_404 || { secure_workdir_cleanup; exit 1; }

    if $runtime_cleanup_error; then
        echo "ERROR: pilot is disabled, but managed runtime-file cleanup needs operator attention" >&2
        secure_workdir_cleanup
        return 1
    fi

    secure_workdir_cleanup
    echo "==> Public-terminal pilot disabled."
    echo "    Redis volume retained for diagnosis."
}

# ---------------------------------------------------------------------------
# Action: rollback — compatibility alias for fail-closed disable.
#
# A feature-disabled static-six fallback would remove the global max-two
# research gate and is therefore not an acceptable production rollback.
# ---------------------------------------------------------------------------
cmd_rollback() {
    echo "==> Rollback is fail-closed: disabling the public-terminal pilot."
    cmd_disable
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
        status)           cmd_status ;;
        activate-runtime) cmd_activate_runtime ;;
        verify-runtime)   cmd_verify_runtime ;;
        disable)          cmd_disable ;;
        rollback)         cmd_rollback ;;
    esac

    local exit_code=$?
    release_lock
    secure_workdir_cleanup
    trap - EXIT
    exit "$exit_code"
}

main
