#!/usr/bin/env bash
# Activate or upgrade the authenticated browser-terminal canary on the
# production host. This script is streamed over verified SSH by the protected
# GitHub Action; it intentionally changes only the browser-canary env values
# and Caddy/service state, leaving the Pi-owned /fin-terminal/ route untouched.

set -euo pipefail
umask 077

if [[ "$#" -ne 5 ]]; then
    echo "usage: $0 REMOTE_DIR IMAGE PUBLIC_URL SMOKE_COOKIE_FILE EXPECTED_INFRA_SHA" >&2
    exit 2
fi

remote_dir="$1"
image="$2"
public_url="$3"
smoke_cookie_file="$4"
expected_infra_sha="$5"
env_file="$remote_dir/.env"
compose_args=(
    --profile fin-terminal-browser-canary
    -f "$remote_dir/docker-compose.yml"
    -f "$remote_dir/docker-compose.browser-terminal.yml"
)

[[ "$remote_dir" = /* ]] || { echo "REMOTE_DIR must be absolute" >&2; exit 2; }
[[ "$smoke_cookie_file" = /* ]] || { echo "SMOKE_COOKIE_FILE must be absolute" >&2; exit 2; }
[[ "$expected_infra_sha" =~ ^[0-9a-f]{40}$ ]] || {
    echo "EXPECTED_INFRA_SHA must be a 40-character lowercase Git SHA" >&2
    exit 2
}
[[ "$image" =~ ^[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[0-9a-f]{64}$ ]] || {
    echo "IMAGE must be an immutable digest-pinned reference" >&2
    exit 2
}
[[ "$public_url" =~ ^https://[A-Za-z0-9.-]+/fin-terminal-browser/$ ]] || {
    echo "PUBLIC_URL must be the canonical HTTPS browser-terminal URL" >&2
    exit 2
}
[[ -f "$smoke_cookie_file" && ! -L "$smoke_cookie_file" ]] || {
    echo "SMOKE_COOKIE_FILE is missing or symlinked" >&2
    exit 1
}
smoke_cookie="$(<"$smoke_cookie_file")"
[[ -n "$smoke_cookie" && "${#smoke_cookie}" -le 16384 && "$smoke_cookie" != *$'\n'* && "$smoke_cookie" != *$'\r'* ]] || {
    echo "SMOKE_COOKIE_FILE must contain one bounded cookie value" >&2
    exit 1
}
cd "$remote_dir"

exec 9>>"$remote_dir/.deploy.lock"
if ! flock -n 9; then
    echo "deployment lock is already held" >&2
    exit 75
fi

validate_deployed_release_identity() {
    REMOTE_DIR="$remote_dir" EXPECTED_INFRA_SHA="$expected_infra_sha" python3 - <<'PY'
import datetime as dt
import os
import re
import stat
import sys

path = os.path.join(os.environ["REMOTE_DIR"], ".deploy-current")
expected = os.environ["EXPECTED_INFRA_SHA"]
maximum_size = 1024


def fail(message):
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


try:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
except OSError:
    fail("deployed release metadata is missing or unsafe")

try:
    metadata_stat = os.fstat(fd)
    if not stat.S_ISREG(metadata_stat.st_mode):
        fail("deployed release metadata is not a regular file")
    if metadata_stat.st_uid != os.geteuid():
        fail("deployed release metadata is not owned by the activation user")
    if stat.S_IMODE(metadata_stat.st_mode) != 0o600:
        fail("deployed release metadata must have mode 0600")
    raw = os.read(fd, maximum_size + 1)
finally:
    os.close(fd)

if len(raw) > maximum_size:
    fail("deployed release metadata is too large")
try:
    text = raw.decode("utf-8")
except UnicodeDecodeError:
    fail("deployed release metadata is not valid UTF-8")
if "\r" in text or not text.endswith("\n"):
    fail("deployed release metadata has an invalid line ending")

lines = text[:-1].split("\n")
fields = {}
for line in lines:
    if not line or line.count("=") != 1:
        fail("deployed release metadata contains an invalid field")
    name, value = line.split("=", 1)
    if name not in {"revision", "deploy_id", "deployed_at"}:
        fail(f"deployed release metadata contains an unknown field: {name}")
    if name in fields:
        fail(f"deployed release metadata contains a duplicate field: {name}")
    fields[name] = value

if set(fields) != {"revision", "deploy_id", "deployed_at"}:
    fail("deployed release metadata is missing a required field")
if not re.fullmatch(r"[0-9a-f]{40}", fields["revision"]):
    fail("deployed release metadata revision is not a lowercase Git SHA")
if not re.fullmatch(r"[0-9a-f]{24}", fields["deploy_id"]):
    fail("deployed release metadata deploy_id is not valid")
if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", fields["deployed_at"]):
    fail("deployed release metadata timestamp is not UTC")
try:
    dt.datetime.strptime(fields["deployed_at"], "%Y-%m-%dT%H:%M:%SZ")
except ValueError:
    fail("deployed release metadata timestamp is invalid")
if fields["revision"] != expected:
    fail(
        "deployed release revision does not match the reviewed infra revision "
        f"(expected {expected}, actual {fields['revision']})"
    )
PY
}

validate_deployed_release_identity
[[ -f "$env_file" && ! -L "$env_file" ]] || {
    echo "production .env is missing or symlinked" >&2
    exit 1
}

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
        401)
                printf '%s\n' "$status"
                return 0
                ;;
        esac
        sleep 2
    done
    echo "timed out waiting for enabled browser route (last HTTP status: $status)" >&2
    return 1
}

wait_for_authenticated_status() {
    local status session_status
    local session_url="${public_url%/}/api/browser/v1/session"
    for _ in $(seq 1 30); do
        status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
            --connect-timeout 5 --max-time 10 --cookie "$smoke_cookie" "$public_url" || true)"
        session_status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
            --connect-timeout 5 --max-time 10 --cookie "$smoke_cookie" "$session_url" || true)"
        if [[ "$status" == "200" && "$session_status" == "200" ]]; then
            printf '%s/%s\n' "$status" "$session_status"
            return 0
        fi
        if [[ "$status" == "401" || "$status" == "403" || "$session_status" == "401" || "$session_status" == "403" ]]; then
            echo "authenticated smoke probe was denied (page HTTP $status, session HTTP $session_status)" >&2
            return 1
        fi
        sleep 2
    done
    echo "timed out waiting for authenticated browser route (last page/session status: $status/$session_status)" >&2
    return 1
}

backup_dir="$(mktemp -d "$remote_dir/.browser-canary-activation.XXXXXX")"
chmod 700 "$backup_dir"
cp -p -- "$env_file" "$backup_dir/.env"
completed=false
old_enabled=""
old_mcp_image_ref=""
old_mcp_image_id=""
rollback() {
    if [[ "$completed" == "true" ]]; then
        rm -rf -- "$backup_dir"
        return
    fi
    echo "activation failed; restoring the previous browser-canary state" >&2
    if ! cp -p -- "$backup_dir/.env" "$env_file"; then
        echo "ERROR: failed to restore the previous browser-canary environment" >&2
    elif ! chmod 600 "$env_file"; then
        echo "ERROR: failed to restore production .env permissions" >&2
    fi
    if [[ "$old_enabled" == "true" ]]; then
        # The previous route was live. Restore the old digest and backend before
        # asking Caddy to resolve the service again; never roll a live upgrade
        # back to the disabled base-stack route.
        if [[ -z "$old_mcp_image_ref" || -z "$old_mcp_image_id" ]]; then
            echo "ERROR: previous MCP sidecar image was not captured; refusing an incomplete rollback" >&2
        elif ! docker tag "$old_mcp_image_id" "$old_mcp_image_ref"; then
            echo "ERROR: failed to restore the previous MCP sidecar image tag" >&2
        elif ! docker compose "${compose_args[@]}" up -d --no-deps --no-build --pull never \
            --force-recreate fin-terminal-browser-mcp; then
            echo "ERROR: failed to recreate the previous MCP sidecar" >&2
        elif ! wait_for_health fin-terminal-browser-mcp; then
            echo "ERROR: previous MCP sidecar did not become healthy" >&2
        fi
        if ! docker compose "${compose_args[@]}" up -d --no-deps --no-build --pull never \
            --force-recreate fin-terminal-browser; then
            echo "ERROR: failed to recreate the previous browser canary image" >&2
        elif ! wait_for_health fin-terminal-browser; then
            echo "ERROR: previous browser canary image did not become healthy" >&2
        elif ! docker compose "${compose_args[@]}" exec -T caddy caddy reload \
            --config /etc/caddy/Caddyfile </dev/null; then
            echo "ERROR: Caddy could not reload the restored browser canary route" >&2
        fi
    else
        docker compose "${compose_args[@]}" stop fin-terminal-browser fin-terminal-browser-mcp >/dev/null 2>&1 || true
        # Recreate only from the base stack: the previous .env may not have had
        # browser image/token values, so the optional overlay may not render during
        # rollback. The base Caddy route defaults to disabled.
        docker compose -f "$remote_dir/docker-compose.yml" up -d --no-deps --no-build \
            --pull never --force-recreate caddy >/dev/null 2>&1 || true
    fi
    rm -rf -- "$backup_dir"
}
trap rollback EXIT

old_enabled="$(get_env_value FIN_TERMINAL_BROWSER_ENABLED || true)"
if [[ "$old_enabled" != "true" && "$old_enabled" != "false" ]]; then
    echo "FIN_TERMINAL_BROWSER_ENABLED must be true or false" >&2
    exit 1
fi
if [[ "$old_enabled" == "true" ]]; then
    old_mcp_container="$(docker compose "${compose_args[@]}" ps -q fin-terminal-browser-mcp)"
    if [[ -z "$old_mcp_container" ]]; then
        echo "enabled canary has no running MCP sidecar to snapshot" >&2
        exit 1
    fi
    old_mcp_image_ref="$(docker inspect --format '{{.Config.Image}}' "$old_mcp_container" 2>/dev/null || true)"
    old_mcp_image_id="$(docker inspect --format '{{.Image}}' "$old_mcp_container" 2>/dev/null || true)"
    if [[ -z "$old_mcp_image_ref" || -z "$old_mcp_image_id" ]]; then
        echo "could not snapshot the running MCP sidecar image" >&2
        exit 1
    fi
fi

set_env_value FIN_TERMINAL_BROWSER_IMAGE "$image"
browser_token="$(get_env_value FIN_TERMINAL_BROWSER_PROXY_TOKEN || true)"
if [[ -z "$browser_token" ]]; then
    if [[ "$old_enabled" == "true" ]]; then
        echo "cannot upgrade an enabled canary without its existing browser proxy token" >&2
        exit 1
    fi
    browser_token="$(openssl rand -hex 32)"
    set_env_value FIN_TERMINAL_BROWSER_PROXY_TOKEN "$browser_token"
fi
pi_token="$(get_env_value FIN_TERMINAL_PROXY_TOKEN || true)"
if [[ "${#browser_token}" -lt 32 || "$browser_token" == "$pi_token" ]]; then
    echo "browser proxy token is missing, too short, or not independent" >&2
    exit 1
fi

# Keep a first activation disabled during preflight and service health checks.
# During an upgrade, the existing route remains live while only the backend is
# replaced; the preflight receives a process-scoped false override.
if [[ "$old_enabled" == "true" ]]; then
    FIN_TERMINAL_BROWSER_ENABLED=false "$remote_dir/deploy/browser_terminal_canary_preflight.sh"
    # Recreate and functionally health-check the MCP sidecar on upgrades too.
    # The browser backend can remain on the old image while this dependency is
    # replaced, but a stale sidecar must not survive an application upgrade.
    docker compose "${compose_args[@]}" up -d --build fin-terminal-browser-mcp
    wait_for_health fin-terminal-browser-mcp
    docker compose "${compose_args[@]}" up -d --no-deps --no-build --pull never \
        --force-recreate fin-terminal-browser
else
    set_env_value FIN_TERMINAL_BROWSER_ENABLED false
    "$remote_dir/deploy/browser_terminal_canary_preflight.sh"
    docker compose "${compose_args[@]}" up -d --build fin-terminal-browser-mcp fin-terminal-browser
    wait_for_health fin-terminal-browser-mcp
fi
wait_for_health fin-terminal-browser

if [[ "$old_enabled" == "true" ]]; then
    # Caddy stays running during the replacement and is gracefully reloaded only
    # after the new backend is healthy, refreshing the service resolution.
    docker compose "${compose_args[@]}" exec -T caddy caddy reload \
        --config /etc/caddy/Caddyfile </dev/null
else
    set_env_value FIN_TERMINAL_BROWSER_ENABLED true
    docker compose "${compose_args[@]}" up -d --no-deps --no-build --pull never \
        --force-recreate caddy
fi

status="$(wait_for_public_status)"
authenticated_status="$(wait_for_authenticated_status)"

completed=true
echo "browser-terminal canary enabled; unauthenticated probe returned HTTP $status; authenticated probe returned HTTP $authenticated_status"
