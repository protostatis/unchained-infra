#!/usr/bin/env bash
# Validate and promote a staged Caddy configuration without exposing an
# unvalidated file through the live single-file bind mount.

set -euo pipefail
umask 077

die() {
    echo "ERROR: $*" >&2
    exit 1
}

validate_stage_path() {
    local stage_dir="$1" remote_dir="$2" deploy_id="$3"
    [[ "$deploy_id" =~ ^[0-9a-f]{24}$ ]] || die "invalid deployment ID"
    [[ "$stage_dir" == "$remote_dir/.deploy-staging/$deploy_id" ]] \
        || die "refusing unexpected Caddy staging path"
    [[ -d "$stage_dir" && ! -L "$stage_dir" ]] \
        || die "Caddy staging directory is missing or symlinked"
}

validate_staged_config() {
    local stage_dir="$1" remote_dir="$2" deploy_id="$3"
    validate_stage_path "$stage_dir" "$remote_dir" "$deploy_id"

    local compose_file="$stage_dir/docker-compose.yml"
    local public_terminal_compose_file="$stage_dir/docker-compose.public-terminal.yml"
    local candidate_file="$stage_dir/Caddyfile"
    local env_file="$stage_dir/.env"
    [[ -f "$compose_file" && ! -L "$compose_file" ]] \
        || die "staged docker-compose.yml is missing or symlinked"
    [[ -f "$public_terminal_compose_file" && ! -L "$public_terminal_compose_file" ]] \
        || die "staged docker-compose.public-terminal.yml is missing or symlinked"
    [[ -f "$candidate_file" && ! -L "$candidate_file" ]] \
        || die "staged Caddyfile is missing or symlinked"
    [[ -f "$env_file" && ! -L "$env_file" ]] \
        || die "staged production .env is missing or symlinked"

    local work_dir="$stage_dir/.caddy-preflight"
    local container_name="unchained-caddy-preflight-$deploy_id"
    [[ ! -e "$work_dir" ]] || die "Caddy preflight work directory already exists"
    mkdir -m 700 "$work_dir"
    CADDY_PREFLIGHT_WORK_DIR="$work_dir"
    CADDY_PREFLIGHT_CONTAINER_NAME="$container_name"

    cleanup_validation() {
        local status=$?
        trap - EXIT
        docker rm -f "${CADDY_PREFLIGHT_CONTAINER_NAME:-}" >/dev/null 2>&1 || true
        rm -rf "${CADDY_PREFLIGHT_WORK_DIR:-}"
        exit "$status"
    }
    trap cleanup_validation EXIT

    # Validate the optional overlay's merged structure without requiring its
    # external Turnstile credentials. deploy.sh stages but never activates it.
    docker compose --project-directory "$stage_dir" \
        -f "$compose_file" -f "$public_terminal_compose_file" \
        config --no-interpolate --quiet >/dev/null </dev/null

    docker compose --project-directory "$stage_dir" --env-file "$env_file" \
        -f "$compose_file" config --format json > "$work_dir/compose.json" </dev/null

    python3 - "$work_dir/compose.json" "$work_dir/caddy.env" \
        > "$work_dir/metadata" <<'PY'
import json
import os
import re
import sys

config_path, environment_path = map(os.fspath, sys.argv[1:])
config = json.loads(open(config_path, encoding="utf-8").read())
try:
    caddy = config["services"]["caddy"]
except (KeyError, TypeError) as exc:
    raise SystemExit(f"Caddy service is missing from rendered Compose config: {exc}")

image = caddy.get("image")
if not isinstance(image, str) or not image:
    raise SystemExit("Caddy service must specify a rendered image")

platform = caddy.get("platform")
if platform is not None and (not isinstance(platform, str) or not platform):
    raise SystemExit("Caddy platform must be a non-empty string when specified")

environment = caddy.get("environment", {})
if not isinstance(environment, dict):
    raise SystemExit("rendered Caddy environment must be a mapping")

name_pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Caddy boolean feature flags. The Caddyfile references them with
# `{$VAR:false}` placeholders — an EMPTY value substitutes to the literal
# empty string and produces an invalid `expression ''` config, so a
# set-but-empty value must never reach Caddy. Non-boolean values are rejected
# with a clear error before reload (Caddy CEL matchers require true/false).
caddy_boolean_flags = {
    "FIN_TERMINAL_PUBLIC_ENABLED",
    "FIN_TERMINAL_WORKSPACE_ENABLED",
}
with open(environment_path, "x", encoding="utf-8", newline="\n") as handle:
    for name, value in sorted(environment.items()):
        if not isinstance(name, str) or not name_pattern.fullmatch(name):
            raise SystemExit(f"invalid Caddy environment name: {name!r}")
        if value is None:
            raise SystemExit(f"Caddy environment variable {name} is unresolved")
        value = str(value)
        if "\n" in value or "\r" in value:
            raise SystemExit(f"Caddy environment variable {name} contains a newline")
        if name in caddy_boolean_flags:
            normalized = value.strip().lower()
            if normalized == "":
                # Compose's `:-false` interpolation normally normalizes this;
                # reject any path that still delivers an empty value so the
                # staged Caddyfile can never validate against it.
                raise SystemExit(
                    f"Caddy boolean flag {name} must be 'true' or 'false', got empty value"
                )
            if normalized not in ("true", "false"):
                raise SystemExit(
                    f"Caddy boolean flag {name} must be 'true' or 'false', got {value!r}"
                )
            value = normalized
        handle.write(f"{name}={value}\n")
os.chmod(environment_path, 0o600)

print(image)
print(platform or "")
PY

    local -a metadata
    mapfile -t metadata < "$work_dir/metadata"
    local image="${metadata[0]:-}"
    local platform="${metadata[1]:-}"
    [[ -n "$image" ]] || die "could not determine the prospective Caddy image"

    local -a pull_command=(docker image pull)
    if [[ -n "$platform" ]]; then
        pull_command+=(--platform "$platform")
    fi
    pull_command+=("$image")
    "${pull_command[@]}" </dev/null

    local -a run_command=(
        docker run --rm --name "$container_name" --network none --read-only
        --tmpfs /data:rw,nosuid,nodev,noexec,size=16m
        --tmpfs /config:rw,nosuid,nodev,noexec,size=16m
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m
        --mount "type=bind,src=$candidate_file,dst=/etc/caddy/Caddyfile,readonly"
        --env-file "$work_dir/caddy.env"
        --entrypoint caddy
    )
    if [[ -n "$platform" ]]; then
        run_command+=(--platform "$platform")
    fi
    run_command+=("$image" validate --config /etc/caddy/Caddyfile --adapter caddyfile)
    "${run_command[@]}" </dev/null
}

