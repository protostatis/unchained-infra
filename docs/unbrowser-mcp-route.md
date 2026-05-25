# unbrowser MCP Route

This stack exposes a separate hosted `unbrowser` MCP endpoint without changing
the main Unchained FastMCP endpoint at `/mcp`.

## Public Routes

- Streamable HTTP: `https://unchainedsky.com/unbrowser-mcp`
- Explicit Streamable HTTP path: `https://unchainedsky.com/unbrowser-mcp/mcp`
- SSE compatibility path: `https://unchainedsky.com/unbrowser-mcp/sse`
- Health/status: `https://unchainedsky.com/unbrowser-mcp/status`

`Caddyfile` rewrites the exact `/unbrowser-mcp` path to the proxy's internal
`/mcp` endpoint and strips the `/unbrowser-mcp` prefix for subpaths.

## Runtime

`docker-compose.yml` runs a dedicated `unbrowser-mcp` service on port `8767`.
The service uses `Dockerfile.unbrowser-mcp`, installs pinned PyPI packages, and
bridges unbrowser's stdio MCP server to HTTP with `mcp-proxy`:

```bash
mcp-proxy --host 0.0.0.0 --port 8767 --pass-environment -- unbrowser --mcp
```

Current pins:

- `pyunbrowser==0.0.14`
- `mcp-proxy==0.12.0`

Update `Dockerfile.unbrowser-mcp` when publishing a new hosted unbrowser MCP
release.

## Operational Notes

- The route is intentionally separate from `/mcp`, which remains Unchained's
  authenticated FastMCP server for real Chrome workflows.
- The service is attached to both `app` and `egress`: `app` lets Caddy reach it,
  and `egress` lets unbrowser fetch public web pages.
- The endpoint is a shared hosted process. Use it for public discovery, smoke
  tests, and directory validation. Do not replay private cookies or secrets
  through it unless per-user isolation or auth is added.
