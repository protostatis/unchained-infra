# Terminal Runtime Reconciler

The host-side singleton reconciler manages the public-terminal warm pool. It
starts and stops only the six allowlisted worker seat containers (01–06);
Redis, the shared MCP service, and the gateway are always managed by the
existing pilot activation workflow.

## Architecture

```
[reconciler systemd service]
        |
        |  docker exec -i <gateway> node -e ... (private API, X-Management-Token)
        |  payload over stdin — never a host temp path inside the container
        v
[fin-terminal-public-gateway]  (private listener 127.0.0.1:8789, never published)
        |
        |  /api/management/reconcile-snapshot  → versioned seat map + totals + plan
        |  /api/management/reconcile-plan      → desired warm-pool plan
        |  /api/management/drain               → atomic drain (generation CAS)
        |  /api/management/activate             → mark seat desired ({accepted})
        |
        v
[docker compose start/stop of allowlisted seat-01..06 containers]
```

The reconciler never mounts the Docker socket into application containers. All
gateway interaction is via `docker exec` inside the gateway container with a
private management token. The reconciler only controls the six seat service
names — any unrecognized name is rejected before a subprocess is invoked.

## Default-off

Feature flag `TERMINAL_RUNTIME_FEATURE_ENABLED` defaults to `false`. When
disabled, the reconciler exits immediately. The existing six-seat pilot
is not publicly activatable: the protected workflow enables the runtime gate,
proves the global max-two permit policy, and starts this reconciler before it
exposes the edge.

## Warm pool contract

- Six logical Compose seats/networks remain static. The reconciler never
  creates or removes seat definitions.
- **Target running = min(6, assigned + queued + 1)**: one warm spare is always
  available when feasible.
- **5-minute idle scale-down**: the reconciler enforces the configured
  `TERMINAL_RUNTIME_IDLE_SCALE_DOWN` seconds (default 300) itself — an
  unassigned seat below the threshold is never a candidate — and it sends the
  same value to the gateway via `reconcile-plan` (`idleScaleDownSeconds`) so
  both sides share one source of truth. The gateway also enforces the exact
  threshold on `/drain`.
- **Drain-then-stop**: the gateway must accept an atomic drain before the
  container is stopped. A rejected drain leaves the seat running.
- **Sticky-drain scale-up**: when demand returns, drained seats whose
  containers were stopped are restart candidates. The reconciler starts the
  container while the drain stays **sticky** — it never calls `activate` on a
  same-generation drain (the gateway rejects it with `409 drain sticky;
  generation unchanged`). Once the restarted container registers a NEW healthy
  generation (the gateway lists the seat in `plan.activateCandidates`), the
  reconciler calls `activate` so the generation CAS releases the drain. A seat
  is never activated before its container is healthy, and a same-generation
  drain is never released.
- **Assigned seats never stopped**: seats with active or reconnecting sessions
  are excluded from scale-down.
- **Resource guard**: starts are blocked when host memory/disk headroom is
  below configured thresholds or a recent OOM kill is detected.

## Crash recovery

- **STARTING seats**: if the reconciler crashes while a seat is starting, the
  next reconcile cycle detects the transitory state. If the container is
  healthy locally, it reactivates the seat in the gateway. If absent, it
  cleans up.
- **DRAINING seats**: recovery is container-state aware. A draining seat with
  no local container is a completed scale-down stop (nothing to do). A
  draining seat with a running/healthy container is a scale-up restart in
  progress — it is LEFT RUNNING so the new generation can register, and the
  activate path releases the drain once `plan.activateCandidates` lists it.
  An exited/dead container is cleaned up.
- **STOPPED seats**: any lingering containers are removed.

## Deployment lock

The reconciler respects the existing `.deploy.lock` shared with `deploy.sh`
and the pilot workflow (`public_terminal_pilot_remote.sh` activate/disable/
rollback actions hold it via `flock` while they mutate deployment state).
The lock is held **only during each observed→mutate cycle** — never for the
process lifetime — so those actions (including rollback, which stops the
reconciler systemd unit and re-enables the static six-seat pilot) can never
deadlock behind a lifetime lock. If the lock is held by a deploy at the start
of a cycle, the reconciler runs that cycle passive (reads snapshot, logs, does
not start/stop) and retries on the next tick.

## Rollback

Use the protected pilot `disable` action. It closes and verifies the edge first,
disables/stops the systemd service, removes the named pilot containers, resets
the runtime feature and token, and removes only metadata/hash-matched managed
configuration. A public feature-disabled static-six fallback is forbidden: it
would bypass the global max-two research gate.

## Pilot workflow integration

`deploy/public_terminal_pilot_remote.sh`
(`activate-runtime`/`verify-runtime`/`disable`/`status`)
is dynamic-mode aware:

- **Dynamic mode** (`TERMINAL_RUNTIME_FEATURE_ENABLED=true`): the activate
  gates allow stopped seats — the reconciler owns seat lifecycle. Runtime
  verification accepts the exact nine-service set with seats absent, and the
  gateway readiness check requires only the one-warm-spare pool (never six
  stopped seats).
- **Feature-disabled mode**: the public edge remains 404. It is not an
  activation fallback.
- **Activation**: verifies exact deployed artifact hashes, generates the shared
  management token on-host, starts all six seats behind the closed edge, proves
  max-two FIFO permits, starts the reconciler under the deployment lock, then
  promotes Caddy. A post-unlock workflow check requires a real reconcile cycle.
- **rollback compatibility action**: aliases fail-closed `disable`; it never
  exposes static-six mode.
