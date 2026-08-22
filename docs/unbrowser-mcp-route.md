# unbrowser MCP Route

This stack exposes a separate hosted `unbrowser` MCP endpoint without changing
the main Unchained FastMCP endpoint at `/mcp`.

## Public Routes

- Streamable HTTP: `https://unchainedsky.com/unbrowser-mcp`
- Explicit Streamable HTTP path: `https://unchainedsky.com/unbrowser-mcp/mcp`
- Health/status: `https://unchainedsky.com/unbrowser-mcp/status`

`Caddyfile` rewrites the exact `/unbrowser-mcp` path to the broker's internal
`/mcp` endpoint and strips the `/unbrowser-mcp` prefix for subpaths.

The route accepts Streamable HTTP. The broker proxies Streamable HTTP responses
(including a response stream when the protocol needs one), but does not expose
the legacy standalone `/sse` or `/messages/` transport paths.

## Runtime

`docker-compose.yml` runs a dedicated `unbrowser-mcp` service on port `8767`.
The service uses `Dockerfile.unbrowser-mcp`, installs pinned PyPI packages, and
runs `unbrowser_mcp_router.py`. For every successfully initialized public MCP
session, the router starts a dedicated loopback worker:

```bash
mcp-proxy --host 127.0.0.1 --port <worker-port> --pass-environment -- unbrowser --mcp
```

The worker owns exactly one stateful Unbrowser process. Its cookie jar, DOM,
JavaScript state, and temporary home directory are not shared with another MCP
session. The router replaces the worker's session ID with a fresh opaque public
`Mcp-Session-Id`, so clients cannot route requests to another worker by knowing
an internal ID.

Workers are bounded and cleaned up as follows:

- `DELETE /mcp` closes the worker immediately.
- An abandoned worker self-closes after **120 seconds of idle time** (no request
  in flight).
- A worker is retired after a 15-minute lifetime once its current request has
  finished, even if a client keeps sending new requests.
- The service admits at most eight concurrent sessions by default (six in the
  public-terminal and workspace overlays).

The limits are configurable with `UNBROWSER_MCP_MAX_SESSIONS`,
`UNBROWSER_MCP_IDLE_TIMEOUT_SECONDS`, and
`UNBROWSER_MCP_MAX_SESSION_SECONDS`. The service uses a loopback-only worker
port range and starts as Docker's init process so worker process groups are
reaped on expiry and shutdown.

Current pins:

- `pyunbrowser==0.0.21`
- `mcp-proxy==0.12.0`

Update `Dockerfile.unbrowser-mcp` when publishing a new hosted unbrowser MCP
release.

This dedicated hosted MCP pin is independent from the `pyunbrowser` pin in the
root `Dockerfile`, which powers the main web image and its live demo. Update the
root image, demo runtime metadata, and matching contract test together during a
separate full-stack deployment.

## Network Isolation

The hosted endpoint is public and unauthenticated, so it must not get direct
access to Unchained's internal services or metadata endpoints.

- `unbrowser-mcp` is not attached to the main `app` network.
- Caddy reaches `unbrowser-mcp` through the dedicated internal
  `unbrowser_mcp` network.
- `unbrowser-mcp` has no direct internet egress. It can only reach
  `unbrowser-egress` through the dedicated internal `unbrowser_egress_proxy`
  network.
- `unbrowser-egress` is a small HTTP/CONNECT proxy that resolves target hosts and
  rejects non-global addresses, including private, loopback, link-local, and
  metadata addresses. It currently allows only ports `80` and `443`.
- If the optional unbrowser service fails to start, Caddy still starts and serves
  the rest of production ingress.

## Operational Notes

- The route is intentionally separate from `/mcp`, which remains Unchained's
  authenticated FastMCP server for real Chrome workflows.
- The endpoint is public and unauthenticated. Its Unbrowser state is isolated
  per MCP session, but its opaque `Mcp-Session-Id` remains a bearer credential
  for that session until it expires. Do not disclose it or use this public
  endpoint for private cookies or secrets. Caddy redacts this header from
  access logs.
