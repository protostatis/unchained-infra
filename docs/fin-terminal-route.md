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

## Browser-owned canary

The browser-owned implementation is staged separately at:

- `https://unbrowser.unchainedsky.com/fin-terminal-browser/`

It is not the production `/fin-terminal/` implementation and must not reuse the
workspace control-plane service. The optional
`docker-compose.browser-terminal.yml` overlay starts two profiled services:

- `fin-terminal-browser`: a prebuilt `Dockerfile.browser-terminal` image with
  `TERMINAL_RUNTIME_MODE=browser` and a dedicated persistent volume;
- `fin-terminal-browser-mcp`: a dedicated Unbrowser MCP instance/network so a
  canary cannot consume the singleton terminal's MCP session pool.

The overlay requires `FIN_TERMINAL_BROWSER_IMAGE` to be an immutable image
reference (`repository@sha256:<64 hex chars>`) and
`FIN_TERMINAL_BROWSER_PROXY_TOKEN` to be independent from the Pi terminal
token. The route is fail-closed while `FIN_TERMINAL_BROWSER_ENABLED=false` and
returns a no-store 404 rather than the landing page.

Commission the services before enabling the edge route:

```bash
./deploy/browser_terminal_canary_preflight.sh
docker compose --profile fin-terminal-browser-canary \
  -f docker-compose.yml -f docker-compose.browser-terminal.yml \
  up -d fin-terminal-browser-mcp fin-terminal-browser
docker compose --profile fin-terminal-browser-canary \
  -f docker-compose.yml -f docker-compose.browser-terminal.yml ps
```

The preflight also starts the digest-pinned browser image on an isolated
network and checks its production `/api/ready` startup contract. The broker and
dedicated MCP service use the reviewed 120-second idle and 900-second absolute
session limits; do not change one side without changing the other.

Confirm both services are healthy, then set
`FIN_TERMINAL_BROWSER_ENABLED=true` in the host `.env` and recreate Caddy with
the same two Compose files. Roll back by setting the flag to `false` and
recreating Caddy; the Pi `/fin-terminal/` route is unchanged throughout the
canary. The protected activation workflow additionally refuses a dirty or stale
host infra worktree and requires a dedicated approved-account cookie smoke
before completing activation.

The browser route uses the dedicated `web:8080/internal/fin-terminal/browser-auth`
gate. Any approved signed-in UnchainedSky account is admitted; pending or
rejected accounts and sessions without a stable user ID are denied. The
principal is derived from that stable user ID, not the user's email. Caddy
strips client identity/API credentials and injects only the browser canary
proxy token. It has no WebSocket path and the browser broker must never receive
`OPENROUTER_API_KEY` or an account cookie.

The browser service also persists a daily provider-budget ledger under `/data`.
Its JSON ledger uses a SQLite sidecar transaction lock so overlapping broker
processes cannot lose reservations.
By default, each account may make 40 research requests and 5 screenshot imports
per UTC day, while the canary reserves no more than $25 of estimated provider
cost globally. The `FIN_TERMINAL_BROWSER_*` overrides in the Compose overlay may
lower or raise these reviewed limits; provider-side spend limits remain required.
The protected activation workflow also requires the
`FIN_TERMINAL_BROWSER_SMOKE_COOKIE` production secret for a dedicated approved
test account. It must see both the expected logged-out `401` and an authenticated
`200` before the route is considered enabled; the cookie is copied to the host
only for the smoke check and then removed.

## Market-event scout (singleton-only guarded dispatch)

