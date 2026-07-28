# unbrowser MCP Route

This stack exposes a separate hosted `unbrowser` MCP endpoint without changing
the main Unchained FastMCP endpoint at `/mcp`.

## Public Routes

- Streamable HTTP: `https://unchainedsky.com/unbrowser-mcp`
- Explicit Streamable HTTP path: `https://unchainedsky.com/unbrowser-mcp/mcp`
- Health/status: `https://unchainedsky.com/unbrowser-mcp/status`

`Caddyfile` rewrites the exact `/unbrowser-mcp` path to the proxy's internal
`/mcp` endpoint and strips the `/unbrowser-mcp` prefix for subpaths.

SSE is intentionally not exposed on this prefixed route. `mcp-proxy` advertises
an absolute `/messages/` callback for SSE, which would collide with the main web
stack unless this service had its own root path or subdomain.

## Runtime

`docker-compose.yml` runs a dedicated `unbrowser-mcp` service on port `8767`.
The service uses `Dockerfile.unbrowser-mcp`, installs pinned PyPI packages, and
bridges unbrowser's stdio MCP server to HTTP with `mcp-proxy`:

```bash
mcp-proxy --host 0.0.0.0 --port 8767 --pass-environment -- unbrowser --mcp
```

Current pins:

- `pyunbrowser==0.0.18`
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
- The endpoint is a shared hosted process. Use it for public discovery, smoke
  tests, and directory validation. Do not replay private cookies or secrets
  through it unless per-user isolation or auth is added.
