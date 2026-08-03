# Public Live Terminal Pilot

This optional overlay runs one real, disposable Pi-backed terminal for public
visitors. The normal deployment stages and validates the overlay but never
starts its profiled services. `FIN_TERMINAL_PUBLIC_ENABLED` defaults to
`false`, so Caddy returns the existing catch-all 404 instead of proxying a dead
pilot upstream. The signed-in `/fin-terminal/` application and static replay at
`/fin-terminal-demo/` are unchanged.

## Architecture

`browser → Caddy → Turnstile → public gateway → one disposable worker seat`

- Caddy strips any client-supplied edge token and real-IP header, injects an
  independent edge token, and omits the capability-bearing route from access
  logs. The gateway trusts the forwarded visitor IP only with that token.
- The gateway owns signed opaque visitor/ticket tokens, a FIFO queue, a daily
  conservative admission reservation, and WebSocket proxying. Browser cookies
  and ordinary authorization headers do not cross the public boundary.
- Redis persists admission state and permits only one active gateway lease.
- The one seat has its own container, Pi session, temporary storage, and one
  concurrent research worker. Idle, absolute-duration, and disconnect-grace
  expiry stop the process; Compose restarts a clean generation before reuse.
- Public research uses a dedicated MCP process but intentionally receives the
  same `OPENROUTER_API_KEY` as the trial agent. Provider quota, billing, and
  outage blast radius are shared.
- No gateway, Redis, MCP, or worker port is published on the host. Browser
  traffic can address only Caddy, never a worker container.

The gateway and worker are pinned to application commit
`e287a54e12b29c33e4ee9e751946fb98ec3fba8e`. Redis is also pinned by its
multi-platform image digest. Changes to either pin require a reviewed
infrastructure PR.

## Activation gates

Do not build or start the profile until all of these gates pass:

