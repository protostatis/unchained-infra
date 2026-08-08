# Financial terminal route

The singleton financial terminal is served at:

- `https://unbrowser.unchainedsky.com/fin-terminal/`

The Compose build is pinned to the full commit SHA from
`protostatis/unbrowser-fin-terminal`. `PUBLIC_BASE_PATH` is fixed at build time,
and Caddy strips `/fin-terminal` before proxying to port `8787`. The former
`https://unchainedsky.com/unbrowser/fin-terminal/` route redirects here.

## Required production configuration

Set these values in the deployment host's `.env`; never commit them:

```dotenv
OPENROUTER_API_KEY=<existing hosted inference key>
# Optional additional approved operators:
FIN_TERMINAL_ALLOWED_EMAILS=
```

This deployment intentionally reuses the hosted trial worker's OpenRouter key
for inference. Apply a provider-side spend limit that accounts for both services.
`deploy.sh` creates `FIN_TERMINAL_PROXY_TOKEN` directly in the host `.env` when
it is absent. It is an independent 256-bit token; deployment replaces a token
that matches `OPENROUTER_API_KEY` rather than sending a billing credential to
the persistent terminal.

Every approved account in `ADMIN_EMAILS` can access the terminal.
`FIN_TERMINAL_ALLOWED_EMAILS` optionally adds other approved accounts. This
deployment has one shared archive and one active WebSocket owner, so all listed
administrators must intentionally share its state. A second principal is
rejected until the terminal process restarts.

Optional settings:

```dotenv
FIN_TERMINAL_OPENROUTER_MODEL=deepseek/deepseek-v4-flash-0731
FIN_TERMINAL_MAX_OUTPUT_TOKENS=4096
```

The authenticated singleton pins `MARKET_RESEARCH_PROMPT=compact` in Compose.
That reviewed variant applies the hard BRIEF output contract used by the
quality-gated research-cache pre-warm; changing it requires an infrastructure
release rather than an unreviewed host override.

## Request and network boundaries

1. Caddy deletes any client-provided terminal identity and proxy-token headers.
2. Caddy calls `web:8080/internal/fin-terminal/auth` with the user's normal
   session cookie.
3. `web` requires an approved admin/allowlisted email and returns a hashed,
   opaque principal.
4. Caddy removes browser session/API credentials, then injects the
   deployment-only persistent-terminal token before forwarding HTTP or the
   WebSocket upgrade.
5. The terminal accepts only the first authenticated principal for its process
   lifetime.

The terminal is not attached to the general `app` or `egress` networks. It uses
an internal Caddy network, the internal `unbrowser_mcp` network, and a dedicated
outbound network for OpenRouter. Its root filesystem is read-only and persistent
state is limited to `fin_terminal_data` mounted at `/data`.

The archive currently has no automatic retention limit. Treat the volume as
operator-visible shared state and remove old archives manually when required.

## Public Unbrowser landing page

The Unbrowser product landing page is served at:

- `https://unbrowser.unchainedsky.com/`

## Replay demo (retired)

The static fin-terminal replay demo at
`https://unbrowser.unchainedsky.com/fin-terminal-demo/` and all legacy redirects
are retired. All former demo URLs return direct HTTP 404 (no-store, no redirect).

The live-session pilot at `/fin-terminal-live-pilot/` is a separate, opt-in
public terminal path that is not enabled by default. See
[`public-live-terminal-pilot.md`](public-live-terminal-pilot.md) for activation
instructions.

## Verification

After deployment:

```bash
docker compose ps fin-terminal
docker compose exec -T fin-terminal \
  node -e "fetch('http://127.0.0.1:8787/api/ready').then(async r => { console.log(r.status, await r.text()); process.exit(r.ok ? 0 : 1) })"
```

From a logged-out browser or client:
- The Unbrowser root (`https://unbrowser.unchainedsky.com/`) must return `200`.
- The retired demo (`https://unbrowser.unchainedsky.com/fin-terminal-demo/`)
  must return `404` (no-store, no redirect).
- The bare `/fin-terminal` path must return `308` to its trailing-slash
  canonical URL.
- The authenticated `/fin-terminal/` route must return `401` when logged out;
  the former apex terminal must redirect to `/fin-terminal/`.
  From an approved allowlisted session, the page and `/fin-terminal/ws`
  WebSocket should load through Caddy.
- Direct container-network requests without `X-Fin-Terminal-Proxy-Token` must
  return `403`.

