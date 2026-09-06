# Financial terminal route

The authenticated browser-owned terminal is served at:

- `https://unbrowser.unchainedsky.com/fin-terminal-browser/`

The service is built from `Dockerfile.browser-terminal`, runs with
`TERMINAL_RUNTIME_MODE=browser`, and uses the profiled
`docker-compose.browser-terminal.yml` overlay. Its image must be an immutable
digest reference:

```dotenv
FIN_TERMINAL_BROWSER_IMAGE=ghcr.io/protostatis/unbrowser-fin-terminal-browser@sha256:<64 hex chars>
FIN_TERMINAL_BROWSER_PROXY_TOKEN=<independent 256-bit token>
FIN_TERMINAL_BROWSER_ENABLED=false
```

The Pi-backed singleton at `/fin-terminal/` has been retired. Its container and
dedicated network are removed during deployment once the new Caddy 404 contract
is live. The old `fin_terminal_data` volume is intentionally retained until an
operator makes a separate archive/deletion decision.

## Request and network boundaries

1. Caddy deletes client-provided identity, proxy-token, authorization, and
   control-token headers.
2. Caddy calls `web:8080/internal/fin-terminal/browser-auth` with the user's
   normal session cookie.
3. `web` requires an approved signed-in account and returns a stable opaque
   principal derived from the user ID.
4. Caddy removes browser credentials and injects only the browser-terminal
   proxy token before forwarding the request.
5. The browser broker never receives `OPENROUTER_API_KEY` or an account cookie.

The browser terminal has its own persistent data volume, MCP service, and
network boundary. Its MCP broker uses the reviewed 120-second idle and
900-second absolute session limits. It has no WebSocket path.

## Activation

Run the preflight before enabling the route:

```bash
./deploy/browser_terminal_canary_preflight.sh
docker compose --profile fin-terminal-browser-canary \
  -f docker-compose.yml -f docker-compose.browser-terminal.yml \
  up -d fin-terminal-browser-mcp fin-terminal-browser
docker compose --profile fin-terminal-browser-canary \
  -f docker-compose.yml -f docker-compose.browser-terminal.yml ps
```

After both services are healthy, set
`FIN_TERMINAL_BROWSER_ENABLED=true` and recreate Caddy with the same Compose
files. The protected GitHub workflow performs the commissioning and cookie
smoke test. Roll back by setting the flag to `false` and recreating Caddy.

The browser service persists a daily provider-budget ledger under `/data`.
Defaults are 40 research requests and 5 screenshot imports per account per UTC
day, with a global estimated provider budget of $25. Provider-side spend limits
remain required.

## Verification

After deployment:

- `https://unbrowser.unchainedsky.com/` returns `200`.
- `/fin-terminal/` and `/fin-terminal` return direct `404` when the private
  workspace route is disabled.
- `/fin-terminal-browser/` returns `404` while
  `FIN_TERMINAL_BROWSER_ENABLED=false`.
- A logged-out browser-terminal request reaches the auth gate and returns `401`
  after the route is enabled.
- An approved signed-in account receives `200` through Caddy.
- Former `/unbrowser/fin-terminal/*` URLs return direct `404` with no-store
  caching and no redirect.

The private workspace runtime at `/fin-terminal/` and the opt-in public live
pilot at `/fin-terminal-live-pilot/` are separate systems. They are not the
retired singleton and are not changed by this browser-terminal retirement.
