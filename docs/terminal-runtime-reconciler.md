# Terminal Runtime Reconciler

The host-side singleton reconciler manages the public-terminal warm pool. It
starts and stops only the six allowlisted worker seat containers (01–06);
Redis, the shared MCP service, and the gateway are always managed by the
existing pilot activation workflow.

## Architecture

```
[reconciler systemd service]
        |
        |  docker exec (private management API, X-Management-Token header)
        v
[fin-terminal-public-gateway:8788]
        |
        |  /api/management/reconcile-snapshot  → current seat states
        |  /api/management/reconcile-plan      → desired seat set
        |  /api/management/drain               → atomic drain (generation check)
        |  /api/management/activate             → mark seat desired
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
activation workflow requires all nine containers running and is unchanged.

## Warm pool contract

- Six logical Compose seats/networks remain static. The reconciler never
  creates or removes seat definitions.
- **Target running = min(6, assigned + queued + 1)**: one warm spare is always
  available when feasible.
- **5-minute idle scale-down**: unassigned seats idle longer than
  `TERMINAL_RUNTIME_IDLE_SCALE_DOWN` seconds (default 300) are candidates for
  drain.
- **Drain-then-stop**: the gateway must accept an atomic drain before the
  container is stopped. A rejected drain leaves the seat running.
- **Assigned seats never stopped**: seats with active or reconnecting sessions
  are excluded from scale-down.
- **Resource guard**: starts are blocked when host memory/disk headroom is
  below configured thresholds or a recent OOM kill is detected.

## Crash recovery

- **STARTING seats**: if the reconciler crashes while a seat is starting, the
  next reconcile cycle detects the transitory state. If the container is
  healthy locally, it reactivates the seat in the gateway. If absent, it
  cleans up.
- **DRAINING seats**: if drain was accepted but the container wasn't stopped,
  the next reconcile completes the stop.
- **STOPPED seats**: any lingering containers are removed.

## Deployment lock

The reconciler respects the existing `.deploy.lock` shared with `deploy.sh`.
Only the lock holder performs mutations. If the lock is held by a deploy, the
reconciler runs passive (reads snapshot, logs, does not start/stop).

## Rollback

To disable the reconciler and return to static six-seat mode:

1. Stop the systemd service: `sudo systemctl stop terminal-runtime-reconciler`
2. Set `TERMINAL_RUNTIME_FEATURE_ENABLED=false` in `.env.reconciler` (or
   remove the file).
3. Run `rollback_start_all()` from the reconciler to start all six seats, or
   re-run the pilot activation workflow.

The rollback path has been tested: `rollback_start_all` starts only absent
seats, skips already-healthy ones, and never touches Redis/MCP/gateway.

## Gateway management API contract

The gateway must expose a private management listener on port 8788 with:

| Endpoint | Method | Input | Output |
|---|---|---|---|
| `/api/management/reconcile-snapshot` | POST | `{}` | `{seats: {name: {containerId, status, generation, assigned, idleSeconds}}, totalAssigned, totalQueued}` |
| `/api/management/reconcile-plan` | POST | `{desiredSeats: [string]}` | `{plan: [...], accepted: bool}` |
| `/api/management/drain` | POST | `{seatName: string, expectedGeneration: string}` | `{accepted: bool}` |
| `/api/management/activate` | POST | `{seatName: string}` | `{accepted: bool}` |

Authentication: `X-Management-Token` header must match
`TERMINAL_RUNTIME_MANAGEMENT_TOKEN`.

## Configuration

Create `/home/ec2-user/unchained/.env.reconciler`:

```bash
TERMINAL_RUNTIME_FEATURE_ENABLED=true
TERMINAL_RUNTIME_MANAGEMENT_TOKEN=<256-bit random hex>
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
