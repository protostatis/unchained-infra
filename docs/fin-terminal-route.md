# Financial terminal route

The singleton financial terminal is served at:

- `https://unchainedsky.com/unbrowser/fin-terminal/`

The Compose build is pinned to the full commit SHA from
`protostatis/unbrowser-fin-terminal`. `PUBLIC_BASE_PATH` is fixed at build time,
and Caddy strips `/unbrowser/fin-terminal` before proxying to port `8787`.

## Required production configuration

Set these values in the deployment host's `.env`; never commit them:

```dotenv
FIN_TERMINAL_OPENROUTER_API_KEY=<dedicated OpenRouter key>
FIN_TERMINAL_PROXY_TOKEN=<independent random token>
FIN_TERMINAL_ALLOWED_EMAILS=operator@example.com
```

Generate the proxy token independently from every other service credential, for
example with `openssl rand -hex 32`. The OpenRouter key must also be dedicated to
this service rather than reusing the hosted trial worker's key. Apply a
provider-side spend limit appropriate for this single-operator deployment.

`FIN_TERMINAL_ALLOWED_EMAILS` adds approved accounts to `ADMIN_EMAILS`. Because
this deployment has one shared archive and one active WebSocket owner, configure
one operator email unless all listed administrators intentionally share its
state. A second principal is rejected until the terminal process restarts.

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
