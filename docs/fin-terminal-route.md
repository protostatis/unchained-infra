# Financial terminal route

The singleton financial terminal is served at:

- `https://unchainedsky.com/unbrowser/fin-terminal/`

The Compose build is pinned to the full commit SHA from
`protostatis/unbrowser-fin-terminal`. `PUBLIC_BASE_PATH` is fixed at build time,
and Caddy strips `/unbrowser/fin-terminal` before proxying to port `8787`.

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
it is absent. If it ever matches `OPENROUTER_API_KEY`, deployment replaces it
with an independent 256-bit token rather than sending a billing credential in
an internal HTTP header.

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
4. Caddy injects the deployment-only proxy token before forwarding HTTP or the
   WebSocket upgrade to the terminal.
5. The terminal accepts only the first authenticated principal for its process
   lifetime.

The terminal is not attached to the general `app` or `egress` networks. It uses
an internal Caddy network, the internal `unbrowser_mcp` network, and a dedicated
outbound network for OpenRouter. Its root filesystem is read-only and persistent
state is limited to `fin_terminal_data` mounted at `/data`.

The archive currently has no automatic retention limit. Treat the volume as
operator-visible shared state and remove old archives manually when required.

## Public demo route

The same terminal is also served to anonymous visitors at:

- `https://unchainedsky.com/unbrowser/fin-terminal-demo/`

Caddy does not call `forward_auth` for this route. It injects a fixed
`guest` principal instead, strips any client-supplied identity headers, applies
per-IP rate limits (WebSocket and HTTP zones), and proxies to the separate
`fin-terminal-demo` service on port `8788`.

The demo service runs the same image with `PUBLIC_DEMO=1`: the UI shows a
PUBLIC DEMO banner and a waiting room when the singleton seat is taken, and the
process exits after `DEMO_IDLE_SECONDS` (300) without WebSocket activity so the
container restart policy hands the seat to the next visitor. It has no
persistent volume — every reset starts pristine. Research remains enabled; give
the demo build its own provider-capped OpenRouter key if public spend matters.

The demo and authenticated routes are independent deployments of the same
singleton product, so an active demo visitor never disturbs the authenticated
terminal and vice versa.

## Verification

After deployment:

```bash
docker compose ps fin-terminal
docker compose exec -T fin-terminal \
  node -e "fetch('http://127.0.0.1:8787/api/ready').then(async r => { console.log(r.status, await r.text()); process.exit(r.ok ? 0 : 1) })"
```

From a logged-out browser or client, the public route must return `401`. From an
approved allowlisted session, the page and `/unbrowser/fin-terminal/ws`
WebSocket should load through Caddy. Direct container-network requests without
`X-Fin-Terminal-Proxy-Token` must return `403`.

When updating the terminal, review its Dockerfile and dependency changes, run
its container smoke tests, then replace the full Git commit SHA in
`docker-compose.yml`.
