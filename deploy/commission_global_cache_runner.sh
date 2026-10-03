#!/usr/bin/env bash
# Commission, disable, or inspect the private global research cache runner.
#
# Streamed over verified SSH by the protected GitHub Action
# (.github/workflows/commission-global-cache.yml). Actions:
#   enable   - pin the reviewed image, start the runner, and prove shadow
#              commissioning (feed reachability + scheduler advancement,
#              zero model calls, zero cache writes). Model dispatch is not
#              wired in the runner image, so dispatch cannot occur here.
#   disable  - stop the runner and leave it disabled (idempotent, keeps the
#              data volume for forensics).
#   status   - report container/health/journal aggregates without mutating.
#
# The script prints aggregate counts only: never feed titles, URLs, journal
# contents, tokens, or credentials.

set -euo pipefail
umask 077

if [[ "$#" -ne 4 ]]; then
    echo "usage: $0 ACTION REMOTE_DIR EXPECTED_INFRA_SHA EXPECTED_RUNNER_SHA" >&2
    exit 2
fi

action="$1"
remote_dir="$2"
expected_infra_sha="$3"
expected_runner_sha="$4"
env_file="$remote_dir/.env"
compose_args=(
    --profile fin-terminal-browser-canary
    -f "$remote_dir/docker-compose.yml"
    -f "$remote_dir/docker-compose.browser-terminal.yml"
)

case "$action" in
enable | disable | status) ;;
*)
    echo "ACTION must be enable, disable, or status" >&2
    exit 2
    ;;
