#!/usr/bin/env bash
# Activate the authenticated browser-terminal canary on the production host.
# This script is streamed over verified SSH by the protected GitHub Action; it
# intentionally changes only the browser-canary env values and Caddy/service
# state, leaving the Pi-owned /fin-terminal/ route untouched.

set -euo pipefail
umask 077

if [[ "$#" -ne 3 ]]; then
    echo "usage: $0 REMOTE_DIR IMAGE PUBLIC_URL" >&2
    exit 2
fi

remote_dir="$1"
image="$2"
public_url="$3"
env_file="$remote_dir/.env"
compose_args=(
    --profile fin-terminal-browser-canary
    -f "$remote_dir/docker-compose.yml"
    -f "$remote_dir/docker-compose.browser-terminal.yml"
)

[[ "$remote_dir" = /* ]] || { echo "REMOTE_DIR must be absolute" >&2; exit 2; }
[[ "$image" =~ ^[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[0-9a-f]{64}$ ]] || {
    echo "IMAGE must be an immutable digest-pinned reference" >&2
    exit 2
}
[[ "$public_url" =~ ^https://[A-Za-z0-9.-]+/fin-terminal-browser/$ ]] || {
    echo "PUBLIC_URL must be the canonical HTTPS browser-terminal URL" >&2
    exit 2
}
[[ -f "$env_file" && ! -L "$env_file" ]] || {
    echo "production .env is missing or symlinked" >&2
    exit 1
}
cd "$remote_dir"

get_env_value() {
    local name="$1"
    ENV_FILE="$env_file" ENV_NAME="$name" python3 - <<'PY'
import os

name = os.environ["ENV_NAME"]
with open(os.environ["ENV_FILE"], encoding="utf-8") as handle:
    for raw in handle:
        line = raw.rstrip("\r\n")
        if line.startswith(name + "="):
            print(line[len(name) + 1:])
            break
PY
}

set_env_value() {
    local name="$1"
    local value="$2"
    ENV_FILE="$env_file" ENV_NAME="$name" ENV_VALUE="$value" python3 - <<'PY'
import os
import tempfile

path = os.environ["ENV_FILE"]
name = os.environ["ENV_NAME"]
value = os.environ["ENV_VALUE"]
prefix = name + "="
with open(path, encoding="utf-8") as handle:
    lines = handle.readlines()

replacement = prefix + value + "\n"
found = False
updated = []
for line in lines:
    if line.rstrip("\r\n").startswith(prefix):
        if not found:
            updated.append(replacement)
            found = True
        continue
    updated.append(line)
if not found:
    if updated and not updated[-1].endswith("\n"):
        updated[-1] += "\n"
    updated.append(replacement)

directory = os.path.dirname(path) or "."
fd, temp_path = tempfile.mkstemp(prefix=".browser-canary-env.", dir=directory, text=True)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.writelines(updated)
    os.replace(temp_path, path)
finally:
    try:
        os.unlink(temp_path)
    except FileNotFoundError:
        pass
PY
}

wait_for_health() {
    local service="$1"
    local container health
    for _ in $(seq 1 60); do
        container="$(docker compose "${compose_args[@]}" ps -q "$service")"
        if [[ "$container" =~ ^[0-9a-f]{12,64}$ ]]; then
            health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}starting{{end}}' "$container")"
            if [[ "$health" == "healthy" ]]; then
                return 0
            fi
        fi
        sleep 2
    done
    echo "timed out waiting for $service health" >&2
    docker compose "${compose_args[@]}" logs --tail 80 "$service" >&2 || true
    return 1
}

wait_for_public_status() {
    local status
    for _ in $(seq 1 30); do
        status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
            --connect-timeout 5 --max-time 10 "$public_url" || true)"
        case "$status" in
            401|403)
                printf '%s\n' "$status"
                return 0
                ;;
        esac
        sleep 2
    done
    echo "timed out waiting for enabled browser route (last HTTP status: $status)" >&2
    return 1
}

backup_dir="$(mktemp -d "$remote_dir/.browser-canary-activation.XXXXXX")"
chmod 700 "$backup_dir"
cp -p -- "$env_file" "$backup_dir/.env"
completed=false
rollback() {
    if [[ "$completed" == "true" ]]; then
        rm -rf -- "$backup_dir"
        return
    fi
    echo "activation failed; restoring the previous browser-canary state" >&2
    docker compose "${compose_args[@]}" stop fin-terminal-browser fin-terminal-browser-mcp >/dev/null 2>&1 || true
    cp -p -- "$backup_dir/.env" "$env_file" || true
    chmod 600 "$env_file" || true
    # Recreate only from the base stack: the previous .env may not have had
    # browser image/token values, so the optional overlay may not render during
    # rollback. The base Caddy route defaults to disabled.
    docker compose -f "$remote_dir/docker-compose.yml" up -d --no-deps --no-build \
        --pull never --force-recreate caddy >/dev/null 2>&1 || true
    rm -rf -- "$backup_dir"
}
trap rollback EXIT

old_enabled="$(get_env_value FIN_TERMINAL_BROWSER_ENABLED || true)"
if [[ "$old_enabled" == "true" ]]; then
    echo "browser-terminal canary is already enabled" >&2
    exit 1
fi

set_env_value FIN_TERMINAL_BROWSER_IMAGE "$image"
browser_token="$(get_env_value FIN_TERMINAL_BROWSER_PROXY_TOKEN || true)"
if [[ -z "$browser_token" ]]; then
    browser_token="$(openssl rand -hex 32)"
    set_env_value FIN_TERMINAL_BROWSER_PROXY_TOKEN "$browser_token"
fi
pi_token="$(get_env_value FIN_TERMINAL_PROXY_TOKEN || true)"
if [[ "${#browser_token}" -lt 32 || "$browser_token" == "$pi_token" ]]; then
    echo "browser proxy token is missing, too short, or not independent" >&2
    exit 1
fi

# Keep the route disabled while preflight and service health checks run.
set_env_value FIN_TERMINAL_BROWSER_ENABLED false
"$remote_dir/deploy/browser_terminal_canary_preflight.sh"
docker compose "${compose_args[@]}" up -d --build fin-terminal-browser-mcp fin-terminal-browser
wait_for_health fin-terminal-browser-mcp
wait_for_health fin-terminal-browser

set_env_value FIN_TERMINAL_BROWSER_ENABLED true
docker compose "${compose_args[@]}" up -d --no-deps --no-build --pull never \
    --force-recreate caddy

status="$(wait_for_public_status)"

completed=true
echo "browser-terminal canary enabled; unauthenticated probe returned HTTP $status"