- **Companion Redis keys** (workspace DB 1) are backed up before any state
  mutation, restored on rollback, and cleaned on disable.
- **Runtime Redis keys** (`capacity` and `research-permits` in DB 0) are backed
  up and cleared before a fresh activation, restored on activation rollback,
  and cleared on disable so stale drains cannot fence the next warm pool.
- **SQLite online backup** is performed before any additive schema migration
  (activate gate) via the Python `sqlite3` online backup API; the snapshot is
  stored in the secure workdir.
- **status** reports `TERMINAL_RUNTIME_FEATURE_ENABLED` and the reconciler
  systemd state alongside the service states.

## Gateway management API contract (v1)

The gateway must expose a private management listener on port **8789**
(`TERMINAL_RUNTIME_MANAGEMENT_PORT`) with:

| Endpoint | Method | Input | Output |
|---|---|---|---|
| `/api/management/reconcile-snapshot` | POST | `{}` | `{version: 1, seats: {workerId: {workerId, status, phase, generation\|null, assigned, idleSeconds, drainRequested, drainId\|null, containerId:""}}, totalAssigned, totalQueued, plan}` |
| `/api/management/reconcile-plan` | POST | `{desiredSeats, idleScaleDownSeconds}` | `{version: 1, reconciled: true, plan}` |
| `/api/management/drain` | POST | `{workerId, drainId, expectedGeneration}` | `{accepted: true, drainId}` \| 409 `{accepted: false, reason}` |
| `/api/management/activate` | POST | `{workerId}` | `{accepted: true}` \| 409 `{accepted: false, reason}` |

- `status` is one of `absent | starting | healthy | draining | stopped`.
- `seats` is keyed by the gateway's **worker id** (`seat-01`..`seat-06`); the
  reconciler maps these to the allowlisted Compose service names
  (`fin-terminal-public-seat-01`..`-06`) via an exact, tested bijection.
- `totalAssigned` / `totalQueued` and the plan are reported by the gateway;
  `plan.desiredRunning` is **authoritative** for the reconcile decision (the
  totals are informational for logging).
- `drain` requires `expectedGeneration` matching the seat's current generation
  (a CAS). A stale generation is rejected with 409 so a replaced worker is
  never drained.
- `activate` releases a sticky drain only when the process generation changed
  (the reconciler restarted the container); a same-generation activate is
  rejected with 409, and a non-draining seat is an accepted no-op.
- Authentication: `X-Management-Token` header must match
  `TERMINAL_RUNTIME_MANAGEMENT_TOKEN`. The reconciler calls it via
  `docker exec -i <gateway> node -e <script>` with the payload on **stdin** —
  no host temp file is assumed inside the container, and the token/path are
  JSON-escaped so neither can break out of the JS string literal.

The exact cross-repo wire contract (units, headers, cookie names, env vars,
paths) is the canonical document in
[`docs/financial-terminal-cross-repo-contract.md`](financial-terminal-cross-repo-contract.md).

## Configuration

The protected activation action atomically creates
`/home/ec2-user/unchained/.env.reconciler` with this exact shape (never place a
real token in source control):

```bash
TERMINAL_RUNTIME_FEATURE_ENABLED=true
TERMINAL_RUNTIME_MANAGEMENT_TOKEN=<256-bit random hex>
TERMINAL_RUNTIME_MANAGEMENT_PORT=8789
TERMINAL_RUNTIME_COMPOSE_PROJECT=unchained
TERMINAL_RUNTIME_COMPOSE_DIR=/home/ec2-user/unchained
TERMINAL_RUNTIME_RECONCILE_INTERVAL=15
TERMINAL_RUNTIME_IDLE_SCALE_DOWN=300
TERMINAL_RUNTIME_MAX_START_CONCUR=2
TERMINAL_RUNTIME_HOST_MEM_RESERVE_MB=512
TERMINAL_RUNTIME_HOST_MEM_HEADROOM_PCT=15
TERMINAL_RUNTIME_HOST_DISK_MAX_PCT=85
```

## Installation

Production installation is owned by the protected `activate-runtime` workflow,
which verifies the deployed file hashes and systemd unit before installation.
The following commands are diagnostic/development reference only:

```bash
sudo cp deploy/terminal-runtime-reconciler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable terminal-runtime-reconciler
sudo systemctl start terminal-runtime-reconciler
```

## Operator status check

```bash
sudo systemctl status terminal-runtime-reconciler
sudo journalctl -u terminal-runtime-reconciler -n 50 --no-pager
```

## Pre-migration SQLite backup runbook

Before any schema migration to the relay/analytics databases, take an online
backup:

```bash
docker compose exec relay sqlite3 /data/auth.db ".backup '/data/auth.db.backup'"
docker compose exec relay sqlite3 /data/analytics.db ".backup '/data/analytics.db.backup'"
```

Store backups outside the container before proceeding with migration.

## Redis companion backup/restore

The public-terminal Redis state key `fin-terminal-public:v1:state` can be
backed up without altering the current v1 state:

```bash
# Backup
docker compose exec fin-terminal-public-redis \
  redis-cli --raw GET 'fin-terminal-public:v1:state' > /tmp/redis-state-backup.json

# Restore (stop gateway first)
docker compose stop fin-terminal-public-gateway
docker compose exec -i fin-terminal-public-redis \
  redis-cli -x SET 'fin-terminal-public:v1:state' < /tmp/redis-state-backup.json
docker compose start fin-terminal-public-gateway
```

The existing pilot activation workflow already handles Redis state backup and
restore atomically during activate/disable transitions.