esac
[[ "$remote_dir" = /* ]] || { echo "REMOTE_DIR must be absolute" >&2; exit 2; }
[[ "$expected_infra_sha" =~ ^[0-9a-f]{40}$ ]] || {
    echo "EXPECTED_INFRA_SHA must be a 40-character lowercase Git SHA" >&2
    exit 2
}
[[ "$expected_runner_sha" =~ ^[0-9a-f]{40}$ ]] || {
    echo "EXPECTED_RUNNER_SHA must be a 40-character lowercase Git SHA" >&2
    exit 2
}
cd "$remote_dir"

exec 9>>"$remote_dir/.deploy.lock"
if ! flock -n 9; then
    echo "deployment lock is already held" >&2
    exit 75
fi

# Non-secret values only: prints the raw value to stdout for logs, so this
# helper must never be used for tokens, keys, or other credentials. It
# streams the file and stops at the first match so secret material is never
# accumulated in memory.
get_nonsensitive_env_value() {
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
fd, temp_path = tempfile.mkstemp(prefix=".global-cache-env.", dir=directory, text=True)
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
if fields["revision"] != expected:
    fail("deployed release revision does not match the reviewed infra revision")
PY
}

runner_container() {
    docker compose "${compose_args[@]}" ps -q fin-terminal-global-cache 2>/dev/null || true
}

wait_for_health() {
    local container health
    for _ in $(seq 1 40); do
        container="$(runner_container)"
        if [[ "$container" =~ ^[0-9a-f]{12,64}$ ]]; then
            health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}starting{{end}}' "$container")"
            if [[ "$health" == "healthy" ]]; then
                return 0
            fi
        fi
        sleep 5
    done
    echo "timed out waiting for global-cache runner health" >&2
    docker compose "${compose_args[@]}" logs --tail 60 fin-terminal-global-cache >&2 || true
    return 1
}

# Aggregate-only journal/SQLite inspection. Runs inside the runner container
# (node:22 provides node:sqlite) and prints counts, never contents.
write_health_probe() {
    local probe_path="$1"
    cat >"$probe_path" <<'JS'
import fs from "node:fs";
import { DatabaseSync } from "node:sqlite";
const JOURNAL = "/data/global-cache/market-event-scout.json";
const STORE = "/data/global-cache/global-research-cache.sqlite";
const EXPECTED_SOURCES = [
  "nasdaq-trade-halts",
  "nasdaq-corporate-actions",
  "sec-current-filings",
  "federal-reserve-monetary",
  "bea-news",
  "ftc-press-releases",
  "doj-news",
];
const EXPECTED_HOSTS = new Set([
  "www.nasdaqtrader.com",
  "www.sec.gov",
  "www.federalreserve.gov",
  "apps.bea.gov",
  "www.ftc.gov",
  "www.justice.gov",
]);
const SOURCE_HOSTS = {
  "nasdaq-trade-halts": "www.nasdaqtrader.com",
  "nasdaq-corporate-actions": "www.nasdaqtrader.com",
  "sec-current-filings": "www.sec.gov",
  "federal-reserve-monetary": "www.federalreserve.gov",
  "bea-news": "apps.bea.gov",
  "ftc-press-releases": "www.ftc.gov",
  "doj-news": "www.justice.gov",
};
const since = Number(process.argv[2] || "0");
const out = {};
try {
  const state = JSON.parse(fs.readFileSync(JOURNAL, "utf8"));
  const sources = Array.isArray(state.sources) ? state.sources : [];
  out.journalExists = true;
  out.journalVersion = state.version;
  out.sourceCount = sources.length;
  out.expectedSources = EXPECTED_SOURCES.every((id) => sources.some((s) => s && s.sourceId === id));
  out.attempted = sources.filter((s) => s && s.lastAttemptAt > since).length;
  const fresh = sources.filter((s) => s && s.lastStatus === "ok" && s.lastSuccessAt > since);
  out.successCount = fresh.length;
  const hosts = new Set();
  for (const s of fresh) {
    const host = SOURCE_HOSTS[s.sourceId];
    if (host && EXPECTED_HOSTS.has(host)) hosts.add(host);
  }
  out.hostCount = hosts.size;
  out.schedulerAdvanced = state.updatedAt > since && fresh.length > 0;
  const dryRun = state.triggerDryRun;
  out.policyVerified = Boolean(dryRun && dryRun.policy && dryRun.policy.version === 1
    && dryRun.policy.minPriority === 80 && dryRun.policy.dailyCap === 8);
  out.dispatchRecords = Array.isArray(state.triggerDispatches) ? state.triggerDispatches.length : -1;
} catch (error) {
  out.journalExists = false;
  out.journalError = error.code || "read-failed";
}
if (!fs.existsSync(STORE)) {
  out.cacheEntries = 0;
} else {
  try {
    const db = new DatabaseSync(STORE, { readOnly: true });
    try {
      const row = db.prepare("SELECT COUNT(*) AS n FROM global_cache_entries").get();
      out.cacheEntries = row.n;
    } finally {
      db.close();
    }
  } catch (error) {
    out.cacheEntries = -1;
    out.cacheError = error.code || "read-failed";
  }
}
console.log(JSON.stringify(out));
JS
}

commission_probe() {
    local container="$1"
    local since="$2"
    local probe_file probe_stderr_file probe detail
    probe_file="$(mktemp /tmp/global-cache-probe.XXXXXX.mjs)"
    chmod 600 "$probe_file"
    write_health_probe "$probe_file"
    # Remove any stale probe from a previous commission before installing
    # the current one, so a reused container cannot run outdated checks.
    docker exec "$container" rm -f /tmp/gc-health-probe.mjs >/dev/null 2>&1 || true
    docker cp "$probe_file" "$container:/tmp/gc-health-probe.mjs" >/dev/null
    rm -f "$probe_file"
    # On probe failure, preserve a bounded slice of stderr in the failure
    # flag so operators see the crash reason instead of a bare boolean.
    probe_stderr_file="$(mktemp /tmp/global-cache-probe-err.XXXXXX)"
    chmod 600 "$probe_stderr_file"
    if probe="$(docker exec "$container" node /tmp/gc-health-probe.mjs "$since" 2>"$probe_stderr_file")"; then
        :
    else
        detail="$(head -c 500 "$probe_stderr_file" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
        probe="{\"probeFailed\":true,\"detail\":$detail}"
    fi
    rm -f "$probe_stderr_file"
    echo "$probe"
    docker exec "$container" rm -f /tmp/gc-health-probe.mjs >/dev/null 2>&1 || true
}

disable_runner() {
    set_env_value FIN_TERMINAL_GLOBAL_CACHE_ENABLED false
    docker compose "${compose_args[@]}" stop fin-terminal-global-cache >/dev/null 2>&1 || true
    echo "global-cache runner disabled"
}

validate_deployed_release_identity
[[ -f "$env_file" && ! -L "$env_file" ]] || {
    echo "production .env is missing or symlinked" >&2
    exit 1
}

case "$action" in
status)
    container="$(runner_container)"
    if [[ -z "$container" ]]; then
        echo "global-cache runner is not running"
        exit 0
    fi
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$container")"
    echo "container=$container health=$health"
    commission_probe "$container" 0
    ;;
disable)
    disable_runner
    ;;
enable)
    runner_image="$(get_nonsensitive_env_value FIN_TERMINAL_GLOBAL_CACHE_IMAGE || true)"
    [[ "$runner_image" =~ ^[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[0-9a-f]{64}$ ]] || {
        echo "FIN_TERMINAL_GLOBAL_CACHE_IMAGE is not a digest-pinned reference" >&2
        exit 1
    }
    expected_digest="${runner_image##*@}"
    pull_attempt=0
    while [[ "$pull_attempt" -lt 3 ]]; do
        pull_attempt=$((pull_attempt + 1))
        if docker pull "$runner_image" >/dev/null 2>&1; then
            break
        fi
        if [[ "$pull_attempt" -ge 3 ]]; then
            echo "could not pull the pinned runner image after 3 attempts" >&2
            exit 1
        fi
        sleep 10
    done
    # Verify the immutable digest itself, not just the mutable OCI revision
    # label: a registry mishap must not substitute a different image that
    # happens to carry the expected label.
    actual_refs="$(docker image inspect --format '{{json .RepoDigests}}' "$runner_image" 2>/dev/null || true)"
    if [[ "$actual_refs" != *"@${expected_digest}"* ]]; then
        echo "pulled runner image digest does not match the pinned reference" >&2
        exit 1
    fi
    actual_runner_sha="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$runner_image" 2>/dev/null || true)"
    if [[ "$actual_runner_sha" != "$expected_runner_sha" ]]; then
        echo "runner image revision does not match the reviewed app revision (expected $expected_runner_sha, actual ${actual_runner_sha:-missing})" >&2
        exit 1
    fi
    set_env_value FIN_TERMINAL_GLOBAL_CACHE_IMAGE "$runner_image"
    set_env_value FIN_TERMINAL_GLOBAL_CACHE_ENABLED true
    docker compose "${compose_args[@]}" up -d --no-deps --no-build --pull never fin-terminal-global-cache
    wait_for_health
    container="$(runner_container)"
    commission_start="$(date +%s%3N)"
    echo "runner healthy; proving shadow commissioning (feed reachability + scheduler advancement, zero dispatch)..."
    for _ in $(seq 1 20); do
        sleep 30
        probe="$(commission_probe "$container" "$commission_start")"
        if COMMISSION_JSON="$probe" python3 - <<'PY'; then
import json
import os
import sys

probe = json.loads(os.environ["COMMISSION_JSON"])


def fail(message):
    print(f"commission pending: {message}")
    raise SystemExit(1)


if probe.get("probeFailed"):
    fail(f"probe execution failed: {probe.get('detail', '')}")
if not probe.get("journalExists"):
    fail("journal not present yet")
if probe.get("sourceCount") != 7 or not probe.get("expectedSources"):
    fail("source contract mismatch")
if probe.get("attempted", 0) < 7:
    fail("not all sources attempted yet")
if probe.get("successCount", 0) < 4:
    fail("fewer than 4 fresh successes")
if probe.get("hostCount", 0) < 3:
    fail("fewer than 3 distinct hosts")
if not probe.get("schedulerAdvanced"):
    fail("scheduler has not advanced yet")
if not probe.get("policyVerified"):
    fail("trigger dry-run policy mismatch")
if probe.get("dispatchRecords", -1) != 0:
    fail("unexpected dispatch records (dispatch must stay off)")
if probe.get("cacheEntries", -1) != 0:
    fail("unexpected cache entries (no publication in shadow mode)")
print("commission checks passed")
PY
            echo "COMMISSION OK"
            echo "  sourceCount=7 successCount>=4 hostCount>=3 schedulerAdvanced=true dispatchRecords=0 cacheEntries=0"
            echo "$probe"
            exit 0
        fi
    done
    echo "timed out waiting for shadow commissioning" >&2
    exit 1
    ;;
esac
