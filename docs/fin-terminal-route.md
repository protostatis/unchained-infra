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
`deploy.sh` creates `FIN_TERMINAL_PROXY_TOKEN` and
`FIN_TERMINAL_DEMO_PROXY_TOKEN` directly in the host `.env` when either is
absent. They are independent 256-bit tokens; deployment replaces a token that
matches `OPENROUTER_API_KEY` or the other terminal token rather than sending a
billing credential or public-demo credential to the persistent terminal.

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

## Public demo route

The Unbrowser product landing page is served at:

- `https://unbrowser.unchainedsky.com/`

Its public financial-terminal demo is served at:

- `https://unbrowser.unchainedsky.com/fin-terminal-demo/`

Caddy does not call `forward_auth` for the public demo path. It removes browser
credentials and client-supplied identity headers, then injects a fixed `guest`
principal plus the separate demo-only proxy token before proxying to
`fin-terminal-demo` on port `8788`. The demo has isolated Caddy, MCP, and
OpenRouter egress networks, so it cannot reach the persistent terminal. The
terminal enforces its own per-IP rate limits (connections and inputs) in demo
mode, so the pinned Caddy image needs no custom modules.

`https://unchainedsky.com/unbrowser/fin-terminal-demo/` and the earlier
`/unbrowser/fin-terminal/demo` spelling permanently redirect to the canonical
demo path while preserving a path suffix and query string. The former
`https://unchainedsky.com/unbrowser` landing page redirects to the new root.

The demo service runs the same image with `PUBLIC_DEMO=1`: the UI shows a
PUBLIC DEMO banner and a waiting room when the singleton seat is taken (a new
visitor is rejected, never evicting the current one), the process exits after
`DEMO_IDLE_SECONDS` (300) without validated activity or after
`DEMO_MAX_SESSION_SECONDS` (1800), and the container restart policy hands the
seat to the next visitor. It has no persistent volume — every reset starts
pristine. Research remains enabled; set `FIN_TERMINAL_DEMO_OPENROUTER_KEY` in
the host `.env` to give the demo build its own provider-capped key instead of
sharing the production key.

The demo host and authenticated route are independent deployments of the same
singleton product, so an active demo visitor never disturbs the authenticated
terminal and vice versa.

## Verification

After deployment:

```bash
docker compose ps fin-terminal
docker compose exec -T fin-terminal \
  node -e "fetch('http://127.0.0.1:8787/api/ready').then(async r => { console.log(r.status, await r.text()); process.exit(r.ok ? 0 : 1) })"
```

From a logged-out browser or client, both the Unbrowser root and
`https://unbrowser.unchainedsky.com/fin-terminal-demo/` must return `200`.
The authenticated `/fin-terminal/` route must return `401` when logged out;
the former apex terminal must redirect to `/fin-terminal/`, and its landing and
demo URLs must redirect to their canonical paths. The demo HTML must reference
`/fin-terminal-demo/assets/`. From an approved allowlisted session, the page
and `/fin-terminal/ws` WebSocket should load through Caddy. Direct
container-network requests without `X-Fin-Terminal-Proxy-Token` must return
`403`.

When updating the terminal, review its Dockerfile and dependency changes, run
its container smoke tests, then replace the full Git commit SHA in
`docker-compose.yml`.