copy_atomically() {
    local source="$1" destination="$2" mode="$3"
    local destination_dir base temporary
    destination_dir="$(dirname "$destination")"
    base="$(basename "$destination")"
    temporary="$(mktemp "$destination_dir/.${base}.preflight.XXXXXX")"
    if ! cat "$source" > "$temporary"; then
        rm -f "$temporary"
        return 1
    fi
    chmod "$mode" "$temporary"
    if ! mv -f "$temporary" "$destination"; then
        rm -f "$temporary"
        return 1
    fi
}

promote_caddyfile_in_place() {
    local candidate_file="$1" live_file="$2"
    python3 - "$candidate_file" "$live_file" <<'PY'
import os
import stat
import sys

candidate_path, live_path = sys.argv[1:]
no_follow = getattr(os, "O_NOFOLLOW", 0)

candidate_fd = os.open(candidate_path, os.O_RDONLY | no_follow)
try:
    if not stat.S_ISREG(os.fstat(candidate_fd).st_mode):
        raise SystemExit("staged Caddyfile is not a regular file")
    chunks = []
    while True:
        chunk = os.read(candidate_fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    candidate = b"".join(chunks)
finally:
    os.close(candidate_fd)

live_before = os.lstat(live_path)
if not stat.S_ISREG(live_before.st_mode):
    raise SystemExit("live Caddyfile is not a regular file")

live_fd = os.open(live_path, os.O_WRONLY | no_follow)
try:
    opened = os.fstat(live_fd)
    if opened.st_dev != live_before.st_dev or opened.st_ino != live_before.st_ino:
        raise SystemExit("live Caddyfile changed while promoting")
    os.ftruncate(live_fd, 0)
    offset = 0
    while offset < len(candidate):
        offset += os.write(live_fd, candidate[offset:])
    os.fsync(live_fd)
finally:
    os.close(live_fd)

live_after = os.stat(live_path)
if live_after.st_dev != live_before.st_dev or live_after.st_ino != live_before.st_ino:
    raise SystemExit("Caddyfile promotion replaced the live bind-mount inode")
with open(live_path, "rb") as handle:
    if handle.read() != candidate:
        raise SystemExit("Caddyfile promotion did not preserve candidate bytes")
PY
}

promote_staged_config() {
    local stage_dir="$1" remote_dir="$2" deploy_id="$3"
    validate_stage_path "$stage_dir" "$remote_dir" "$deploy_id"

    local file
    for file in Dockerfile Dockerfile.unbrowser-mcp docker-compose.yml \
        docker-compose.public-terminal.yml Caddyfile .env; do
        [[ -f "$stage_dir/$file" && ! -L "$stage_dir/$file" ]] \
            || die "staged $file is missing or symlinked"
    done
    [[ -f "$remote_dir/Caddyfile" && ! -L "$remote_dir/Caddyfile" ]] \
        || die "live Caddyfile is missing or symlinked"
    [[ -f "$remote_dir/.env" && ! -L "$remote_dir/.env" ]] \
        || die "live .env is missing or symlinked"

    for file in Dockerfile Dockerfile.unbrowser-mcp docker-compose.yml \
        docker-compose.public-terminal.yml; do
        copy_atomically "$stage_dir/$file" "$remote_dir/$file" 0644
    done

    local environment_changed=false
    if ! cmp -s "$stage_dir/.env" "$remote_dir/.env"; then
        copy_atomically "$stage_dir/.env" "$remote_dir/.env" 0600
        environment_changed=true
    else
        # The staged secret helper enforces this even when it retains the
        # existing token, so preserve that hardening on the live file too.
        chmod 600 "$remote_dir/.env"
    fi

    # The running Caddy container bind-mounts this one file. Replacing it would
    # leave that container attached to the old inode, so write validated bytes
    # in place and verify that the inode did not change.
    promote_caddyfile_in_place "$stage_dir/Caddyfile" "$remote_dir/Caddyfile"
    printf 'environment_changed=%s\n' "$environment_changed"
}

if [[ "$#" != "4" ]]; then
    die "usage: $0 validate|promote STAGE_DIR REMOTE_DIR DEPLOY_ID"
fi

case "$1" in
    validate)
        validate_staged_config "$2" "$3" "$4"
        ;;
    promote)
        promote_staged_config "$2" "$3" "$4"
        ;;
    *)
        die "unknown action: $1"
        ;;
esac