1. Application PRs
   [`protostatis/unbrowser-fin-terminal#13`](https://github.com/protostatis/unbrowser-fin-terminal/pull/13)
   and
   [`protostatis/unbrowser-fin-terminal#14`](https://github.com/protostatis/unbrowser-fin-terminal/pull/14)
   are merged, and commit `e287a54e12b29c33e4ee9e751946fb98ec3fba8e`
   remains reachable from a protected branch or release tag. Any application
   release change requires a reviewed immutable-pin update.
2. The operator accepts that anonymous pilot research and the trial agent share
   the existing OpenRouter credential, quota, billing, and provider-level
   limits. The application reservation guard is not provider-side metering.
3. Production Turnstile site and secret values are provisioned for
   `unbrowser.unchainedsky.com` and action `public_terminal_admission`.
4. A real Docker rehearsal verifies startup, health, timeout exit, restart
   generation, reconnect fencing, and rollback behavior. Static Compose and
   Caddy validation are not substitutes for this gate.

The infrastructure PR may merge before these gates because the normal deploy
keeps the route disabled and does not start the profile.

### Docker rehearsal evidence

The current immutable pins passed an isolated arm64 Docker Desktop rehearsal
on 2026-08-03. Turnstile was bypassed only through a test-only Compose override;
the production Turnstile gate remains separate. The rehearsal verified:

- all four profiled services reached healthy with no host-published ports;
- MCP SDK `1.29.0` initialized, listed 32 tools, and completed a harmless
  `navigate` call through the dedicated SSRF egress proxy;
- a browser reconnected inside grace without changing worker generation;
- disconnect expiry stopped the worker, Compose restarted it once with a new
  generation, and the stale ticket was rejected with HTTP 409;
- the replacement returned to the one-seat ready pool;
- the named-service rollback removed every profiled container, retained the
  Redis volume for diagnosis, and left the production pilot route at 404.

Repeat this rehearsal after changing an application, Redis, MCP dependency, or
lifecycle configuration pin. It does not authorize activation without the
other gates above.

## Pilot policy

- Exactly one worker seat. Multi-seat activation is not defined by this PR.
- 50 waiting tickets, with a 10-minute ticket lifetime.
- 5-minute idle timeout, 15-minute absolute session maximum, and 30-second
  reconnect grace.
- Up to five research launches per guest session; one runs at a time.
- USD 10 daily application admission-reservation budget, using conservative
  USD 0.20 reservations for each possible run when a seat is assigned.

The reservation guard does not meter provider invoices and is not a hard spend
ceiling: one run may cost more than its assumed reservation. Any provider-level
limit applies jointly to the pilot and trial agent, so pilot traffic can consume
trial-agent quota.

## Protected configuration

The deployment-host secret helper creates independent 256-bit values for these
trust boundaries while the pilot is disabled:

- `FIN_TERMINAL_PUBLIC_SESSION_SIGNING_KEY`
- `FIN_TERMINAL_PUBLIC_WORKER_PROXY_TOKEN`
- `FIN_TERMINAL_PUBLIC_EDGE_PROXY_TOKEN`

It refuses to generate or rotate those values while
`FIN_TERMINAL_PUBLIC_ENABLED=true`. Before starting the pilot, provision these
external values in the GitHub `production` Environment:

- variable `FIN_TERMINAL_PUBLIC_TURNSTILE_SITE_KEY` (public browser site key)
- secret `FIN_TERMINAL_PUBLIC_TURNSTILE_SECRET` (server-only verification key)

The approved production job requires both values and streams them over the
verified SSH connection directly into the protected staging directory. They are
never command arguments, log output, repository files, or local temporary
files. The staging helper validates their format and atomically upserts them
into the candidate `.env`; a validation failure leaves the live `.env`
untouched. Changing either value while the public route is enabled is rejected,
so disable the route before rotating the Turnstile pair.

The worker receives the existing protected `OPENROUTER_API_KEY`, matching the
trial agent. Setting `FIN_TERMINAL_PUBLIC_ENABLED=true` makes the secret helper
require both Turnstile values. Do not put any protected value in this repository
or a command-line argument.

## One-seat startup and enablement

Run Compose from the deployed release directory. Always name the public
services explicitly: invoking `up` without service names would also act on all
unprofiled services from the base production file.

Keep `FIN_TERMINAL_PUBLIC_ENABLED=false` while starting the upstreams:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.public-terminal.yml \
  --profile fin-terminal-public-pilot \
  pull fin-terminal-public-redis

docker compose \
  -f docker-compose.yml \
  -f docker-compose.public-terminal.yml \
  --profile fin-terminal-public-pilot \
  build \
  fin-terminal-public-unbrowser-mcp \
  fin-terminal-public-seat-01 \
  fin-terminal-public-gateway

docker compose \
  -f docker-compose.yml \
  -f docker-compose.public-terminal.yml \
  --profile fin-terminal-public-pilot \
  up -d --no-build \
  fin-terminal-public-redis \
  fin-terminal-public-unbrowser-mcp \
  fin-terminal-public-seat-01 \
  fin-terminal-public-gateway
```

Verify the four public services are healthy and complete the Docker rehearsal
before changing the edge flag. Then set `FIN_TERMINAL_PUBLIC_ENABLED=true`
through the protected environment. Run the host-side credential helper and
proceed only when it reports that existing credentials were retained; if it
generates a default-terminal credential, stop and use the normal deployment
flow so every affected trust-boundary container is recreated:

```bash
python3 .deploy-tools/ensure_fin_terminal_secrets.py .env
```

That check enforces the external Turnstile gate once the flag is true. Next,
validate the exact rendered Caddy candidate using the already pinned image:

```bash
docker compose -f docker-compose.yml run --rm --no-deps --pull never \
  --entrypoint caddy caddy \
  validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

Only after validation succeeds, force-recreate Caddy without dependencies so
its process receives the new boolean value:

```bash
docker compose -f docker-compose.yml up -d \
  --no-deps --no-build --pull never --force-recreate caddy
```

Verify `/fin-terminal-live-pilot/api/ready`, invalid-origin rejection,
Turnstile action and hostname binding, queue expiry, WebSocket reconnect,
timeout cleanup, and worker replacement generation. Record RSS, CPU, actual
provider charges, and dedicated MCP health throughout the one-seat soak.

Keep the pilot at `/fin-terminal-live-pilot/`. Promoting it over the replay path
requires a separate operational review and infrastructure PR.

## Expansion boundary

This overlay deliberately defines no second seat. Multi-seat rollout requires
per-seat network and authentication isolation plus a separate capacity and cost
review; it must not be enabled by copying the seat-01 service definition.

## Fail-closed rollback

First set `FIN_TERMINAL_PUBLIC_ENABLED=false` through the protected environment,
then validate and recreate Caddy with the exact two commands from the enablement
section. Confirm the disabled route is live before stopping an upstream:

```bash
test "$(curl --silent --show-error --output /dev/null \
  --write-out '%{http_code}' \
  https://unbrowser.unchainedsky.com/fin-terminal-live-pilot/)" = 404
```

Stop and remove only the named pilot services:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.public-terminal.yml \
  --profile fin-terminal-public-pilot \
  stop \
  fin-terminal-public-gateway \
  fin-terminal-public-seat-01 \
  fin-terminal-public-unbrowser-mcp \
  fin-terminal-public-redis

docker compose \
  -f docker-compose.yml \
  -f docker-compose.public-terminal.yml \
  --profile fin-terminal-public-pilot \
  rm -f \
  fin-terminal-public-gateway \
  fin-terminal-public-seat-01 \
  fin-terminal-public-unbrowser-mcp \
  fin-terminal-public-redis
```

Retain the Redis volume for diagnosis unless data removal has been separately
approved. Never run `docker compose down` with this overlay: the merged project
also contains the default production services.

The regular deploy script does not start, rebuild, stop, or health-check the
profiled pilot. Its automatic rollback includes a renderable overlay when
removing orphans; if external pilot configuration is incomplete, it preserves
orphans rather than deleting independently managed public containers.
