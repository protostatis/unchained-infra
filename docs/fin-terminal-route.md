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