The authenticated singleton retrieves public financial-event feeds via the
internal Unbrowser MCP and evaluates a bounded trigger dry run. Validated
targets map to ticker BRIEF, macro EVENTS BRIEF, or SIGNALS/Market Story BRIEF
proposals. The fixed evaluation policy requires P80, a two-hour publication TTL,
a six-hour target cooldown, and no more than eight `would-trigger` outcomes per
UTC day. Real execution is separate and default-off: when enabled it uses only
`nvidia/nemotron-3.5-lightning:free`, accepts at most one job per poll and four
attempts per UTC day, and fails closed without a paid fallback. It persists
decisions, candidates, gate reasons, aggregates, and the candidate-ID dispatch
outbox to `/data/market-terminal/market-event-scout.json`. It never runs on
public gateways, public session workers, workspace runtimes, or the local CLI.

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
- `MARKET_SCOUT_DISPATCH_ENABLED` is exactly `0` or `1`; `1` requires
  `MARKET_SCOUT_ENABLED=1` and `MARKET_SCOUT_MODEL_ID` exactly
  `nvidia/nemotron-3.5-lightning:free`.
- When dispatch is enabled, `MARKET_SCOUT_DISPATCH_PER_RUN` must be exactly
  `1` and `MARKET_SCOUT_DISPATCH_DAILY_CAP` must be exactly `4`; these limits
  are not operator-overridable.
- When enabled (`1`), additional invariants:
  - `UNBROWSER_MCP_REQUIRED` is `1`, `UNBROWSER_MCP_URL` is exactly
    `http://unbrowser-mcp:8767/mcp`.
  - `MARKET_DATA_DIR` is `/data/market-terminal`.
  - The computed journal path is `/data/market-terminal/market-event-scout.json`.
  - Exactly seven reviewed default sources are exported.
  - The app exports the reviewed trigger mapper/evaluator and exact simulation
    policy (`v1`, P80, 2h TTL, 6h target cooldown, eight/day).
  - The data directory is truly writable (verified by unique temp-file create
    and remove).
  - If a journal already exists, the app's strict reader accepts it and it
    has no unknown source IDs. A valid persisted journal v1 is accepted only
    through the candidate app's in-memory v3 migration; retained decisions are
    not backfilled as candidates.
  - The strict reader returns the v3 trigger-dry-run/outbox envelope, exact policy,
    bounded record arrays, and internally consistent aggregate counters.

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

1. The on-disk journal has been atomically persisted as raw schema v3 with
   `triggerDryRun` and `triggerDispatches` envelopes. A still-persisted v1 or v2
   journal is pending, not a success, until the scheduler writes v3.
2. The strict reader verifies the exact dry-run policy and aggregate contract.
3. Exactly seven known source IDs present in the journal (exact expected set).
4. All seven have a fresh `lastAttemptAt` timestamp from the current
   container lifetime (numeric epoch ms; ~2 seconds tolerance for
   host/container clock precision).
5. At least four sources have `baselineComplete: true` and a fresh
   `lastSuccessAt` within the container lifetime.
6. Those successful sources span at least three distinct source origin hosts.
7. A later persisted journal where `updatedAt` advances **and** at least one
   source `lastAttemptAt` advances, proving the scheduler re-armed.

The check never invokes the scout or sync, never prints journal JSON,
decision fields, source errors, URLs, symbols, titles, or env values.
It prints only aggregate counts, journal version, policy-verification status,
and timestamps. Malformed state (strict reader rejection) fails immediately.
A missing journal or valid persisted v1 during initial commission is treated as
pending (polls until timeout). Timeout errors include aggregates only.

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

Journal v3 is intentionally fail-closed for application versions that predate
the trigger outbox. For a planned rollback to a pre-v3 app, forward-disable the
scout first. If an unrelated pre-commit deployment failure automatically
restores a pre-v3 image after v3 was already written, the core terminal remains
available but that old scout cannot resume; deploy `MARKET_SCOUT_ENABLED=0` or
restore a v3-compatible app rather than modifying the journal by hand.

### Forward-disable runbook

To disable the scout:

1. Edit `docker-compose.yml` and change `MARKET_SCOUT_ENABLED=1` to
   `MARKET_SCOUT_ENABLED=0` in the `fin-terminal` service environment.
   Keep `MARKET_SCOUT_LOCAL_CLI=0`.
2. Commit, push, and run the normal deployment workflow.
3. Preflight will recognize the disabled state, skip enabled-only checks,
   and pass. Commission will be skipped because the running container has
   scout disabled. The deploy completes normally.