When updating the terminal, review its Dockerfile and dependency changes, run
its container smoke tests, then replace the full Git commit SHA in
`docker-compose.yml`.

## Market-event scout (singleton-only shadow mode)

The authenticated singleton runs a shadow-only market-event scout that
periodically retrieves public financial-event feeds via the internal
Unbrowser MCP and persists a journal to
`/data/market-terminal/market-event-scout.json`. It does not dispatch
model inference, precache writes, or canvas writes. It never runs on
public gateways, public session workers, workspace runtimes, or the local
CLI.

### Preflight (automated, pre-commit)

Every deployment runs a preflight verification **after** production health
checks and **before** writing deploy metadata (the commit point). The
preflight validates deterministic invariants:

- `NODE_ENV` is `production`, `PUBLIC_DEMO` is `0`.
- The container is the authenticated singleton (no public-gateway,
  public-session-worker, private-workspace, or financial-workspace-checkpoints
  markers).
- `MARKET_SCOUT_LOCAL_CLI` is `0`.
- `MARKET_SCOUT_ENABLED` is exactly `1` or `0`. When `0` (forward-disable),
  all enabled-only checks below are skipped and preflight passes cleanly.
- When enabled (`1`), additional invariants:
  - `UNBROWSER_MCP_REQUIRED` is `1`, `UNBROWSER_MCP_URL` is exactly
    `http://unbrowser-mcp:8767/mcp`.
  - `MARKET_DATA_DIR` is `/data/market-terminal`.
  - The computed journal path is `/data/market-terminal/market-event-scout.json`.
  - Exactly seven reviewed default sources are exported.
  - The data directory is truly writable (verified by unique temp-file create
    and remove).
  - If a journal already exists, the app's strict reader accepts it and it
    has no unknown source IDs.

A preflight failure prevents the deployment from committing metadata.
No secrets, URLs, symbols, titles, or journal contents appear in error
output — only safe aggregate values.

### Commissioning (automated, post-commit, enabled-only)

When the fin-terminal service was selected or recreated by the deployment
and the running container has `MARKET_SCOUT_ENABLED=1`, a commissioning
check runs **after** `DEPLOY_SUCCEEDED=true`. If the container has scout
disabled, commissioning is skipped cleanly. External feed success is
**not** part of the rollback transaction. The core deployment is already
committed; commissioning failure signals that a forward-disable deployment
is needed.

Commissioning polls the journal file read-only (via the app's strict reader)
for up to 20 minutes and requires:

1. Exactly seven known source IDs present in the journal (exact expected set).
2. All seven have a fresh `lastAttemptAt` timestamp from the current
   container lifetime (numeric epoch ms; ~2 seconds tolerance for
   host/container clock precision).
3. At least four sources have `baselineComplete: true` and a fresh
   `lastSuccessAt` within the container lifetime.
4. Those successful sources span at least three distinct source origin hosts.
5. A later persisted journal where `updatedAt` advances **and** at least one
   source `lastAttemptAt` advances, proving the scheduler re-armed.

The check never invokes the scout or sync, never prints journal JSON,
decision fields, source errors, URLs, symbols, titles, or env values.
It prints only aggregate counts and timestamps. Malformed state (strict
reader rejection) fails immediately. A missing journal during initial
commission is treated as pending (polls until timeout). Timeout errors
include aggregates only.

### Degraded / failure response

If commissioning fails the deployment job exits nonzero with a clear message:

```
MARKET-SCOUT COMMISSION FAILED
The core deployment is committed.
No automatic rollback occurs.
A forward-disable deployment (MARKET_SCOUT_ENABLED=0) is required.
```

Zero or low success across sources makes the deployment job red. The
response is a **forward-disable** deployment — setting `MARKET_SCOUT_ENABLED=0`
in `docker-compose.yml` — not a broad rollback. Third-party feed failure
does not trigger the existing rollback mechanism.

### Forward-disable runbook

To disable the scout:

1. Edit `docker-compose.yml` and change `MARKET_SCOUT_ENABLED=1` to
   `MARKET_SCOUT_ENABLED=0` in the `fin-terminal` service environment.
   Keep `MARKET_SCOUT_LOCAL_CLI=0`.
2. Commit, push, and run the normal deployment workflow.
3. Preflight will recognize the disabled state, skip enabled-only checks,
   and pass. Commission will be skipped because the running container has
   scout disabled. The deploy completes normally.
